from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class FinalizationError(RuntimeError):
    """The trial cannot safely advance through finalization."""


class FinalizationPhase(StrEnum):
    RUNNING = "running"
    FINALIZING = "finalizing"
    FROZEN_FINAL = "frozen_final"
    VERIFIED = "verified"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class FinalizationState:
    phase: FinalizationPhase = FinalizationPhase.RUNNING


def _require_phase(
    state: FinalizationState,
    expected: FinalizationPhase,
    *,
    operation: str,
) -> None:
    if state.phase is not expected:
        raise FinalizationError(
            f"Cannot {operation} while finalization phase is "
            f"{state.phase.value!r}; expected {expected.value!r}."
        )


def begin_finalization(
    state: FinalizationState,
) -> FinalizationState:
    _require_phase(
        state,
        FinalizationPhase.RUNNING,
        operation="begin finalization",
    )

    state = replace(state, phase=FinalizationPhase.FINALIZING)
    return state


def mark_frozen_final(
    state: FinalizationState,
    *,
    durability_confirmed: bool,
    freeze_confirmed: bool,
) -> FinalizationState:
    _require_phase(
        state,
        FinalizationPhase.FINALIZING,
        operation="mark the guest final",
    )

    # BOTH facts must be true:
    #
    #   durability_confirmed
    #   freeze_confirmed

    if not durability_confirmed or not freeze_confirmed:
        raise FinalizationError(
            f"Cannot mark the guest final: durability_confirmed="
            f"{durability_confirmed}, freeze_confirmed={freeze_confirmed}; "
            f"both must be true."
        )
        # Finalizing --> Frozen
    return replace(state, phase=FinalizationPhase.FROZEN_FINAL)


def mark_verified(
    state: FinalizationState,
    *,
    verification_succeeded: bool,
) -> FinalizationState:
    _require_phase(
        state,
        FinalizationPhase.FROZEN_FINAL,
        operation="mark verification complete",
    )

    if not verification_succeeded:
        raise FinalizationError("Verification not succesful")

    # FROZEN_FINAL -> VERIFIED
    return replace(state, phase=FinalizationPhase.VERIFIED)


def mark_archived(
    state: FinalizationState,
    *,
    archive_succeeded: bool,
) -> FinalizationState:
    _require_phase(
        state,
        FinalizationPhase.VERIFIED,
        operation="mark the trial archived",
    )

    # VERIFIED -> ARCHIVED
    if not archive_succeeded:
        raise FinalizationError("Archive not succesful")

    return replace(state, phase=FinalizationPhase.ARCHIVED)


# Normal exec is allowed ONLY in RUNNING.
def assert_exec_allowed(
    state: FinalizationState,
) -> None:
    _require_phase(
        state,
        FinalizationPhase.RUNNING,
        operation="execute",
    )


def assert_thaw_allowed(
    state: FinalizationState,
) -> None:
    _require_phase(
        state,
        FinalizationPhase.RUNNING,
        operation="thaw",
    )
    # Titanium must not thaw once finalization has begun.
    # Generic Cella can still support freeze/thaw.
    # This is only Titanium's final-trial policy.
