"""Tests for the common environment completion lifecycle."""

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from titanium.environments.base import ArchiveError, BaseEnvironment
from titanium.environments.docker.docker import DockerEnvironment
from titanium.environments.factory import EnvironmentFactory
from titanium.environments.gvisor.environment import GVisorEnvironment
from titanium.environments.gvisor.podman import GVisorPodmanEnvironment
from titanium.environments.krun.podman import KrunPodmanEnvironment
from titanium.environments.podman import podman as podman_module
from titanium.environments.podman.podman import PodmanEnvironment
from titanium.models.environment_type import EnvironmentType
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.config import EnvironmentConfig, OnCompletion
from titanium.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from titanium.models.trial.paths import TrialPaths
from titanium.trial.trial import Trial


def test_on_completion_defaults_to_teardown():
    config = EnvironmentConfig()

    assert config.on_completion == OnCompletion.TEARDOWN


def test_on_completion_parses_archive():
    config = EnvironmentConfig(on_completion="archive")

    assert config.on_completion == OnCompletion.ARCHIVE


def test_on_completion_rejects_unknown_value():
    with pytest.raises(ValidationError):
        EnvironmentConfig(on_completion="archvie")


# ---------------------------------------------------------------------------
# Trial-level lifecycle: archive publishes the artifact, stop reclaims
# ---------------------------------------------------------------------------
# The environment no longer dispatches on the policy. `archive()` only
# produces the artifact and `stop(delete=...)` is the sole owner of
# reclamation, so the policy now lives in the trial that calls both.


def _trial_for_completion(environment, on_completion, trial_paths=None):
    """A Trial carrying only what `_stop_agent_environment` reads."""
    trial = object.__new__(Trial)
    trial._environment = environment
    trial._is_agent_environment_stopped = False
    trial._trial_paths = trial_paths
    trial.config = SimpleNamespace(
        trial_name="probe",
        environment=EnvironmentConfig(on_completion=on_completion, delete=True),
    )
    trial._result = SimpleNamespace(environment_completion=None, exception_info=None)
    return trial


@pytest.mark.asyncio
async def test_trial_teardown_stops_without_archiving():
    environment = AsyncMock()
    trial = _trial_for_completion(environment, OnCompletion.TEARDOWN)

    await trial._stop_agent_environment()

    environment.archive.assert_not_awaited()
    environment.stop.assert_awaited_once_with(delete=True)
    assert trial.result.environment_completion is None


@pytest.mark.asyncio
async def test_trial_archive_publishes_the_artifact_then_stops():
    environment = AsyncMock()
    order = []
    environment.archive.side_effect = lambda: order.append("archive")
    environment.stop.side_effect = lambda **kwargs: order.append("stop")
    trial = _trial_for_completion(environment, OnCompletion.ARCHIVE)

    await trial._stop_agent_environment()

    # The artifact exists before the environment it was taken from is gone.
    assert order == ["archive", "stop"]
    environment.archive.assert_awaited_once_with()
    environment.stop.assert_awaited_once_with(delete=True)
    assert trial.result.environment_completion.status == "succeeded"
    assert (
        trial.result.environment_completion.archive_path == "archive/environment.tar"
    )


@pytest.mark.asyncio
async def test_trial_archive_failure_is_not_reported_as_succeeded(tmp_path):
    # A failed archive must not publish a success, and must not be papered
    # over by the stop that would otherwise follow it.
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    environment = AsyncMock()
    environment.archive.side_effect = ArchiveError("export exited 1")
    trial = _trial_for_completion(environment, OnCompletion.ARCHIVE, trial_paths)

    await trial._stop_agent_environment()

    environment.stop.assert_not_awaited()
    assert trial.result.environment_completion.status == "failed"


@pytest.mark.asyncio
async def test_krun_archive_is_explicitly_unsupported():
    # krun inherits from the gVisor family but opts out: `podman stop`
    # reaches only the VMM, so the guest state an archive would promise
    # does not survive.
    environment = object.__new__(KrunPodmanEnvironment)

    with pytest.raises(NotImplementedError):
        await environment.archive()


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


