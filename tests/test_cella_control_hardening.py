from __future__ import annotations

import queue
import socket
import struct
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
    ExecComplete,
    ExecOutputChunk,
    ExecRequest,
)
from titanium.environments.cella.wire import (
    FrameDecoder,
    encode_controller_ready,
    encode_exec_complete,
    encode_exec_output_chunk,
)


def sample_request() -> ExecRequest:
    return ExecRequest(
        request_id=42,
        argv=(
            "/bin/bash",
            "-c",
            "printf hello",
        ),
        cwd="/workspace/task",
        env={"MODE": "test"},
        timeout_sec=30,
        user=1000,
    )


def _recv_one(
    conn: socket.socket,
) -> ExecRequest:
    decoder = FrameDecoder()

    while True:
        data = conn.recv(4096)

        if not data:
            raise AssertionError("Client disconnected before sending request.")

        messages = decoder.feed(data)

        if not messages:
            continue

        assert len(messages) == 1

        message = messages[0]

        assert isinstance(
            message,
            ExecRequest,
        )

        return message


def _start_peer(
    path: Path,
    handler: Callable[[socket.socket], None],
) -> tuple[
    threading.Thread,
    queue.Queue[Exception],
]:
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


def test_streamed_stdout_and_stderr_are_assembled(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stdout",
                    data=b"hello ",
                )
            )
            + encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stderr",
                    data=b"warning\n",
                )
            )
            + encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stdout",
                    data=b"world\n",
                )
            )
            + encode_exec_complete(
                ExecComplete(
                    request_id=42,
                    return_code=3,
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
        response = client.execute(
            request,
            response_timeout_sec=1,
        )

        assert response.stdout == b"hello world\n"
        assert response.stderr == b"warning\n"
        assert response.return_code == 3
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_binary_streamed_output_survives(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stdout",
                    data=b"\x00\xffbinary",
                )
            )
            + encode_exec_complete(
                ExecComplete(
                    request_id=42,
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
        response = client.execute(
            request,
            response_timeout_sec=1,
        )

        assert response.stdout == b"\x00\xffbinary"
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_streamed_response_survives_split_socket_reads(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        wire = encode_exec_output_chunk(
            ExecOutputChunk(
                request_id=42,
                stream="stdout",
                data=b"split-response\n",
            )
        ) + encode_exec_complete(
            ExecComplete(
                request_id=42,
                return_code=0,
            )
        )

        conn.sendall(wire[:2])
        conn.sendall(wire[2:9])
        conn.sendall(wire[9:31])
        conn.sendall(wire[31:])

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    try:
        response = client.execute(
            request,
            response_timeout_sec=1,
        )

        assert response.stdout == b"split-response\n"
        assert response.return_code == 0
    finally:
        client.close()

    _finish_peer(thread, errors)


def test_wrong_request_id_poisons_connection(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_complete(
                ExecComplete(
                    request_id=999,
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

    with pytest.raises(
        ControlClientError,
        match="request ID",
    ):
        client.execute(
            request,
            response_timeout_sec=1,
        )

    with pytest.raises(
        ControlClientError,
        match="not connected",
    ):
        client.execute(request)

    _finish_peer(thread, errors)


def test_malformed_wire_message_poisons_connection(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        payload = b"{not-json}"

        conn.sendall(
            struct.pack(
                ">I",
                len(payload),
            )
            + payload
        )

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    with pytest.raises(
        ControlClientError,
        match="invalid wire",
    ):
        client.execute(
            request,
            response_timeout_sec=1,
        )

    with pytest.raises(
        ControlClientError,
        match="not connected",
    ):
        client.execute(request)

    _finish_peer(thread, errors)


def test_timeout_poisons_connection(
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

    with pytest.raises(
        ControlClientError,
        match="Timed out",
    ):
        client.execute(
            request,
            response_timeout_sec=0.05,
        )

    with pytest.raises(
        ControlClientError,
        match="not connected",
    ):
        client.execute(request)

    _finish_peer(thread, errors)


def test_bytes_after_completion_poison_connection(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_complete(
                ExecComplete(
                    request_id=42,
                    return_code=0,
                )
            )
            + b"\x00\x00"
        )

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    with pytest.raises(
        ControlClientError,
        match="after exec completion",
    ):
        client.execute(
            request,
            response_timeout_sec=1,
        )

    with pytest.raises(
        ControlClientError,
        match="not connected",
    ):
        client.execute(request)

    _finish_peer(thread, errors)


def test_total_output_budget_is_enforced(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stdout",
                    data=b"123456",
                )
            )
        )

    thread, errors = _start_peer(
        socket_path,
        peer,
    )

    client = CellaControlClient(
        socket_path,
        max_output_bytes=5,
    )
    client.connect()

    with pytest.raises(
        ControlClientError,
        match="configured limit",
    ):
        client.execute(
            request,
            response_timeout_sec=1,
        )

    with pytest.raises(
        ControlClientError,
        match="not connected",
    ):
        client.execute(request)

    _finish_peer(thread, errors)


def test_clean_reconnect_after_poisoned_connection(
    tmp_path: Path,
):
    socket_path = tmp_path / "control.sock"
    request = sample_request()

    def bad_peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_complete(
                ExecComplete(
                    request_id=999,
                    return_code=0,
                )
            )
        )

    thread, errors = _start_peer(
        socket_path,
        bad_peer,
    )

    client = CellaControlClient(socket_path)
    client.connect()

    with pytest.raises(
        ControlClientError,
        match="request ID",
    ):
        client.execute(
            request,
            response_timeout_sec=1,
        )

    _finish_peer(thread, errors)

    socket_path.unlink()

    def good_peer(conn: socket.socket) -> None:
        assert _recv_one(conn) == request

        conn.sendall(
            encode_exec_output_chunk(
                ExecOutputChunk(
                    request_id=42,
                    stream="stdout",
                    data=b"fresh connection\n",
                )
            )
            + encode_exec_complete(
                ExecComplete(
                    request_id=42,
                    return_code=0,
                )
            )
        )

    thread, errors = _start_peer(
        socket_path,
        good_peer,
    )

    client.connect()

    try:
        response = client.execute(
            request,
            response_timeout_sec=1,
        )

        assert response.stdout == b"fresh connection\n"
        assert response.return_code == 0
    finally:
        client.close()

    _finish_peer(thread, errors)
