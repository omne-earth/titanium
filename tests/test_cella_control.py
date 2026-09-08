from __future__ import annotations

import pytest

from titanium.environments.cella.control import (
    ControlProtocolError,
    ExecRequest,
    make_exec_request,
    validate_exec_request,
)


def test_command_becomes_explicit_bash_argv():
    request = make_exec_request(
        request_id=1,
        command="pytest -q && echo done",
        cwd="/app",
        env={"MODE": "test"},
        timeout_sec=30,
        user=1000,
    )

    assert request.argv == (
        "/bin/bash",
        "-c",
        "pytest -q && echo done",
    )


def test_request_preserves_exec_semantics():
    request = make_exec_request(
        request_id=7,
        command="python test.py",
        cwd="/work",
        env={"A": "1"},
        timeout_sec=15,
        user="alice",
    )

    assert request.request_id == 7
    assert request.cwd == "/work"
    assert request.env == {"A": "1"}
    assert request.timeout_sec == 15
    assert request.user == "alice"


def test_negative_request_id_is_refused():
    with pytest.raises(ControlProtocolError):
        make_exec_request(
            request_id=-1,
            command="true",
            cwd=None,
            env={},
            timeout_sec=None,
            user=0,
        )


def test_nonpositive_timeout_is_refused():
    with pytest.raises(ControlProtocolError):
        make_exec_request(
            request_id=1,
            command="true",
            cwd=None,
            env={},
            timeout_sec=0,
            user=0,
        )

def valid_request() -> ExecRequest:
    return ExecRequest(
        request_id=1,
        argv=("/bin/bash", "-c", "echo hello"),
        cwd="/app",
        env={"MODE": "test"},
        timeout_sec=30,
        user=1000,
    )


def test_bool_request_id_is_refused():
    request = valid_request()
    request = ExecRequest(
        request_id=True,
        argv=request.argv,
        cwd=request.cwd,
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


def test_empty_argv_is_refused():
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=(),
        cwd=request.cwd,
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


def test_nul_in_argv_is_refused():
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=("python", "bad\x00argument"),
        cwd=request.cwd,
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


def test_empty_cwd_is_refused():
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=request.argv,
        cwd="",
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


def test_nul_in_cwd_is_refused():
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=request.argv,
        cwd="/app\x00other",
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


@pytest.mark.parametrize(
    "env",
    [
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"NAME": "bad\x00value"},
    ],
)
def test_invalid_environment_is_refused(env):
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=request.argv,
        cwd=request.cwd,
        env=env,
        timeout_sec=request.timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


@pytest.mark.parametrize("timeout_sec", [0, -1, True])
def test_invalid_timeout_is_refused(timeout_sec):
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=request.argv,
        cwd=request.cwd,
        env=request.env,
        timeout_sec=timeout_sec,
        user=request.user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)


@pytest.mark.parametrize("user", [None, True, -1, "", "   "])
def test_invalid_user_is_refused(user):
    request = valid_request()
    request = ExecRequest(
        request_id=request.request_id,
        argv=request.argv,
        cwd=request.cwd,
        env=request.env,
        timeout_sec=request.timeout_sec,
        user=user,
    )

    with pytest.raises(ControlProtocolError):
        validate_exec_request(request)
