"""Environment type, factory registration, and the Docker-only engine seam.

No Docker daemon and no gVisor installation are required: the factory only
imports classes, and construction is driven with a task config that never
starts anything.
"""

import sys

import pytest

from pier.environments.docker.docker import DockerEnvironment
from pier.environments.factory import (
    _ENVIRONMENT_REGISTRY,
    EnvironmentFactory,
    _load_environment_class,
)
from pier.environments.gvisor.environment import GVisorEnvironment
from pier.environments.gvisor.runtime import resolve_engine_cli
from pier.models.environment_type import EnvironmentType
from pier.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from pier.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from pier.models.trial.paths import TrialPaths

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the gVisor environment requires a Linux host"
)


def _task_dirs(tmp_path):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return environment_dir, trial_paths


def _from_config(tmp_path, config: TrialEnvironmentConfig, **kwargs):
    environment_dir, trial_paths = _task_dirs(tmp_path)
    return EnvironmentFactory.create_environment_from_config(
        config,
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Environment type
# ---------------------------------------------------------------------------


def test_gvisor_is_a_first_class_environment_type():
    assert EnvironmentType.GVISOR.value == "gvisor"
    assert EnvironmentType("gvisor") is EnvironmentType.GVISOR


def test_gvisor_environment_reports_its_type():
    assert GVisorEnvironment.type() is EnvironmentType.GVISOR


def test_docker_environment_type_is_unchanged():
    assert EnvironmentType.DOCKER.value == "docker"
    assert DockerEnvironment.type() is EnvironmentType.DOCKER


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------


def test_gvisor_is_registered_in_the_factory():
    assert EnvironmentType.GVISOR in _ENVIRONMENT_REGISTRY
    assert _load_environment_class(EnvironmentType.GVISOR) is GVisorEnvironment


def test_gvisor_needs_no_optional_extra():
    assert _ENVIRONMENT_REGISTRY[EnvironmentType.GVISOR].pip_extra is None


def test_env_gvisor_resolves_through_the_factory(tmp_path):
    env = _from_config(tmp_path, TrialEnvironmentConfig(type=EnvironmentType.GVISOR))

    assert isinstance(env, GVisorEnvironment)
    assert env.type() is EnvironmentType.GVISOR


def test_env_docker_still_resolves_to_plain_docker(tmp_path):
    env = _from_config(tmp_path, TrialEnvironmentConfig(type=EnvironmentType.DOCKER))

    assert type(env) is DockerEnvironment
    assert not isinstance(env, GVisorEnvironment)


def test_default_environment_type_is_still_docker(tmp_path):
    # No explicit type: TrialEnvironmentConfig defaults to Docker, and adding
    # the gVisor member must not change that.
    env = _from_config(tmp_path, TrialEnvironmentConfig())

    assert type(env) is DockerEnvironment


# ---------------------------------------------------------------------------
# Engine seam -- Docker is the only implemented engine
# ---------------------------------------------------------------------------


def test_no_engine_kwarg_defaults_to_docker(tmp_path):
    env = _from_config(tmp_path, TrialEnvironmentConfig(type=EnvironmentType.GVISOR))

    assert env.engine == "docker"
    assert env._engine_cli == "docker"


def test_engine_docker_is_accepted(tmp_path):
    env = _from_config(
        tmp_path,
        TrialEnvironmentConfig(
            type=EnvironmentType.GVISOR, kwargs={"engine": "docker"}
        ),
    )

    assert env.engine == "docker"


def test_engine_podman_fails_immediately_and_clearly(tmp_path):
    with pytest.raises(NotImplementedError, match="gVisor podman engine support"):
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.GVISOR, kwargs={"engine": "podman"}
            ),
        )


def test_unknown_engine_fails_immediately_and_clearly(tmp_path):
    with pytest.raises(ValueError, match="Unknown container engine 'containerd'"):
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.GVISOR, kwargs={"engine": "containerd"}
            ),
        )


def test_engine_never_silently_falls_back_to_docker():
    # The seam is thin on purpose, but it must never resolve an unimplemented
    # engine to the Docker CLI: that would hand back different isolation
    # properties than the caller asked for.
    assert resolve_engine_cli("docker") == "docker"
    assert resolve_engine_cli("DOCKER") == "docker"

    for engine in ("podman", "containerd", "", "runc"):
        with pytest.raises((NotImplementedError, ValueError)):
            resolve_engine_cli(engine)


# ---------------------------------------------------------------------------
# The obsolete `--env docker --ek gvisor=true` interface
# ---------------------------------------------------------------------------


def test_docker_rejects_the_obsolete_gvisor_kwarg(tmp_path):
    # Environment kwargs are splatted into the constructor unvalidated, so
    # without this check BaseEnvironment(**kwargs) would swallow `gvisor` and
    # hand back an ordinary Docker sandbox with no gVisor isolation.
    with pytest.raises(ValueError) as excinfo:
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.DOCKER, kwargs={"gvisor": True}
            ),
        )

    message = str(excinfo.value)
    assert "--env gvisor" in message
    assert "'gvisor'" in message


def test_docker_rejects_the_obsolete_gvisor_runtime_kwarg(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.DOCKER,
                kwargs={"gvisor_runtime": "runsc-custom"},
            ),
        )

    message = str(excinfo.value)
    assert "--env gvisor" in message
    assert "--ek runtime=runsc-custom" in message


def test_docker_rejects_the_obsolete_kwargs_even_when_disabled(tmp_path):
    # `gvisor=false` is not a request for plain Docker -- the kwarg simply no
    # longer exists, so its presence is always an error.
    with pytest.raises(ValueError, match="--env gvisor"):
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.DOCKER, kwargs={"gvisor": False}
            ),
        )


def test_obsolete_kwargs_do_not_leak_into_the_gvisor_environment(tmp_path):
    # The check is scoped to Docker, but the gVisor environment must still not
    # quietly accept the old names: its signature has no such parameter, and
    # BaseEnvironment would otherwise absorb them.
    env = _from_config(
        tmp_path,
        TrialEnvironmentConfig(type=EnvironmentType.GVISOR, kwargs={"gvisor": True}),
    )

    assert isinstance(env, GVisorEnvironment)
    assert env.runtime == "runsc"


def test_docker_still_accepts_its_own_kwargs(tmp_path):
    # Narrow by construction: this must not become general kwargs validation.
    env = _from_config(
        tmp_path,
        TrialEnvironmentConfig(
            type=EnvironmentType.DOCKER, kwargs={"keep_containers": True}
        ),
    )

    assert type(env) is DockerEnvironment


def test_unregistered_environment_type_still_raises():
    class FakeType(str):
        pass

    with pytest.raises(ValueError, match="Unsupported environment type"):
        _load_environment_class(FakeType("nope"))