# Rootless Podman-family rungs: archive is restricted to these, because
# a rootless engine export is the only supported capture path.
ARCHIVE_CAPABLE = [
    EnvironmentType.PODMAN,
    EnvironmentType.GVISOR_PODMAN,
]
# krun hard-kills its VMM, cella manages its own machines, and rootful
# Docker (with or without gVisor) is excluded from archiving outright.
ARCHIVE_UNSUPPORTED = [
    EnvironmentType.KRUN_PODMAN,
    EnvironmentType.CELLA,
    EnvironmentType.DOCKER,
    EnvironmentType.GVISOR
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


# krun DEFINES archive -- to refuse it -- so "defines archive" cannot be
# the capability test; the declared flag is.
def test_archive_capability_is_declared_not_inferred_from_defining_archive():
    from titanium.environments.factory import _supports_archive

    assert hasattr(KrunPodmanEnvironment, "archive")

    assert not _supports_archive(DockerEnvironment)
    assert _supports_archive(PodmanEnvironment)
    assert not _supports_archive(GVisorEnvironment)
    assert _supports_archive(GVisorPodmanEnvironment)
    assert not _supports_archive(KrunPodmanEnvironment)
    assert not _supports_archive(BaseEnvironment)


# ---------------------------------------------------------------------------
# Archive preflight: rootless Podman is a precondition, checked before build
# ---------------------------------------------------------------------------


def _podman_info(rootless: str):
    """Answer only the rootless query `archive_preflight` issues."""

    def fake_run(command, **kwargs):
        assert command[1:] == [
            "info",
            "--format",
            "{{.Host.Security.Rootless}}",
        ]
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{rootless}\n", stderr=""
        )

    return fake_run


def _silence_host_preflight(monkeypatch):
    """Keep the ordinary host preflight off a real engine in unit tests."""
    monkeypatch.setattr(
        PodmanEnvironment, "preflight", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(
        GVisorPodmanEnvironment, "preflight", classmethod(lambda cls: None)
    )


@pytest.mark.parametrize("env_type", ARCHIVE_CAPABLE)
def test_archive_preflight_accepts_rootless_podman(env_type, monkeypatch):
    monkeypatch.setattr(podman_module.subprocess, "run", _podman_info("true"))
    _silence_host_preflight(monkeypatch)

    EnvironmentFactory.run_preflight(
        type=env_type, completion_policy=OnCompletion.ARCHIVE
    )


@pytest.mark.parametrize("env_type", ARCHIVE_CAPABLE)
def test_archive_preflight_refuses_rootful_podman(env_type, monkeypatch):
    monkeypatch.setattr(podman_module.subprocess, "run", _podman_info("false"))
    _silence_host_preflight(monkeypatch)

    with pytest.raises(SystemExit, match="requires rootless Podman"):
        EnvironmentFactory.run_preflight(
            type=env_type, completion_policy=OnCompletion.ARCHIVE
        )


@pytest.mark.parametrize("env_type", ARCHIVE_CAPABLE)
def test_rootful_archive_never_creates_an_environment(
    tmp_path, env_type, monkeypatch
):
    # The refusal lands before the environment exists, so nothing is built
    # for a policy the host cannot honor.
    monkeypatch.setattr(podman_module.subprocess, "run", _podman_info("false"))
    _silence_host_preflight(monkeypatch)

    with pytest.raises(SystemExit, match="requires rootless Podman"):
        _create(tmp_path, env_type, OnCompletion.ARCHIVE)


@pytest.mark.parametrize("env_type", ARCHIVE_CAPABLE)
def test_teardown_preflight_is_not_rootless_gated(env_type, monkeypatch):
    # Ordinary teardown must keep working on a rootful host: the rootless
    # requirement belongs to archiving alone.
    asked = []

    def fake_run(command, **kwargs):
        asked.append(list(command))
        return subprocess.CompletedProcess(
            command, 0, stdout="false\n", stderr=""
        )

    monkeypatch.setattr(podman_module.subprocess, "run", fake_run)
    _silence_host_preflight(monkeypatch)

    EnvironmentFactory.run_preflight(
        type=env_type, completion_policy=OnCompletion.TEARDOWN
    )

    assert asked == []
