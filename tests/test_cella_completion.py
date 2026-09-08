from __future__ import annotations

import pytest

from titanium.environments.cella.completion import (
    FinalizationError,
    FinalizationPhase,
    FinalizationState,
    assert_exec_allowed,
    assert_thaw_allowed,
    begin_finalization,
    mark_archived,
    mark_frozen_final,
    mark_verified,
)


def test_initial_state_is_running():
    state = FinalizationState()

    assert state.phase is FinalizationPhase.RUNNING


def test_begin_finalization_moves_running_to_finalizing():
    original = FinalizationState()

    finalizing = begin_finalization(original)

    assert original.phase is FinalizationPhase.RUNNING
    assert finalizing.phase is FinalizationPhase.FINALIZING


def test_begin_finalization_refuses_wrong_phase():
    state = FinalizationState(
        phase=FinalizationPhase.FINALIZING,
    )

    with pytest.raises(
        FinalizationError,
        match="expected 'running'",
    ):
        begin_finalization(state)


@pytest.mark.parametrize(
    ("durability_confirmed", "freeze_confirmed"),
    [
        (False, False),
        (False, True),
        (True, False),
    ],
)
def test_frozen_final_requires_both_durability_and_freeze(
    durability_confirmed: bool,
    freeze_confirmed: bool,
):
    state = FinalizationState(
        phase=FinalizationPhase.FINALIZING,
    )

    with pytest.raises(
        FinalizationError,
        match="both must be true",
    ):
        mark_frozen_final(
            state,
            durability_confirmed=durability_confirmed,
            freeze_confirmed=freeze_confirmed,
        )


def test_frozen_final_succeeds_when_both_are_confirmed():
    state = FinalizationState(
        phase=FinalizationPhase.FINALIZING,
    )

    frozen = mark_frozen_final(
        state,
        durability_confirmed=True,
        freeze_confirmed=True,
    )

    assert frozen.phase is FinalizationPhase.FROZEN_FINAL


def test_cannot_mark_running_trial_frozen_final():
    state = FinalizationState()

    with pytest.raises(
        FinalizationError,
        match="expected 'finalizing'",
    ):
        mark_frozen_final(
            state,
            durability_confirmed=True,
            freeze_confirmed=True,
        )


def test_verification_success_moves_frozen_final_to_verified():
    state = FinalizationState(
        phase=FinalizationPhase.FROZEN_FINAL,
    )

    verified = mark_verified(
        state,
        verification_succeeded=True,
    )

    assert verified.phase is FinalizationPhase.VERIFIED


def test_verification_failure_does_not_advance():
    state = FinalizationState(
        phase=FinalizationPhase.FROZEN_FINAL,
    )

    with pytest.raises(
        FinalizationError,
        match="Verification",
    ):
        mark_verified(
            state,
            verification_succeeded=False,
        )

    assert state.phase is FinalizationPhase.FROZEN_FINAL


def test_archive_success_moves_verified_to_archived():
    state = FinalizationState(
        phase=FinalizationPhase.VERIFIED,
    )

    archived = mark_archived(
        state,
        archive_succeeded=True,
    )

    assert archived.phase is FinalizationPhase.ARCHIVED


def test_archive_failure_does_not_advance():
    state = FinalizationState(
        phase=FinalizationPhase.VERIFIED,
    )

    with pytest.raises(
        FinalizationError,
        match="Archive",
    ):
        mark_archived(
            state,
            archive_succeeded=False,
        )

    assert state.phase is FinalizationPhase.VERIFIED


@pytest.mark.parametrize(
    "phase",
    [
        FinalizationPhase.FINALIZING,
        FinalizationPhase.FROZEN_FINAL,
        FinalizationPhase.VERIFIED,
        FinalizationPhase.ARCHIVED,
    ],
)
def test_exec_is_refused_after_finalization_begins(
    phase: FinalizationPhase,
):
    state = FinalizationState(phase=phase)

    with pytest.raises(
        FinalizationError,
        match="execute",
    ):
        assert_exec_allowed(state)


def test_exec_is_allowed_while_running():
    assert_exec_allowed(
        FinalizationState(
            phase=FinalizationPhase.RUNNING,
        )
    )


@pytest.mark.parametrize(
    "phase",
    [
        FinalizationPhase.FINALIZING,
        FinalizationPhase.FROZEN_FINAL,
        FinalizationPhase.VERIFIED,
        FinalizationPhase.ARCHIVED,
    ],
)
def test_thaw_is_refused_after_finalization_begins(
    phase: FinalizationPhase,
):
    state = FinalizationState(phase=phase)

    with pytest.raises(
        FinalizationError,
        match="thaw",
    ):
        assert_thaw_allowed(state)


def test_finalization_happy_path():
    state = FinalizationState()

    state = begin_finalization(state)
    assert state.phase is FinalizationPhase.FINALIZING

    state = mark_frozen_final(
        state,
        durability_confirmed=True,
        freeze_confirmed=True,
    )
    assert state.phase is FinalizationPhase.FROZEN_FINAL

    state = mark_verified(
        state,
        verification_succeeded=True,
    )
    assert state.phase is FinalizationPhase.VERIFIED

    state = mark_archived(
        state,
        archive_succeeded=True,
    )
    assert state.phase is FinalizationPhase.ARCHIVED
