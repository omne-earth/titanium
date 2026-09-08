from __future__ import annotations

import socket
import time
from pathlib import Path

from titanium.environments.cella.control import (
    ControllerReady,
    ControlProtocolError,
    ExecComplete,
    ExecOutputChunk,
    ExecRequest,
    ExecResponse,
)
from titanium.environments.cella.wire import (
    PROTOCOL_VERSION,
    FrameDecoder,
    WireProtocolError,
    encode_exec_request,
)

RECV_CHUNK_BYTES = 64 * 1024

# This is a total in-memory result budget, not a frame-size limit.
#
# Individual output frames remain much smaller. A command may emit many
# chunks, but because ExecResponse ultimately contains the complete stdout
# and stderr in memory, the host needs an explicit overall resource bound.
DEFAULT_MAX_EXEC_OUTPUT_BYTES = 64 * 1024 * 1024


class ControlClientError(RuntimeError):
    pass


class CellaControlClient:
    def __init__(
        self,
        socket_path: str | Path,
        *,
        max_output_bytes: int = DEFAULT_MAX_EXEC_OUTPUT_BYTES,
    ) -> None:
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer.")

        self.socket_path = Path(socket_path)
        self.max_output_bytes = max_output_bytes
        self._socket: socket.socket | None = None
        self._decoder = FrameDecoder()
        self._ready = False

    def _invalidate_connection(self) -> None:
        """Discard a connection whose protocol state can no longer be trusted."""

        sock = self._socket

        self._socket = None
        self._decoder = FrameDecoder()
        self._ready = False

        if sock is None:
            return

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            # The peer may already have closed or reset the connection.
            pass

        sock.close()

    @property
    def is_ready(self) -> bool:
        """Whether a controller has announced itself on this connection."""
        return self._ready

    def connect(
        self,
        *,
        timeout_sec: float | None = 5.0,
        ready_timeout_sec: float | None = 30.0,
    ) -> None:
        if self._socket is not None:
            raise ControlClientError("Control client is already connected.")

        _validate_timeout(timeout_sec)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            sock.settimeout(timeout_sec)
            sock.connect(str(self.socket_path))
            sock.settimeout(None)
        except OSError as exc:
            sock.close()
            raise ControlClientError(
                f"Could not connect to Cella control socket: {self.socket_path}"
            ) from exc

        # A new connection always begins with fresh framing state, and
        # unready: the socket exists as soon as the VMM does, well before the
        # guest controller has claimed the serial line and put it into raw
        # mode. A request written before then would be fed to a tty that still
        # echoes and line-edits.
        self._socket = sock
        self._decoder = FrameDecoder()
        self._ready = False

        self._await_ready(sock, timeout_sec=ready_timeout_sec)

    def _await_ready(self, sock: socket.socket, *, timeout_sec: float | None) -> None:
        """Read exactly one ControllerReady, or close the connection.

        Deliberately not a negotiation. There is one protocol version, and a
        peer that opens with anything else -- a different version, a different
        message, two messages, or trailing bytes -- is not a controller this
        client knows how to talk to.
        """
        _validate_timeout(timeout_sec)
        deadline = None if timeout_sec is None else time.monotonic() + timeout_sec

        try:
            while True:
                sock.settimeout(_remaining(deadline))
                data = sock.recv(RECV_CHUNK_BYTES)
                if not data:
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer closed before announcing readiness."
                    )

                try:
                    messages = self._decoder.feed(data)
                except ControlProtocolError as exc:
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer sent an unreadable readiness frame."
                    ) from exc

                if not messages:
                    continue

                first = messages[0]
                if not isinstance(first, ControllerReady):
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer did not open with a controller "
                        "readiness message."
                    )
                if first.protocol_version != PROTOCOL_VERSION:
                    self._invalidate_connection()
                    raise ControlClientError(
                        f"Cella control peer speaks protocol version "
                        f"{first.protocol_version}, not {PROTOCOL_VERSION}."
                    )
                # Anything decoded behind the readiness frame, or any partial
                # frame left buffered, means the peer spoke before it was
                # asked to and the stream position is no longer certain.
                if len(messages) > 1 or self._decoder.pending_bytes != 0:
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer sent data after its readiness message."
                    )

                self._ready = True
                return
        except TimeoutError as exc:
            self._invalidate_connection()
            raise ControlClientError(
                "Timed out waiting for the Cella controller to announce readiness."
            ) from exc
        except OSError as exc:
            self._invalidate_connection()
            raise ControlClientError(
                "Cella control connection failed before readiness."
            ) from exc
        finally:
            if self._socket is not None:
                self._socket.settimeout(None)

    def execute(
        self,
        request: ExecRequest,
        *,
        response_timeout_sec: float | None = None,
    ) -> ExecResponse:
        sock = self._socket

        if sock is None:
            raise ControlClientError("Control client is not connected.")

        # The readiness frame is the only thing that makes the tty safe to
        # write to. Without it this would be an ordinary programming error
        # with a very confusing symptom, so it is refused here.
        if not self._ready:
            raise ControlClientError("Cella control peer has not announced readiness.")

        _validate_timeout(response_timeout_sec)

        deadline = (
            None
            if response_timeout_sec is None
            else time.monotonic() + response_timeout_sec
        )

        stdout = bytearray()
        stderr = bytearray()
        saw_streamed_output = False

        try:
            sock.settimeout(_remaining(deadline))
            sock.sendall(encode_exec_request(request))

            while True:
                sock.settimeout(_remaining(deadline))
                data = sock.recv(RECV_CHUNK_BYTES)

                if not data:
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer disconnected before responding."
                    )

                try:
                    messages = self._decoder.feed(data)
                except WireProtocolError as exc:
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Cella control peer sent an invalid wire message."
                    ) from exc

                if not messages:
                    continue

                for index, message in enumerate(messages):
                    if message.request_id != request.request_id:
                        self._invalidate_connection()
                        raise ControlClientError(
                            "Control response request ID does not match the request."
                        )

                    if isinstance(message, ExecOutputChunk):
                        saw_streamed_output = True

                        new_total = len(stdout) + len(stderr) + len(message.data)

                        if new_total > self.max_output_bytes:
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella command output exceeded the configured limit."
                            )

                        if message.stream == "stdout":
                            stdout.extend(message.data)
                        elif message.stream == "stderr":
                            stderr.extend(message.data)
                        else:
                            # wire.py should already reject this. Keep the client
                            # fail-closed if that invariant ever changes.
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella control peer sent an invalid output stream."
                            )

                        continue

                    if isinstance(message, ExecComplete):
                        # Completion closes this request's message sequence.
                        # Anything already decoded after it, or any partial frame
                        # left buffered behind it, makes stream state ambiguous.
                        if (
                            index != len(messages) - 1
                            or self._decoder.pending_bytes != 0
                        ):
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella control peer sent data after exec completion."
                            )

                        return ExecResponse(
                            request_id=request.request_id,
                            stdout=bytes(stdout),
                            stderr=bytes(stderr),
                            return_code=message.return_code,
                        )

                    if isinstance(message, ExecResponse):
                        # The single-frame response form. Once streamed
                        # output has begun, switching response formats
                        # mid-request is ambiguous.
                        if saw_streamed_output:
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella control peer mixed response formats."
                            )

                        if (
                            index != len(messages) - 1
                            or self._decoder.pending_bytes != 0
                        ):
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella control peer sent data after exec response."
                            )

                        if (
                            len(message.stdout) + len(message.stderr)
                            > self.max_output_bytes
                        ):
                            self._invalidate_connection()
                            raise ControlClientError(
                                "Cella command output exceeded the configured limit."
                            )

                        return message

                    # A guest must not send host-to-guest requests or any other
                    # unexpected message type on the response side.
                    self._invalidate_connection()
                    raise ControlClientError(
                        "Unexpected message from Cella control peer."
                    )

        except TimeoutError as exc:
            self._invalidate_connection()
            raise ControlClientError(
                "Timed out waiting for the Cella control peer."
            ) from exc

        except OSError as exc:
            self._invalidate_connection()
            raise ControlClientError("Cella control socket I/O failed.") from exc

        finally:
            # A timeout belongs to this operation. If the connection survived,
            # restore blocking behavior for the next operation.
            if self._socket is not None:
                self._socket.settimeout(None)

    def close(self) -> None:
        self._invalidate_connection()


def _validate_timeout(timeout_sec: float | None) -> None:
    if timeout_sec is None:
        return

    if (
        isinstance(timeout_sec, bool)
        or not isinstance(timeout_sec, (int, float))
        or timeout_sec <= 0
    ):
        raise ValueError("Timeout must be a positive number or None.")


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None

    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise TimeoutError

    return remaining
