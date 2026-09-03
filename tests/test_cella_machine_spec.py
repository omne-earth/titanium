"""Contract tests for ``build_machine_spec`` -- the Task/trial -> Cella machine
identity projection.

Nothing here touches KVM, a Cella binary, a network, or a running machine: the
function under test is a pure projection, so these tests are fast and run
anywhere.

NOT tested here, on purpose: refusing gpus / windows / docker_image /
oversized storage_mb / allow_internet. Those are
``CellaEnvironment._validate_definition``'s, not this function's -- see the
ownership boundary in ``titanium.environments.cella.spec``.
"""

import re
from pathlib import Path

import pytest

from titanium.environments.cella.definition import CellaEnvironmentDefinition
from titanium.environments.cella.spec import (
    MAX_MACHINE_NAME_LEN,
    CellaMachineSpec,
    build_machine_spec,
)
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.paths import TrialPaths

# Cella's own rule, for assertions: crates/cella-libs/src/machine.rs
# `valid_name` -- lowercase ASCII, digits, '-', and no leading '-'.
VALID_CELLA_NAME = r"^[a-z0-9][a-z0-9-]*$"


def make_trial_paths(tmp_path: Path, name: str = "trial") -> TrialPaths:
    """A real on-disk trial directory, the way every other suite builds one."""
    trial_paths = TrialPaths(trial_dir=tmp_path / name)
    trial_paths.mkdir()
    return trial_paths


def make_definition(**overrides) -> CellaEnvironmentDefinition:
    """A task's ``environment/cella.toml``, with both flavors set."""
    fields = {"kernel": "canonical", "rootfs": "titanium-smoke"}
    fields.update(overrides)
    return CellaEnvironmentDefinition(**fields)


def make_task_env_config(**overrides) -> TaskEnvironmentConfig:
    """A task ``[environment]`` table. Defaults leave ``memory_mb`` unset, which
    is the case A4 cares about."""
    return TaskEnvironmentConfig(**overrides)


def build(tmp_path: Path, **overrides) -> CellaMachineSpec:
    """One-call wrapper so a test reads as one line.

    Override any keyword ``build_machine_spec`` takes::

        spec = build(tmp_path, session_id="my-task__AbCdEfG")
        spec = build(tmp_path, task_env_config=make_task_env_config(memory_mb=1024))
    """
    kwargs = {
        "session_id": "verify-cella-env__AbCdEfG",
        "environment_name": "verify-cella-env",
        "task_env_config": make_task_env_config(),
        "definition": make_definition(),
        "trial_paths": make_trial_paths(tmp_path),
    }
    kwargs.update(overrides)
    return build_machine_spec(**kwargs)


@pytest.mark.parametrize(
    "session_id",
    [
        "Hello.World",
        "ABC__123",
        "spaces are here",
        "UPPER_case.and.dots",
        "unicode-💥-thing",
        "x" * 200,
    ],
)

# a1
def test_name_is_valid_cella_name(tmp_path: Path, session_id: str):
    spec = build(tmp_path, session_id=session_id)

    assert re.fullmatch(VALID_CELLA_NAME, spec.name)
    assert len(spec.name) <= MAX_MACHINE_NAME_LEN


# a2
def test_name_is_deterministic(tmp_path: Path):
    first = build(tmp_path, session_id="Same.Session__123")
    second = build(tmp_path, session_id="Same.Session__123")

    assert first.name == second.name


# a3
def test_distinct_sessions_do_not_collide(tmp_path: Path):
    common_prefix = "a" * 100

    first = build(tmp_path, session_id=f"{common_prefix}-first")
    second = build(tmp_path, session_id=f"{common_prefix}-second")

    assert first.name != second.name


# error case: no usable characters
def test_name_rejects_input_with_no_usable_characters(tmp_path: Path):
    with pytest.raises(ValueError):
        build(tmp_path, session_id="💥💥💥")


# a4
@pytest.mark.parametrize("memory_mb", [None, 1024])
def test_memory_is_not_invented(tmp_path: Path, memory_mb: int | None):
    paths = make_trial_paths(tmp_path)
    config = make_task_env_config(memory_mb=memory_mb)

    spec = build_machine_spec(
        session_id="memory-test",
        environment_name="test",
        task_env_config=config,
        definition=make_definition(),
        trial_paths=paths,
    )

    assert spec.mem_mb == memory_mb


# a5
def test_network_is_none(tmp_path: Path):
    spec = build(tmp_path)

    assert spec.net == "none"


# a6
def test_cella_home_is_trial_scoped(tmp_path: Path):
    paths = make_trial_paths(tmp_path)

    spec = build_machine_spec(
        session_id="home-test",
        environment_name="test",
        task_env_config=make_task_env_config(),
        definition=make_definition(),
        trial_paths=paths,
    )

    assert spec.cella_home == paths.trial_dir / ".cella"


# a8
def test_flavors_come_from_the_definition(tmp_path: Path):
    paths = make_trial_paths(tmp_path)
    definition = make_definition(
        kernel="my-kernel",
        rootfs="my-rootfs",
    )

    spec = build_machine_spec(
        session_id="flavor-test",
        environment_name="test",
        task_env_config=make_task_env_config(),
        definition=definition,
        trial_paths=paths,
    )

    assert spec.kernel_flavor == "my-kernel"
    assert spec.rootfs_flavor == "my-rootfs"
