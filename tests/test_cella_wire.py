from __future__ import annotations

import struct

import pytest

from titanium.environments.cella.control import (
    ExecComplete,
    ExecOutputChunk,
    ExecRequest,
    ExecResponse,
)
from titanium.environments.cella.wire import (
    MAX_FRAME_BYTES,
    MAX_OUTPUT_CHUNK_BYTES,
    FrameDecoder,
    WireProtocolError,
    encode_exec_complete,
    encode_exec_output_chunk,
    encode_exec_request,
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


def test_exec_request_round_trip():
    decoder = FrameDecoder()

    messages = decoder.feed(encode_exec_request(sample_request()))

    assert messages == [sample_request()]


def test_exec_response_round_trip_preserves_raw_bytes():
    response = ExecResponse(
        request_id=7,
        stdout=b"hello\xff",
        stderr=b"\x00warning",
        return_code=3,
    )

    decoder = FrameDecoder()
    messages = decoder.feed(encode_exec_response(response))

    assert messages == [response]


def test_partial_frame_waits_for_remaining_bytes():
    frame = encode_exec_request(sample_request())
    decoder = FrameDecoder()

    assert decoder.feed(frame[:2]) == []
    assert decoder.pending_bytes == 2

    assert decoder.feed(frame[2:9]) == []
    assert decoder.pending_bytes > 0

    assert decoder.feed(frame[9:]) == [sample_request()]
    assert decoder.pending_bytes == 0


def test_multiple_frames_can_arrive_together():
    request = sample_request()

    response = ExecResponse(
        request_id=7,
        stdout=b"done",
        stderr=b"",
        return_code=0,
    )

    decoder = FrameDecoder()

    messages = decoder.feed(
        encode_exec_request(request) + encode_exec_response(response)
    )

    assert messages == [request, response]


def test_oversized_claim_is_refused_before_payload_arrives():
    decoder = FrameDecoder()

    header = struct.pack(">I", MAX_FRAME_BYTES + 1)

    with pytest.raises(WireProtocolError):
        decoder.feed(header)


def test_invalid_json_is_refused():
    payload = b"{definitely-not-json}"
    frame = struct.pack(">I", len(payload)) + payload

    decoder = FrameDecoder()

    with pytest.raises(WireProtocolError):
        decoder.feed(frame)


def test_stdout_chunk_round_trip_preserves_binary_bytes():
    chunk = ExecOutputChunk(
        request_id=7,
        stream="stdout",
        data=b"hello\x00\xffbinary",
    )

    decoder = FrameDecoder()

    assert decoder.feed(encode_exec_output_chunk(chunk)) == [chunk]


def test_stderr_chunk_round_trip():
    chunk = ExecOutputChunk(
        request_id=7,
        stream="stderr",
        data=b"warning\n",
    )

    decoder = FrameDecoder()

    assert decoder.feed(encode_exec_output_chunk(chunk)) == [chunk]


def test_exec_complete_round_trip():
    complete = ExecComplete(
        request_id=7,
        return_code=42,
    )

    decoder = FrameDecoder()

    assert decoder.feed(encode_exec_complete(complete)) == [complete]


def test_output_chunk_larger_than_limit_is_refused():
    chunk = ExecOutputChunk(
        request_id=7,
        stream="stdout",
        data=b"x" * (MAX_OUTPUT_CHUNK_BYTES + 1),
    )

    with pytest.raises(
        WireProtocolError,
        match="maximum size",
    ):
        encode_exec_output_chunk(chunk)


def test_invalid_output_stream_is_refused():
    chunk = ExecOutputChunk(
        request_id=7,
        stream="not-a-stream",  # type: ignore[arg-type]
        data=b"hello",
    )

    with pytest.raises(
        WireProtocolError,
        match="stream",
    ):
        encode_exec_output_chunk(chunk)


def test_invalid_output_base64_is_refused():
    payload = (
        b'{"v":1,"type":"exec_output",'
        b'"request_id":7,'
        b'"stream":"stdout",'
        b'"data_b64":"%%%NOT-BASE64%%%"}'
    )

    frame = struct.pack(">I", len(payload)) + payload

    with pytest.raises(
        WireProtocolError,
        match="base64",
    ):
        FrameDecoder().feed(frame)


def test_decoder_exposes_partial_trailing_bytes():
    complete = ExecComplete(
        request_id=7,
        return_code=0,
    )

    frame = encode_exec_complete(complete)

    decoder = FrameDecoder()

    assert decoder.feed(frame + b"\x00\x00") == [complete]
    assert decoder.pending_bytes == 2
