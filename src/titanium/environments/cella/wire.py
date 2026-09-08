from __future__ import annotations

import base64
import binascii
import json
import struct
from dataclasses import dataclass, field
from typing import Any

from titanium.environments.cella.control import (
    CONTROL_ERROR_CODES,
    ControlError,
    ControllerReady,
    ControlProtocolError,
    ExecComplete,
    ExecOutputChunk,
    ExecRequest,
    ExecResponse,
    FinalizeComplete,
    FinalizeRequest,
    validate_exec_request,
)

PROTOCOL_VERSION = 1
HEADER_SIZE = 4

# A malformed or hostile peer must not be able to make us allocate an
# unbounded frame merely by claiming an enormous length.
MAX_FRAME_BYTES = 16 * 1024 * 1024

# This limits ONE stdout/stderr message, not the total output of a command.
# Large legitimate output can travel as many bounded chunks.
MAX_OUTPUT_CHUNK_BYTES = 64 * 1024


class WireProtocolError(ControlProtocolError):
    pass


WireMessage = (
    ExecRequest
    | ExecResponse
    | ExecOutputChunk
    | ExecComplete
    | ControlError
    | ControllerReady
    | FinalizeRequest
    | FinalizeComplete
)

#: The longest a ControlError's human message may be. The code is what a
#: caller switches on; the message is detail, and an unbounded one would let a
#: peer spend the frame budget on prose.
MAX_ERROR_MESSAGE_BYTES = 4096


def _encode_json(message: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            message,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (binascii.Error, TypeError, ValueError) as exc:
        raise WireProtocolError("Control message is not JSON-serializable.") from exc

    if len(payload) > MAX_FRAME_BYTES:
        raise WireProtocolError("Control frame exceeds the maximum size.")

    return payload


def _frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise WireProtocolError("Control frame exceeds the maximum size.")

    return struct.pack(">I", len(payload)) + payload


def encode_exec_request(request: ExecRequest) -> bytes:
    validate_exec_request(request)

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "exec",
            "request_id": request.request_id,
            "argv": list(request.argv),
            "cwd": request.cwd,
            "env": request.env,
            "timeout_sec": request.timeout_sec,
            "user": request.user,
        }
    )

    return _frame(payload)


def encode_exec_response(response: ExecResponse) -> bytes:
    if (
        isinstance(response.request_id, bool)
        or not isinstance(response.request_id, int)
        or response.request_id < 0
    ):
        raise WireProtocolError("Response request ID is invalid.")

    if isinstance(response.return_code, bool) or not isinstance(
        response.return_code, int
    ):
        raise WireProtocolError("Response return code must be an integer.")

    if not isinstance(response.stdout, bytes) or not isinstance(response.stderr, bytes):
        raise WireProtocolError("Response output must be bytes.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "exec_result",
            "request_id": response.request_id,
            "stdout_b64": base64.b64encode(response.stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(response.stderr).decode("ascii"),
            "return_code": response.return_code,
        }
    )

    return _frame(payload)


def encode_exec_output_chunk(chunk: ExecOutputChunk) -> bytes:
    if (
        isinstance(chunk.request_id, bool)
        or not isinstance(chunk.request_id, int)
        or chunk.request_id < 0
    ):
        raise WireProtocolError("Output chunk request ID is invalid.")

    if chunk.stream not in ("stdout", "stderr"):
        raise WireProtocolError("Output chunk stream is invalid.")

    if not isinstance(chunk.data, bytes):
        raise WireProtocolError("Output chunk data must be bytes.")

    if len(chunk.data) > MAX_OUTPUT_CHUNK_BYTES:
        raise WireProtocolError("Output chunk exceeds the maximum size.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "exec_output",
            "request_id": chunk.request_id,
            "stream": chunk.stream,
            "data_b64": base64.b64encode(chunk.data).decode("ascii"),
        }
    )

    return _frame(payload)


def encode_exec_complete(complete: ExecComplete) -> bytes:
    if (
        isinstance(complete.request_id, bool)
        or not isinstance(complete.request_id, int)
        or complete.request_id < 0
    ):
        raise WireProtocolError("Completion request ID is invalid.")

    if isinstance(complete.return_code, bool) or not isinstance(
        complete.return_code, int
    ):
        raise WireProtocolError("Completion return code must be an integer.")

    if not isinstance(complete.timed_out, bool):
        raise WireProtocolError("Completion timed_out must be a boolean.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "exec_complete",
            "request_id": complete.request_id,
            "return_code": complete.return_code,
            "timed_out": complete.timed_out,
        }
    )

    return _frame(payload)


