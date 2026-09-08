from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class ControlProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ExecRequest:
    request_id: int
    argv: tuple[str, ...]
    cwd: str | None
    env: dict[str, str]
    timeout_sec: int | None
    user: str | int


@dataclass(frozen=True)
class ExecResponse:
    request_id: int
    stdout: bytes
    stderr: bytes
    return_code: int


# The bounded messages that cross the wire before the final assembled
# ExecResponse exists. Splitting output into chunks is what keeps a command's
# total output from having to fit in one frame.
@dataclass(frozen=True)
class ExecOutputChunk:
    request_id: int
    stream: Literal["stdout", "stderr"]
    data: bytes


@dataclass(frozen=True)
class ExecComplete:
    """The terminal frame of a request whose program actually started.

    ``timed_out`` distinguishes the two ways a started program ends. It is not
    an error: the request was understood, the process ran, and the deadline is
    part of the outcome. A request that could never start reports
    :class:`ControlError` instead, so a caller can tell "your program exited 1"
    from "no program ever ran".
    """

    request_id: int
    return_code: int
    timed_out: bool = False


#: Stable, machine-readable reasons a valid request could not be carried out.
#: Callers switch on these; the human message carries the detail.
CONTROL_ERROR_CODES = frozenset(
    {
        "invalid_identity",
        "cwd_unavailable",
        "spawn_failed",
        "finalized",
        "finalization_failed",
    }
)


@dataclass(frozen=True)
class ControlError:
    """A valid, understood request that could not be carried out.

    Deliberately not used for malformed framing or ambiguous wire state: those
    leave the connection untrustworthy and poison it instead. This frame means
    the peer understood the request and is reporting, in band, that it
    declined or failed to perform it.
    """

    request_id: int
    code: str
    message: str


@dataclass(frozen=True)
class ControllerReady:
    """The guest controller's first frame, sent once when it can serve.

    The control socket exists as soon as the VMM starts, which is well before
    systemd has started the controller and before the protocol tty has been
    put into raw mode. Without an explicit readiness frame a host request
    could be written into a tty that still has canonical processing and echo
    on, where it would be mangled and partially echoed back.
    """

    protocol_version: int


@dataclass(frozen=True)
class FinalizeRequest:
    """Ends ordinary execution and asks the guest to make the disk durable.

    Carries a request id so it participates in the same one-outstanding-
    operation correlation model as an exec.
    """

    request_id: int


@dataclass(frozen=True)
class FinalizeComplete:
    """Quiescence and syncfs both succeeded. The host may now freeze."""

    request_id: int


def make_exec_request(
    *,
    request_id: int,
    command: str,
    cwd: str | None,
    env: dict[str, str],
    timeout_sec: int | None,
    user: str | int,
) -> ExecRequest:
    if request_id < 0:
        raise ControlProtocolError("Request ID cannot be negative.")

    if not command:
        raise ControlProtocolError("Command cannot be empty.")

    if timeout_sec is not None and timeout_sec <= 0:
        raise ControlProtocolError("Timeout must be positive.")

    request = ExecRequest(
        request_id=request_id,
        argv=("/bin/bash", "-c", command),
        cwd=cwd,
        env=dict(env),
        timeout_sec=timeout_sec,
        user=user,
    )

    return validate_exec_request(request)


def validate_exec_request(request: ExecRequest) -> ExecRequest:
    """Validate an exec request before it crosses into the guest."""

    # request_id must be an int, but bool must NOT count as an int.
    # It must also be >= 0.
    if not (
        isinstance(request.request_id, int)
        and not isinstance(request.request_id, bool)
        and request.request_id >= 0
    ):
        raise ControlProtocolError("Request_id must be pure int, and above 0.")

    # argv must not be empty.
    # Every argv element must be a string.
    # No argv element may contain a NUL byte.
    if not request.argv:
        raise ControlProtocolError("Argv cannot be empty.")

    for arg in request.argv:
        if not isinstance(arg, str):
            raise ControlProtocolError("Every argv element must be a string.")

        if "\x00" in arg:
            raise ControlProtocolError("Argv cannot contain NUL bytes.")

    # cwd may be None.
    # If present, it must be a non-empty string.
    # It must not contain a NUL byte.
    if request.cwd is not None:
        if not isinstance(request.cwd, str) or not request.cwd:
            raise ControlProtocolError("Cwd must be a non-empty string.")

        if "\x00" in request.cwd:
            raise ControlProtocolError("Cwd cannot contain NUL bytes.")

    # env must contain only string keys and string values.
    # Keys must not be empty.
    # Keys mustter not contain "=" or NUL.
    # Values must not contain NUL.
    if not isinstance(request.env, dict):
        raise ControlProtocolError("Environment must be a dictionary.")

    for key, value in request.env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ControlProtocolError("Environment keys and values must be strings.")

        if not key or "=" in key or "\x00" in key:
            raise ControlProtocolError("Environment variable name is invalid.")

        if "\x00" in value:
            raise ControlProtocolError("Environment value cannot contain NUL bytes.")

    # timeout_sec may be None.
    # Otherwise it must be an int, not bool, and > 0.
    if request.timeout_sec is not None and (
        isinstance(request.timeout_sec, bool)
        or not isinstance(request.timeout_sec, int)
        or request.timeout_sec <= 0
    ):
        raise ControlProtocolError("Timeout must be a positive integer.")

    # The identity must already be resolved; None is not a valid value here.
    # Valid forms are:
    #   non-empty string
    #   non-negative int
    # bool must be rejected.
    if isinstance(request.user, bool) or not isinstance(request.user, (str, int)):
        raise ControlProtocolError("User must be a username or numeric UID.")

    if isinstance(request.user, str) and not request.user.strip():
        raise ControlProtocolError("User cannot be empty.")

    if isinstance(request.user, int) and request.user < 0:
        raise ControlProtocolError("UID cannot be negative.")

    return request
