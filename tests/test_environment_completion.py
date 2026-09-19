"""Tests for the common environment completion lifecycle."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from titanium.environments.base import BaseEnvironment
from titanium.environments.docker.docker import DockerEnvironment
from titanium.environments.factory import EnvironmentFactory
from titanium.environments.gvisor.environment import GVisorEnvironment
from titanium.models.environment_type import EnvironmentType
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.config import EnvironmentConfig, OnCompletion
from titanium.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from titanium.models.trial.paths import TrialPaths


def test_on_completion_defaults_to_teardown():
    config = EnvironmentConfig()

    assert config.on_completion == OnCompletion.TEARDOWN


def test_on_completion_parses_archive():
    config = EnvironmentConfig(on_completion="archive")

    assert config.on_completion == OnCompletion.ARCHIVE


def test_on_completion_rejects_unknown_value():
    with pytest.raises(ValidationError):
        EnvironmentConfig(on_completion="archvie")


@pytest.mark.asyncio
async def test_complete_teardown_calls_stop():
    environment = object.__new__(DockerEnvironment)
    environment.stop = AsyncMock()
    environment.archive = AsyncMock()

    await BaseEnvironment.complete(
        environment,
        delete=True,
        on_completion=OnCompletion.TEARDOWN,
    )

    environment.stop.assert_awaited_once_with(delete=True)
    environment.archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_archive_calls_archive():
    environment = object.__new__(DockerEnvironment)
    environment.stop = AsyncMock()
    environment.archive = AsyncMock()

    await BaseEnvironment.complete(
        environment,
        delete=True,
        on_completion=OnCompletion.ARCHIVE,
    )

    environment.archive.assert_awaited_once_with(delete=True)
    environment.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_gvisor_archive_is_explicitly_unsupported():
    environment = object.__new__(GVisorEnvironment)

    with pytest.raises(NotImplementedError):
        await environment.archive(delete=True)


# ---------------------------------------------------------------------------
# Preflight: an unsupported archive must be refused before anything is built
# ---------------------------------------------------------------------------


def _task_dirs(tmp_path):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return environment_dir, trial_paths


def _create(tmp_path, env_type, on_completion):
    environment_dir, trial_paths = _task_dirs(tmp_path)
    return EnvironmentFactory.create_environment_from_config(
        TrialEnvironmentConfig(type=env_type, on_completion=on_completion),
        environment_dir=environment_dir,
        environment_name="probe",
        session_id="probe__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
    )


ARCHIVE_CAPABLE = [EnvironmentType.DOCKER, EnvironmentType.PODMAN]
ARCHIVE_UNSUPPORTED = [
    EnvironmentType.GVISOR,
    EnvironmentType.GVISOR_PODMAN,
    EnvironmentType.KRUN_PODMAN,
    EnvironmentType.CELLA,
]


@pytest.mark.parametrize("env_type", ARCHIVE_UNSUPPORTED)
def test_preflight_refuses_archive_for_unsupported_backends(env_type):
    # Before any environment exists: `titanium run` calls this first.
    with pytest.raises(ValueError, match="does not implement on_completion=archive"):
        EnvironmentFactory.run_preflight(
            type=env_type, completion_policy=OnCompletion.ARCHIVE
        )


@pytest.mark.parametrize("env_type", ARCHIVE_CAPABLE)
def test_preflight_allows_archive_for_container_backends(env_type):
    EnvironmentFactory.run_preflight(
        type=env_type, completion_policy=OnCompletion.ARCHIVE
    )


@pytest.mark.parametrize("env_type", ARCHIVE_UNSUPPORTED + ARCHIVE_CAPABLE)
def test_preflight_allows_teardown_everywhere(env_type):
    EnvironmentFactory.run_preflight(
        type=env_type, completion_policy=OnCompletion.TEARDOWN
    )


@pytest.mark.parametrize("env_type", ARCHIVE_UNSUPPORTED)
def test_unsupported_archive_never_creates_an_environment(tmp_path, env_type):
    # The refusal happens at construction too, so a non-CLI caller cannot
    # end up holding a live environment that can never be archived.
    with pytest.raises(ValueError, match="does not implement on_completion=archive"):
        _create(tmp_path, env_type, OnCompletion.ARCHIVE)


@pytest.mark.parametrize("env_type", ARCHIVE_UNSUPPORTED)
def test_unsupported_backends_still_build_for_teardown(tmp_path, env_type):
    environment = _create(tmp_path, env_type, OnCompletion.TEARDOWN)

    assert isinstance(environment, BaseEnvironment)


def test_cella_kwarg_on_completion_is_independent(tmp_path):
    # Cella's own `--ek on_completion=archive` is an environment kwarg and
    # keeps working: it is not the trial-level policy, so it is not refused.
    environment_dir, trial_paths = _task_dirs(tmp_path)
    environment = EnvironmentFactory.create_environment_from_config(
        TrialEnvironmentConfig(
            type=EnvironmentType.CELLA, kwargs={"on_completion": "archive"}
        ),
        environment_dir=environment_dir,
        environment_name="probe",
        session_id="probe__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
    )

    assert environment._on_completion == "archive"


def test_archive_capability_is_declared_not_inferred_from_defining_archive():
    from titanium.environments.factory import _supports_archive

    # gVisor DEFINES archive -- to refuse it -- so "defines archive" cannot be
    # the capability test; the declared flag is.
    assert hasattr(GVisorEnvironment, "archive")
    assert _supports_archive(DockerEnvironment)
    assert not _supports_archive(GVisorEnvironment)
    assert not _supports_archive(BaseEnvironment)
