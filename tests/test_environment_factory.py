"""Environment type, factory registration, and the Docker-only engine seam.

No Docker daemon and no gVisor installation are required: the factory only
imports classes, and construction is driven with a task config that never
starts anything.
"""

import sys

import pytest

from titanium.environments.docker.docker import DockerEnvironment
from titanium.environments.factory import (
    _ENVIRONMENT_REGISTRY,
    EnvironmentFactory,
    _load_environment_class,
)
from titanium.environments.gvisor.environment import GVisorEnvironment
from titanium.environments.gvisor.runtime import resolve_engine_cli
from titanium.models.environment_type import EnvironmentType
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from titanium.models.trial.paths import TrialPaths

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


def test_gvisor_podman_is_a_first_class_environment_type():
    assert EnvironmentType.GVISOR_PODMAN.value == "gvisor-podman"
    assert EnvironmentType("gvisor-podman") is EnvironmentType.GVISOR_PODMAN


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


def test_gvisor_podman_is_registered_in_the_factory():
    from titanium.environments.gvisor.podman import GVisorPodmanEnvironment

    assert EnvironmentType.GVISOR_PODMAN in _ENVIRONMENT_REGISTRY
    assert (
        _load_environment_class(EnvironmentType.GVISOR_PODMAN)
        is GVisorPodmanEnvironment
    )
    assert _ENVIRONMENT_REGISTRY[EnvironmentType.GVISOR_PODMAN].pip_extra is None


def test_env_gvisor_podman_resolves_through_the_factory(tmp_path):
    from titanium.environments.gvisor.podman import GVisorPodmanEnvironment

    env = _from_config(
        tmp_path, TrialEnvironmentConfig(type=EnvironmentType.GVISOR_PODMAN)
    )

    assert isinstance(env, GVisorPodmanEnvironment)
    assert env.type() is EnvironmentType.GVISOR_PODMAN
    assert env.engine == "podman"
    assert env.runtime == "runsc"


def test_env_gvisor_still_resolves_to_the_docker_flavor(tmp_path):
    # Adding the podman flavor must not change what --env gvisor selects.
    from titanium.environments.gvisor.podman import GVisorPodmanEnvironment

    env = _from_config(tmp_path, TrialEnvironmentConfig(type=EnvironmentType.GVISOR))

    assert type(env) is GVisorEnvironment
    assert not isinstance(env, GVisorPodmanEnvironment)
    assert env.engine == "docker"


def test_engine_docker_on_env_gvisor_podman_redirects_to_gvisor(tmp_path):
    with pytest.raises(ValueError, match=r"--env gvisor\b"):
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.GVISOR_PODMAN, kwargs={"engine": "docker"}
            ),
        )


def test_krun_podman_is_a_first_class_environment_type():
    assert EnvironmentType.KRUN_PODMAN.value == "krun-podman"
    assert EnvironmentType("krun-podman") is EnvironmentType.KRUN_PODMAN


def test_krun_podman_is_registered_in_the_factory():
    from titanium.environments.krun.podman import KrunPodmanEnvironment

    assert EnvironmentType.KRUN_PODMAN in _ENVIRONMENT_REGISTRY
    assert (
        _load_environment_class(EnvironmentType.KRUN_PODMAN) is KrunPodmanEnvironment
    )
    assert _ENVIRONMENT_REGISTRY[EnvironmentType.KRUN_PODMAN].pip_extra is None


def test_env_krun_podman_resolves_through_the_factory(tmp_path):
    from titanium.environments.krun.podman import KrunPodmanEnvironment

    env = _from_config(
        tmp_path, TrialEnvironmentConfig(type=EnvironmentType.KRUN_PODMAN)
    )

    assert isinstance(env, KrunPodmanEnvironment)
    assert env.type() is EnvironmentType.KRUN_PODMAN
    assert env.engine == "podman"
    assert env.runtime == "krun"


def test_engine_docker_on_env_krun_podman_fails_without_a_redirect(tmp_path):
    # There is no docker flavor of the krun sandbox to redirect to.
    with pytest.raises(ValueError, match=r"krun-podman.*only drives"):
        _from_config(
            tmp_path,
            TrialEnvironmentConfig(
                type=EnvironmentType.KRUN_PODMAN, kwargs={"engine": "docker"}
            ),
        )


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


def test_engine_podman_on_env_gvisor_redirects_to_gvisor_podman(tmp_path):
    # The engine exists now, but the docker-flavored environment does not
    # drive it: the failure must name the selector that does.
    with pytest.raises(ValueError, match=r"--env gvisor-podman"):
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


def test_engine_never_silently_resolves_outside_its_environment():
    # The seam is thin on purpose, but it must never resolve an engine a given
    # environment does not drive: that would hand back a sandbox driven by a
    # different engine than the caller asked for.
    assert resolve_engine_cli("docker") == "docker"
    assert resolve_engine_cli("DOCKER") == "docker"
    assert resolve_engine_cli("podman", supported=("podman",)) == "podman"
    assert resolve_engine_cli("PODMAN", supported=("podman",)) == "podman"

    # Known engines outside the supported set redirect rather than resolve.
    with pytest.raises(ValueError, match=r"--env gvisor-podman"):
        resolve_engine_cli("podman", supported=("docker",))
    with pytest.raises(ValueError, match=r"--env gvisor"):
        resolve_engine_cli("docker", supported=("podman",))

    # Unknown engines are rejected regardless of the supported set.
    for engine in ("containerd", "", "runc"):
        with pytest.raises(ValueError, match="Unknown container engine"):
            resolve_engine_cli(engine)
        with pytest.raises(ValueError, match="Unknown container engine"):
            resolve_engine_cli(engine, supported=("podman",))


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
