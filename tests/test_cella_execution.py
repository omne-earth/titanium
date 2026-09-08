from __future__ import annotations

import pytest

from titanium.environments.cella.execution import (
    ExecutionContextError,
    resolve_exec_context,
)


def test_requested_cwd_overrides_task_workdir():
    context = resolve_exec_context(
        requested_cwd="/tmp/test",
        task_workdir="/workspace/task",
        merged_env={},
    )

    assert context.cwd == "/tmp/test"


def test_task_workdir_is_used_when_cwd_is_not_requested():
    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir="/workspace/task",
        merged_env={},
    )

    assert context.cwd == "/workspace/task"


def test_cwd_is_none_when_no_directory_is_configured():
    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir=None,
        merged_env={},
    )

    assert context.cwd is None


def test_explicit_merged_environment_is_preserved():
    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir="/workspace/task",
        merged_env={
            "MODE": "test",
            "DEBUG": "1",
        },
    )

    assert context.env == {
        "MODE": "test",
        "DEBUG": "1",
    }


def test_environment_is_copied():
    merged_env = {
        "MODE": "test",
    }

    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir=None,
        merged_env=merged_env,
    )

    merged_env["MODE"] = "changed"
    merged_env["NEW_VALUE"] = "later"

    assert context.env == {
        "MODE": "test",
    }


def test_none_environment_becomes_empty_environment():
    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir=None,
        merged_env=None,
    )

    assert context.env == {}


def test_host_environment_is_not_implicitly_inherited(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "CELLA_CONTROLLER_SECRET",
        "must-not-leak",
    )

    context = resolve_exec_context(
        requested_cwd=None,
        task_workdir=None,
        merged_env={
            "MODE": "test",
        },
    )

    assert context.env == {
        "MODE": "test",
    }

    assert "CELLA_CONTROLLER_SECRET" not in context.env


@pytest.mark.parametrize(
    "requested_cwd",
    [
        "",
        "bad\x00path",
    ],
)
def test_invalid_requested_cwd_is_refused(
    requested_cwd: str,
):
    with pytest.raises(ExecutionContextError):
        resolve_exec_context(
            requested_cwd=requested_cwd,
            task_workdir=None,
            merged_env={},
        )


@pytest.mark.parametrize(
    "task_workdir",
    [
        "",
        "bad\x00path",
    ],
)
def test_invalid_task_workdir_is_refused(
    task_workdir: str,
):
    with pytest.raises(ExecutionContextError):
        resolve_exec_context(
            requested_cwd=None,
            task_workdir=task_workdir,
            merged_env={},
        )


def test_non_string_cwd_is_refused():
    with pytest.raises(TypeError):
        resolve_exec_context(
            requested_cwd=123,  # type: ignore[arg-type]
            task_workdir=None,
            merged_env={},
        )


@pytest.mark.parametrize(
    "merged_env",
    [
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"GOOD": "bad\x00value"},
    ],
)
def test_invalid_environment_is_refused(
    merged_env: dict[str, str],
):
    with pytest.raises(ExecutionContextError):
        resolve_exec_context(
            requested_cwd=None,
            task_workdir=None,
            merged_env=merged_env,
        )


def test_non_string_environment_value_is_refused():
    with pytest.raises(ExecutionContextError):
        resolve_exec_context(
            requested_cwd=None,
            task_workdir=None,
            merged_env={
                "COUNT": 5,  # type: ignore[dict-item]
            },
        )
