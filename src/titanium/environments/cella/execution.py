from __future__ import annotations

from dataclasses import dataclass


class ExecutionContextError(ValueError):
    """The requested Cella process context is invalid."""


@dataclass(frozen=True)
class ExecContext:
    """Resolved cwd and environment for one Cella exec request."""

    cwd: str | None
    env: dict[str, str]


def _validate_cwd_value(
    value: str | None,
    *,
    label: str,
) -> None:
    if value is None:
        return

    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string or None.")

    if not value:
        raise ExecutionContextError(f"{label} cannot be empty.")

    if "\x00" in value:
        raise ExecutionContextError(f"{label} cannot contain NUL bytes.")


def _copy_and_validate_env(
    env: dict[str, str] | None,
) -> dict[str, str]:
    if env is None:
        return {}

    if not isinstance(env, dict):
        raise TypeError("Merged environment must be a dictionary or None.")

    result = dict(env)

    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ExecutionContextError("Environment keys and values must be strings.")

        if not key or "=" in key or "\x00" in key:
            raise ExecutionContextError("Environment variable name is invalid.")

        if "\x00" in value:
            raise ExecutionContextError("Environment value cannot contain NUL bytes.")

    return result


def resolve_exec_context(
    *,
    requested_cwd: str | None,
    task_workdir: str | None,
    merged_env: dict[str, str] | None,
) -> ExecContext:
    """
    Resolve Titanium's existing cwd/env semantics for a Cella exec request.

    `merged_env` must already come from BaseEnvironment._merge_env().
    """

    _validate_cwd_value(
        requested_cwd,
        label="Requested cwd",
    )

    _validate_cwd_value(
        task_workdir,
        label="Task workdir",
    )

    resolved_env = _copy_and_validate_env(merged_env)

    effective_cwd = requested_cwd if requested_cwd is not None else task_workdir

    # Future guest-side cwd protection:
    # resolve cwd once with openat2(RESOLVE_NO_MAGICLINKS), require a
    # directory FD, then fchdir(fd). Fail rather than silently falling back.
    # Ordinary symlinks and normal ".." paths remain supported.

    return ExecContext(
        cwd=effective_cwd,
        env=resolved_env,
    )