def encode_controller_ready(ready: ControllerReady) -> bytes:
    if isinstance(ready.protocol_version, bool) or not isinstance(
        ready.protocol_version, int
    ):
        raise WireProtocolError("Controller protocol version must be an integer.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "controller_ready",
            "protocol_version": ready.protocol_version,
        }
    )

    return _frame(payload)


def encode_control_error(error: ControlError) -> bytes:
    if (
        isinstance(error.request_id, bool)
        or not isinstance(error.request_id, int)
        or error.request_id < 0
    ):
        raise WireProtocolError("Control error request ID is invalid.")

    if error.code not in CONTROL_ERROR_CODES:
        raise WireProtocolError("Control error code is not a known code.")

    if not isinstance(error.message, str):
        raise WireProtocolError("Control error message must be a string.")

    if len(error.message.encode("utf-8")) > MAX_ERROR_MESSAGE_BYTES:
        raise WireProtocolError("Control error message exceeds the maximum size.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "control_error",
            "request_id": error.request_id,
            "code": error.code,
            "message": error.message,
        }
    )

    return _frame(payload)


def encode_finalize_request(request: FinalizeRequest) -> bytes:
    if (
        isinstance(request.request_id, bool)
        or not isinstance(request.request_id, int)
        or request.request_id < 0
    ):
        raise WireProtocolError("Finalize request ID is invalid.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "finalize",
            "request_id": request.request_id,
        }
    )

    return _frame(payload)


def encode_finalize_complete(complete: FinalizeComplete) -> bytes:
    if (
        isinstance(complete.request_id, bool)
        or not isinstance(complete.request_id, int)
        or complete.request_id < 0
    ):
        raise WireProtocolError("Finalize completion request ID is invalid.")

    payload = _encode_json(
        {
            "v": PROTOCOL_VERSION,
            "type": "finalize_complete",
            "request_id": complete.request_id,
        }
    )

    return _frame(payload)


