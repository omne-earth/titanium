from __future__ import annotations

import queue
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from titanium.environments.cella.client import (
    CellaControlClient,
    ControlClientError,
)
from titanium.environments.cella.control import (
    ControllerReady,
    ExecRequest,
    ExecResponse,
)
from titanium.environments.cella.wire import (
    FrameDecoder,
    encode_controller_ready,
    encode_exec_response,
)


def sample_request() -> ExecRequest:
    return ExecRequest(
        request_id=7,
        argv=("/bin/bash", "-c", "printf hello"),
        cwd="/app",
        env={"MODE": "test"},
        timeout_sec=30,
        user=1000,
    )


def _recv_one(
    conn: socket.socket,
) -> ExecRequest | ExecResponse:
    decoder = FrameDecoder()

    while True:
        data = conn.recv(4096)

        if not data:
            raise AssertionError("Client disconnected before sending a message.")

        messages = decoder.feed(data)

        if messages:
            assert len(messages) == 1
            return messages[0]


def _start_peer(
    path: Path,
    handler: Callable[[socket.socket], None],
) -> tuple[threading.Thread, queue.Queue[Exception]]:
    listener = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )
    listener.bind(str(path))
    listener.listen(1)

    errors: queue.Queue[Exception] = queue.Queue()

    def run() -> None:
        try:
            conn, _ = listener.accept()

            with conn:
                # Every connection begins with the controller announcing
                # itself; these handlers all describe what happens after
                # that. The handshake itself is tested separately.
                conn.sendall(encode_controller_ready(ControllerReady(1)))
                handler(conn)
        except Exception as exc:  # noqa: BLE001 - forward peer-thread failures to pytest
            errors.put(exc)
        finally:
            listener.close()

    thread = threading.Thread(
        target=run,
        daemon=True,
    )
    thread.start()

    return thread, errors


def _finish_peer(
    thread: threading.Thread,
    errors: queue.Queue[Exception],
) -> None:
    thread.join(timeout=2)

    assert not thread.is_alive()

    if not errors.empty():
        raise errors.get()


