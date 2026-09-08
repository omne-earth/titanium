"""The host and the guest must agree on bytes, not on intentions.

Both sides of the control protocol are implemented independently -- Python in
``environments/cella/wire.py``, Rust in ``native/titanium-controller`` -- so
duplicated constants prove nothing. These tests drive the real Rust codec as a
subprocess and compare actual frames.

Nothing here downloads a crate or a toolchain: the controller has no
dependencies and the build runs ``--offline``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from titanium.environments.cella.control import (
    ControlError,
    ControllerReady,
    ExecComplete,
    ExecOutputChunk,
    ExecRequest,
    FinalizeComplete,
    FinalizeRequest,
)
from titanium.environments.cella.wire import (
    FrameDecoder,
    encode_control_error,
    encode_controller_ready,
    encode_exec_complete,
    encode_exec_output_chunk,
    encode_exec_request,
    encode_finalize_complete,
    encode_finalize_request,
)

CRATE = Path(__file__).resolve().parent.parent / "native" / "titanium-controller"

# Byte-for-byte the vectors `protocol-codec emit` produces, in that order.
CANONICAL_GUEST_MESSAGES = [
    ControllerReady(protocol_version=1),
    ExecOutputChunk(
        request_id=7,
        stream="stdout",
        data=bytes([0x00, 0x01, 0xFF, 0xFE, 0xE2, 0x98, 0x83, 0x0A]),
    ),
    ExecOutputChunk(request_id=7, stream="stderr", data=b"warning\n"),
    ExecComplete(request_id=7, return_code=0, timed_out=False),
    ExecComplete(request_id=8, return_code=137, timed_out=True),
    ControlError(
        request_id=9,
        code="cwd_unavailable",
        message='no such directory: /nope "quoted" \\ backslash',
    ),
    FinalizeComplete(request_id=10),
]


def encode_guest(message) -> bytes:
    if isinstance(message, ControllerReady):
        return encode_controller_ready(message)
    if isinstance(message, ExecOutputChunk):
        return encode_exec_output_chunk(message)
    if isinstance(message, ExecComplete):
        return encode_exec_complete(message)
    if isinstance(message, ControlError):
        return encode_control_error(message)
    if isinstance(message, FinalizeComplete):
        return encode_finalize_complete(message)
    raise AssertionError(f"no encoder for {type(message).__name__}")


def _build_reason() -> str | None:
    if shutil.which("cargo") is None:
        return "cargo is not on PATH"
    if not CRATE.is_dir():
        return f"the controller crate is absent at {CRATE}"
    built = subprocess.run(
        ["cargo", "build", "--offline", "-q", "--bin", "protocol-codec"],
        cwd=CRATE,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        return (
            f"the controller crate does not build offline: {built.stderr.strip()[:400]}"
        )
    return None


@pytest.fixture(scope="module")
def codec() -> Path:
    reason = _build_reason()
    if reason is not None:
        pytest.skip(f"cross-language protocol test not runnable: {reason}")
    return CRATE / "target" / "debug" / "protocol-codec"


def run_codec(codec: Path, mode: str, frames: list[bytes] | None = None) -> list[str]:
    payload = "" if frames is None else "".join(f"{frame.hex()}\n" for frame in frames)
    completed = subprocess.run(
        [str(codec), mode],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def decode_all(data: bytes) -> list:
    return FrameDecoder().feed(data)


# ------------------------------------------------- Python encode -> Rust decode


def test_rust_decodes_and_reproduces_every_python_guest_frame(codec):
    """Round-tripping through Rust must be a fixed point on the bytes."""
    frames = [encode_guest(message) for message in CANONICAL_GUEST_MESSAGES]
    returned = run_codec(codec, "roundtrip", frames)

    assert len(returned) == len(frames)
    for original, echoed, message in zip(
        frames, returned, CANONICAL_GUEST_MESSAGES, strict=True
    ):
        assert echoed == original.hex(), f"Rust altered the bytes of {message}"


def test_rust_decodes_the_python_exec_request(codec):
    request = ExecRequest(
        request_id=42,
        argv=("/bin/bash", "-c", "echo hi"),
        cwd="/srv/app",
        env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
        timeout_sec=30,
        user=1000,
    )
    (described,) = run_codec(codec, "describe", [encode_exec_request(request)])
    assert described == (
        "exec id=42 argv=/bin/bash\x1f-c\x1fecho hi cwd=/srv/app "
        "env=LANG=C.UTF-8\x1fPATH=/usr/bin timeout=30 user=uid:1000"
    )


def test_rust_decodes_a_named_user_and_absent_cwd_and_timeout(codec):
    request = ExecRequest(
        request_id=1,
        argv=("/bin/true",),
        cwd=None,
        env={},
        timeout_sec=None,
        user="titanium",
    )
    (described,) = run_codec(codec, "describe", [encode_exec_request(request)])
    assert described == (
        "exec id=1 argv=/bin/true cwd=<none> env= timeout=<none> user=name:titanium"
    )


def test_rust_decodes_the_python_finalize_request(codec):
    (described,) = run_codec(
        codec, "describe", [encode_finalize_request(FinalizeRequest(request_id=5))]
    )
    assert described == "finalize id=5"


# ------------------------------------------------- Rust encode -> Python decode


def test_python_decodes_every_canonical_rust_frame(codec):
    frames = [bytes.fromhex(line) for line in run_codec(codec, "emit")]
    decoded = decode_all(b"".join(frames))
    assert decoded == CANONICAL_GUEST_MESSAGES


def test_binary_output_survives_the_crossing_byte_perfect(codec):
    """NUL, a lone 0xff, and multi-byte UTF-8 in one chunk."""
    frames = [bytes.fromhex(line) for line in run_codec(codec, "emit")]
    chunks = [m for m in decode_all(b"".join(frames)) if isinstance(m, ExecOutputChunk)]
    assert chunks[0].data == bytes([0x00, 0x01, 0xFF, 0xFE, 0xE2, 0x98, 0x83, 0x0A])
    assert chunks[0].stream == "stdout"
    assert chunks[1].data == b"warning\n"
    assert chunks[1].stream == "stderr"


def test_the_timed_out_flag_crosses_in_both_states(codec):
    frames = [bytes.fromhex(line) for line in run_codec(codec, "emit")]
    completes = [m for m in decode_all(b"".join(frames)) if isinstance(m, ExecComplete)]
    assert (completes[0].return_code, completes[0].timed_out) == (0, False)
    assert (completes[1].return_code, completes[1].timed_out) == (137, True)


def test_a_control_error_keeps_its_code_and_escaped_message(codec):
    frames = [bytes.fromhex(line) for line in run_codec(codec, "emit")]
    (error,) = [m for m in decode_all(b"".join(frames)) if isinstance(m, ControlError)]
    assert error.code == "cwd_unavailable"
    assert error.message == 'no such directory: /nope "quoted" \\ backslash'


# ------------------------------------------------------------------ refusals


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"{not json", "malformed JSON"),
        (b"[]", "not an object"),
        (
            b'{"v":2,"type":"exec_complete","request_id":1,"return_code":0,"timed_out":false}',
            "wrong version",
        ),
        (b'{"v":1,"type":"nonsense","request_id":1}', "unknown type"),
        (
            b'{"v":1,"type":"exec_complete","request_id":1,"return_code":0}',
            "missing timed_out",
        ),
        (
            b'{"v":1,"type":"exec_complete","request_id":1,"return_code":0,"timed_out":false,"x":1}',
            "extra field",
        ),
        (
            b'{"v":1,"type":"exec_complete","request_id":-1,"return_code":0,"timed_out":false}',
            "negative request id",
        ),
        (
            b'{"v":1,"type":"exec_complete","request_id":1,"return_code":0,"timed_out":1}',
            "timed_out is not a bool",
        ),
        (
            b'{"v":1,"type":"exec_output","request_id":1,"stream":"stdout","data_b64":"!!!!"}',
            "invalid base64",
        ),
        (
            b'{"v":1,"type":"exec_output","request_id":1,"stream":"other","data_b64":""}',
            "bad stream",
        ),
        (
            b'{"v":1,"type":"control_error","request_id":1,"code":"made_up","message":"x"}',
            "unknown error code",
        ),
    ],
)
def test_rust_refuses_what_python_refuses(codec, payload, why):
    (result,) = run_codec(codec, "roundtrip", [_frame(payload)])
    assert result.startswith("ERROR"), f"Rust accepted {why}: {result}"

    # And Python refuses the same bytes.
    from titanium.environments.cella.wire import WireProtocolError

    with pytest.raises(WireProtocolError):
        decode_all(_frame(payload))


def test_direction_is_enforced_at_the_layer_that_owns_the_conversation(codec):
    """Rust refuses at decode; Python refuses at the client, not the codec.

    The guest has one correct answer for a frame it would itself emit, so its
    decoder rejects it outright. On the host, ``FrameDecoder`` is a codec and
    stays direction-agnostic -- it is what the tests themselves use to read
    both directions -- and ``CellaControlClient`` is where an unexpected
    message type invalidates the connection.
    """
    exec_frame = encode_exec_request(
        ExecRequest(
            request_id=1,
            argv=("/bin/true",),
            cwd=None,
            env={},
            timeout_sec=None,
            user=1000,
        )
    )
    (result,) = run_codec(codec, "roundtrip", [exec_frame])
    assert result.startswith("ERROR")
    assert "host-direction" in result

    # The host codec decodes it; refusing it is the client's job, which
    # tests/test_cella_client.py and test_cella_control_hardening.py cover.
    (decoded,) = decode_all(exec_frame)
    assert isinstance(decoded, ExecRequest)


def test_rust_refuses_a_guest_message_arriving_from_the_host(codec):
    ready = encode_controller_ready(ControllerReady(protocol_version=1))
    (result,) = run_codec(codec, "describe", [ready])
    assert result.startswith("ERROR")
    assert "guest-direction" in result


def test_rust_refuses_an_oversized_output_chunk(codec):
    import base64 as b64

    oversized = b64.b64encode(b"x" * (64 * 1024 + 1)).decode()
    payload = (
        b'{"v":1,"type":"exec_output","request_id":1,"stream":"stdout","data_b64":"'
        + oversized.encode()
        + b'"}'
    )
    (result,) = run_codec(codec, "roundtrip", [_frame(payload)])
    assert result.startswith("ERROR")