def _decode_payload(payload: bytes) -> WireMessage:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireProtocolError("Control frame contains invalid JSON.") from exc

    if not isinstance(raw, dict):
        raise WireProtocolError("Control message must be a JSON object.")

    if raw.get("v") != PROTOCOL_VERSION:
        raise WireProtocolError("Unsupported control protocol version.")

    message_type = raw.get("type")

    if message_type == "exec":
        expected = {
            "v",
            "type",
            "request_id",
            "argv",
            "cwd",
            "env",
            "timeout_sec",
            "user",
        }
        if set(raw) != expected:
            raise WireProtocolError("Exec message has unexpected fields.")

        argv = raw["argv"]
        if not isinstance(argv, list):
            raise WireProtocolError("Exec argv must be an array.")

        request = ExecRequest(
            request_id=raw["request_id"],
            argv=tuple(argv),
            cwd=raw["cwd"],
            env=raw["env"],
            timeout_sec=raw["timeout_sec"],
            user=raw["user"],
        )

        # Malformed data received from the wire is a wire-protocol failure,
        # even though the shared validator normally raises ControlProtocolError.
        try:
            return validate_exec_request(request)
        except (ControlProtocolError, TypeError) as exc:
            raise WireProtocolError("Exec request failed protocol validation.") from exc

    if message_type == "exec_result":
        expected = {
            "v",
            "type",
            "request_id",
            "stdout_b64",
            "stderr_b64",
            "return_code",
        }
        if set(raw) != expected:
            raise WireProtocolError("Exec result has unexpected fields.")

        try:
            stdout = base64.b64decode(raw["stdout_b64"], validate=True)
            stderr = base64.b64decode(raw["stderr_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise WireProtocolError("Exec result contains invalid base64.") from exc

        request_id = raw["request_id"]
        return_code = raw["return_code"]

        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Response request ID is invalid.")

        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise WireProtocolError("Response return code must be an integer.")

        return ExecResponse(
            request_id=request_id,
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
        )

    if message_type == "exec_output":
        expected = {
            "v",
            "type",
            "request_id",
            "stream",
            "data_b64",
        }
        if set(raw) != expected:
            raise WireProtocolError("Exec output message has unexpected fields.")

        request_id = raw["request_id"]
        stream = raw["stream"]

        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Output chunk request ID is invalid.")

        if stream not in ("stdout", "stderr"):
            raise WireProtocolError("Output chunk stream is invalid.")

        try:
            data = base64.b64decode(raw["data_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise WireProtocolError("Exec output contains invalid base64.") from exc

        if len(data) > MAX_OUTPUT_CHUNK_BYTES:
            raise WireProtocolError("Output chunk exceeds the maximum size.")

        return ExecOutputChunk(
            request_id=request_id,
            stream=stream,
            data=data,
        )

    if message_type == "exec_complete":
        expected = {
            "v",
            "type",
            "request_id",
            "return_code",
            "timed_out",
        }
        if set(raw) != expected:
            raise WireProtocolError("Exec completion has unexpected fields.")

        request_id = raw["request_id"]
        return_code = raw["return_code"]
        timed_out = raw["timed_out"]

        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Completion request ID is invalid.")

        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise WireProtocolError("Completion return code must be an integer.")

        # A real bool, not a truthy int: 1 and true would otherwise be the
        # same frame, and the two peers are different languages.
        if not isinstance(timed_out, bool):
            raise WireProtocolError("Completion timed_out must be a boolean.")

        return ExecComplete(
            request_id=request_id,
            return_code=return_code,
            timed_out=timed_out,
        )

    if message_type == "controller_ready":
        expected = {"v", "type", "protocol_version"}
        if set(raw) != expected:
            raise WireProtocolError("Controller ready has unexpected fields.")

        protocol_version = raw["protocol_version"]
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
            raise WireProtocolError("Controller protocol version must be an integer.")

        return ControllerReady(protocol_version=protocol_version)

    if message_type == "control_error":
        expected = {"v", "type", "request_id", "code", "message"}
        if set(raw) != expected:
            raise WireProtocolError("Control error has unexpected fields.")

        request_id = raw["request_id"]
        code = raw["code"]
        message = raw["message"]

        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Control error request ID is invalid.")

        # An unknown code is refused rather than passed through: a caller
        # switches on this value, and a code it has never seen would silently
        # fall through whatever branch it has for the ones it knows.
        if not isinstance(code, str) or code not in CONTROL_ERROR_CODES:
            raise WireProtocolError("Control error code is not a known code.")

        if not isinstance(message, str):
            raise WireProtocolError("Control error message must be a string.")

        if len(message.encode("utf-8")) > MAX_ERROR_MESSAGE_BYTES:
            raise WireProtocolError("Control error message exceeds the maximum size.")

        return ControlError(request_id=request_id, code=code, message=message)

    if message_type == "finalize":
        expected = {"v", "type", "request_id"}
        if set(raw) != expected:
            raise WireProtocolError("Finalize request has unexpected fields.")

        request_id = raw["request_id"]
        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Finalize request ID is invalid.")

        return FinalizeRequest(request_id=request_id)

    if message_type == "finalize_complete":
        expected = {"v", "type", "request_id"}
        if set(raw) != expected:
            raise WireProtocolError("Finalize completion has unexpected fields.")

        request_id = raw["request_id"]
        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise WireProtocolError("Finalize completion request ID is invalid.")

        return FinalizeComplete(request_id=request_id)

    raise WireProtocolError("Unknown control message type.")


@dataclass
class FrameDecoder:
    _buffer: bytearray = field(default_factory=bytearray)

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[WireMessage]:
        if not isinstance(data, bytes):
            raise TypeError("Wire input must be bytes.")

        self._buffer.extend(data)
        messages: list[WireMessage] = []

        while True:
            if len(self._buffer) < HEADER_SIZE:
                break

            frame_size = struct.unpack(">I", self._buffer[:HEADER_SIZE])[0]

            if frame_size > MAX_FRAME_BYTES:
                raise WireProtocolError("Control frame exceeds the maximum size.")

            total_size = HEADER_SIZE + frame_size
            if len(self._buffer) < total_size:
                break

            payload = bytes(self._buffer[HEADER_SIZE:total_size])
            del self._buffer[:total_size]

            messages.append(_decode_payload(payload))

        return messages