def test_client_round_trip(tmp_path: Path):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    response = ExecResponse(
        request_id=request.request_id,
        stdout=b"hello",
        stderr=b"",
        return_code=0,
    )

    def peer(conn: socket.socket) -> None:
        received = _recv_one(conn)
        assert received == request
        conn.sendall(encode_exec_response(response))

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        assert (
            client.execute(
                request,
                response_timeout_sec=1,
            )
            == response
        )
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_client_handles_split_response(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    response = ExecResponse(
        request_id=request.request_id,
        stdout=b"split-response",
        stderr=b"",
        return_code=0,
    )

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        frame = encode_exec_response(response)

        conn.sendall(frame[:2])
        conn.sendall(frame[2:11])
        conn.sendall(frame[11:])

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        assert (
            client.execute(
                request,
                response_timeout_sec=1,
            )
            == response
        )
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_mismatched_request_id_is_refused(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_response(
                ExecResponse(
                    request_id=request.request_id + 1,
                    stdout=b"",
                    stderr=b"",
                    return_code=0,
                )
            )
        )

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        with pytest.raises(
            ControlClientError,
            match="request ID",
        ):
            client.execute(
                request,
                response_timeout_sec=1,
            )
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_disconnect_before_response_is_refused(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request
        # Returning closes the accepted connection without responding.

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        with pytest.raises(
            ControlClientError,
            match="disconnected",
        ):
            client.execute(
                request,
                response_timeout_sec=1,
            )
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_response_timeout_is_bounded(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request
        time.sleep(0.2)

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        with pytest.raises(
            ControlClientError,
            match="Timed out",
        ):
            client.execute(
                request,
                response_timeout_sec=0.05,
            )
    finally:
        client.close()

    _finish_peer(thread, errors)


# ------------------------------------------------------------- the handshake

# The control socket exists as soon as the VMM does, long before the guest
# controller has claimed the serial line and put it into raw mode. These tests
# use a peer that does *not* auto-announce, so the handshake itself is under
# test rather than assumed.


def _start_raw_peer(
    path: Path,
    handler: Callable[[socket.socket], None],
) -> tuple[threading.Thread, queue.Queue[Exception]]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    errors: queue.Queue[Exception] = queue.Queue()

    def run() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                handler(conn)
        except Exception as exc:  # noqa: BLE001 - forward peer-thread failures
            errors.put(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def test_a_client_is_not_ready_until_the_controller_says_so(tmp_path: Path):
    socket_path = tmp_path / "control.sock"

    def peer(conn: socket.socket) -> None:
        conn.sendall(encode_controller_ready(ControllerReady(1)))
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    assert client.is_ready is False
    client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is True
    client.close()
    assert client.is_ready is False


def test_execute_before_connect_is_refused(tmp_path: Path):
    client = CellaControlClient(tmp_path / "control.sock")
    with pytest.raises(ControlClientError, match="not connected"):
        client.execute(sample_request())


def test_a_peer_that_never_announces_times_out_and_is_closed(tmp_path: Path):
    socket_path = tmp_path / "control.sock"

    def peer(conn: socket.socket) -> None:
        time.sleep(2.0)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="Timed out waiting"):
        client.connect(ready_timeout_sec=0.3)
    assert client.is_ready is False
    with pytest.raises(ControlClientError, match="not connected"):
        client.execute(sample_request())


def test_a_peer_that_closes_before_announcing_is_refused(tmp_path: Path):
    socket_path = tmp_path / "control.sock"
    _start_raw_peer(socket_path, lambda conn: None)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="closed before announcing"):
        client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is False


def test_a_peer_that_opens_with_the_wrong_message_is_refused(tmp_path: Path):
    """An exec result before readiness is not a controller this client knows."""
    socket_path = tmp_path / "control.sock"
    response = ExecResponse(request_id=1, stdout=b"", stderr=b"", return_code=0)

    def peer(conn: socket.socket) -> None:
        conn.sendall(encode_exec_response(response))
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="did not open with a controller"):
        client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is False


def test_a_peer_announcing_the_wrong_protocol_version_is_refused(tmp_path: Path):
    socket_path = tmp_path / "control.sock"

    def peer(conn: socket.socket) -> None:
        conn.sendall(encode_controller_ready(ControllerReady(2)))
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="protocol version 2"):
        client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is False


def test_a_peer_that_speaks_past_its_readiness_is_refused(tmp_path: Path):
    """Trailing frames mean the stream position is no longer certain."""
    socket_path = tmp_path / "control.sock"
    response = ExecResponse(request_id=1, stdout=b"", stderr=b"", return_code=0)

    def peer(conn: socket.socket) -> None:
        conn.sendall(
            encode_controller_ready(ControllerReady(1)) + encode_exec_response(response)
        )
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="after its readiness"):
        client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is False


def test_an_unreadable_readiness_frame_is_refused(tmp_path: Path):
    socket_path = tmp_path / "control.sock"

    def peer(conn: socket.socket) -> None:
        payload = b'{"v":1,"type":"controller_ready"'
        conn.sendall(len(payload).to_bytes(4, "big") + payload)
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    with pytest.raises(ControlClientError, match="unreadable readiness"):
        client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is False


def test_a_readiness_frame_split_across_reads_still_works(tmp_path: Path):
    socket_path = tmp_path / "control.sock"

    def peer(conn: socket.socket) -> None:
        wire = encode_controller_ready(ControllerReady(1))
        conn.sendall(wire[:2])
        time.sleep(0.05)
        conn.sendall(wire[2:9])
        time.sleep(0.05)
        conn.sendall(wire[9:])
        time.sleep(0.2)

    _start_raw_peer(socket_path, peer)
    client = CellaControlClient(socket_path)
    client.connect(ready_timeout_sec=5.0)
    assert client.is_ready is True
    client.close()
