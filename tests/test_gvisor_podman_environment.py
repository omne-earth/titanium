"""Unit tests for the first-class gVisor-on-Podman environment
(``--env gvisor-podman``).

Every Podman interaction is mocked: these tests must pass on a host with no
gVisor installed and no Podman available, matching the conventions of
``test_gvisor_environment.py`` and ``test_podman_environment.py``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pier.environments.gvisor import podman_runtime
from pier.environments.gvisor.environment import GVisorEnvironment
from pier.environments.gvisor.podman import (
    GVisorPodmanEnvironment,
    GVisorPodmanUnixOps,
)
from pier.environments.gvisor.podman_runtime import (
    assert_runtime_resolvable,
    parse_network_refs,
    runtime_name_matches,
)
from pier.environments.gvisor.transfer import GVisorUnixOps
from pier.environments.podman.podman import PodmanEnvironment
from pier.models.environment_type import EnvironmentType
from pier.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from pier.models.trial.paths import TrialPaths

MAIN_ID = "a" * 64
PROXY_ID = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the gVisor environment requires a Linux host"
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _make_env(tmp_path, **kwargs) -> GVisorPodmanEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    return GVisorPodmanEnvironment(
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
        **kwargs,
    )


class RecordingCli:
    """Stand-in for podman_runtime._run_cli that records argv and scripts replies.

    Replies are matched by a substring of the joined argv; unmatched calls
    return an empty success so discovery loops see "nothing found".
    """

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self._replies = replies or {}

    async def __call__(self, cli, *args):
        call = [cli, *[str(a) for a in args]]
        self.calls.append(call)
        joined = " ".join(call)
        for needle, reply in self._replies.items():
            if needle in joined:
                return reply
        return 0, "", ""


# ---------------------------------------------------------------------------
# Identity, composition, and construction
# ---------------------------------------------------------------------------


def test_reports_its_own_type(tmp_path):
    env = _make_env(tmp_path)
    assert env.type() is EnvironmentType.GVISOR_PODMAN
    assert GVisorPodmanEnvironment.type() is EnvironmentType.GVISOR_PODMAN


def test_mro_layers_gvisor_over_podman_over_docker():
    names = [cls.__name__ for cls in GVisorPodmanEnvironment.__mro__]
    assert names.index("GVisorEnvironment") < names.index("PodmanEnvironment")
    assert names.index("PodmanEnvironment") < names.index("DockerEnvironment")


def test_defaults_to_the_podman_engine(tmp_path):
    env = _make_env(tmp_path)
    assert env.engine == "podman"
    assert env.runtime == "runsc"


def test_engine_cli_honors_pier_podman_bin(tmp_path, monkeypatch):
    # Host-side inspection and cleanup must run the same binary as the compose
    # driving, or verification could interrogate a different Podman than the
    # one that created the containers.
    monkeypatch.setenv("PIER_PODMAN_BIN", "/opt/podman/bin/podman")
    env = _make_env(tmp_path)
    assert env.podman_bin == "/opt/podman/bin/podman"
    assert env._engine_cli == "/opt/podman/bin/podman"


def test_engine_docker_redirects_to_env_gvisor(tmp_path):
    with pytest.raises(ValueError, match=r"--env gvisor\b"):
        _make_env(tmp_path, engine="docker")


def test_unknown_engine_still_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match="Unknown container engine"):
        _make_env(tmp_path, engine="containerd")


def test_transfer_platform_is_the_rootless_staging_ops(tmp_path):
    # GVisorUnixOps (staging, not `podman cp`: the sandbox rootfs is private),
    # narrowed to the rootless ownership rule.
    env = _make_env(tmp_path)
    assert isinstance(env._platform, GVisorPodmanUnixOps)
    assert isinstance(env._platform, GVisorUnixOps)


def test_chown_to_host_user_is_a_noop(tmp_path):
    # Rootless Podman maps container root to the invoking user; the Docker
    # chown would resolve into the subuid range.
    import asyncio

    env = _make_env(tmp_path)
    asyncio.run(env._chown_to_host_user("/logs", recursive=True))  # must not raise


def test_staged_exports_are_chowned_to_container_root(tmp_path):
    # In-container 0:0 is what maps back to the invoking host user under
    # rootless Podman; the host's own numeric UID would map into subuids.
    env = _make_env(tmp_path)
    assert env._platform._host_owner() == "0:0"


def test_capabilities_are_the_gvisor_ones(tmp_path):
    env = _make_env(tmp_path)
    caps = env.capabilities
    assert caps.docker_compose is False  # compose tasks rejected, like gvisor
    assert caps.windows is False
    assert caps.disable_internet is True
    assert caps.filtered_egress is True


def test_project_name_matches_the_podman_convention(tmp_path):
    # Both parents compute the identical sanitized name; the combined class
    # must agree with the PodmanEnvironment property it composes with.
    env = _make_env(tmp_path)
    assert env._project_name == PodmanEnvironment._project_name.fget(env)
    assert env.project_name == env._project_name


# ---------------------------------------------------------------------------
# Runtime matching -- Podman reports a name or a resolved path
# ---------------------------------------------------------------------------


def test_runtime_name_matches_name_and_path_spellings():
    assert runtime_name_matches("runsc", "runsc")
    assert runtime_name_matches("/usr/local/bin/runsc", "runsc")
    assert runtime_name_matches("/usr/bin/runsc", "runsc")


def test_runtime_name_matches_rejects_other_runtimes():
    assert not runtime_name_matches("crun", "runsc")
    assert not runtime_name_matches("/usr/bin/crun", "runsc")
    assert not runtime_name_matches("runsc-custom", "runsc")
    assert not runtime_name_matches("/usr/bin/runsc-custom", "runsc")
    assert not runtime_name_matches("oci", "runsc")  # HostConfig placeholder
    assert not runtime_name_matches(None, "runsc")
    assert not runtime_name_matches("", "runsc")


def test_runtime_name_matches_requires_a_path_for_basename_matching():
    # A bare name only matches exactly; basename matching applies only to
    # path-like values, which is what Podman reports for by-path selection.
    assert runtime_name_matches("sandbox/runsc", "runsc")  # has a slash: path-like
    assert not runtime_name_matches("myrunsc", "runsc")


def test_environment_runtime_matches_uses_the_podman_rule(tmp_path):
    env = _make_env(tmp_path)
    assert env._runtime_matches("runsc")
    assert env._runtime_matches("/usr/local/bin/runsc")
    assert not env._runtime_matches("oci")
    assert not env._runtime_matches("crun")


def test_docker_flavor_runtime_match_is_still_exact(tmp_path):
    environment_dir = tmp_path / "env2"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial2")
    trial_paths.mkdir()
    env = GVisorEnvironment(
        environment_dir=environment_dir,
        environment_name="hello",
        session_id="hello__1",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
    )
    assert env._runtime_matches("runsc")
    assert not env._runtime_matches("/usr/local/bin/runsc")


# ---------------------------------------------------------------------------
# Inspect templates
# ---------------------------------------------------------------------------


def test_container_runtime_uses_the_oci_runtime_template(tmp_path, monkeypatch):
    # {{.HostConfig.Runtime}} always reads "oci" under Podman; the truthful
    # field is {{.OCIRuntime}}.
    import asyncio

    recorded = {}

    async def fake_inspect(container_id, template, cli):
        recorded["args"] = (container_id, template, cli)
        return "runsc"

    monkeypatch.setattr(podman_runtime, "_inspect", fake_inspect)
    env = _make_env(tmp_path)
    result = asyncio.run(env._container_runtime(MAIN_ID))

    assert result == "runsc"
    assert recorded["args"] == (MAIN_ID, "{{.OCIRuntime}}", env.podman_bin)


# ---------------------------------------------------------------------------
# Service resolution by label -- podman-compose ps cannot scope to a service
# ---------------------------------------------------------------------------


def test_compose_container_id_resolves_by_label_not_compose_ps(tmp_path, monkeypatch):
    import asyncio

    cli = RecordingCli(
        {
            "label=com.docker.compose.service=main": (0, f"{MAIN_ID}\n", ""),
        }
    )
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    resolved = asyncio.run(env._compose_container_id("main"))

    assert resolved == MAIN_ID
    # Running-only by default: no --all in the query.
    assert all("--all" not in call for call in cli.calls)
    # Both filters present in the first query, scoped to this exact project.
    first = " ".join(cli.calls[0])
    assert f"label=com.docker.compose.project={env._project_name}" in first
    assert "label=com.docker.compose.service=main" in first


def test_compose_container_id_include_stopped_passes_all(tmp_path, monkeypatch):
    import asyncio

    cli = RecordingCli()
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    resolved = asyncio.run(env._compose_container_id("main", include_stopped=True))

    assert resolved is None
    assert all("--all" in call for call in cli.calls)


def test_compose_container_id_falls_through_to_podman_namespace_labels(
    tmp_path, monkeypatch
):
    import asyncio

    cli = RecordingCli(
        {
            "label=io.podman.compose.service=main": (0, f"{MAIN_ID}\n", ""),
        }
    )
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    assert asyncio.run(env._compose_container_id("main")) == MAIN_ID


def test_service_resolution_fails_closed_on_query_failure(tmp_path, monkeypatch):
    import asyncio

    cli = RecordingCli({"ps": (1, "", "cannot connect")})
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    with pytest.raises(RuntimeError, match="could not resolve service 'main'"):
        asyncio.run(env._compose_container_id("main"))


# ---------------------------------------------------------------------------
# Project discovery -- label union, names accepted for networks, fail-closed
# ---------------------------------------------------------------------------


def test_parse_network_refs_accepts_names_and_ids():
    # Podman 4.x `network ls --quiet` prints names; hex-only parsing would
    # make teardown read "nothing to remove" while networks remain.
    out = "pier_default\n" + "c" * 12 + "\n\n  \n"
    assert parse_network_refs(out) == ["pier_default", "c" * 12]
    assert parse_network_refs("") == []
    assert parse_network_refs(None) == []
    assert parse_network_refs("two words\n") == []


def test_project_container_discovery_unions_both_label_namespaces(
    tmp_path, monkeypatch
):
    import asyncio

    cli = RecordingCli(
        {
            "label=com.docker.compose.project": (0, f"{MAIN_ID}\n", ""),
            "label=io.podman.compose.project": (
                0,
                f"{MAIN_ID}\n{PROXY_ID}\n",
                "",
            ),
        }
    )
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    ids = asyncio.run(env._query_project_container_ids())

    # Union, de-duplicated, order-stable.
    assert ids == [MAIN_ID, PROXY_ID]
    labels = " ".join(" ".join(call) for call in cli.calls)
    assert "label=com.docker.compose.project=" in labels
    assert "label=io.podman.compose.project=" in labels


def test_project_network_discovery_returns_names(tmp_path, monkeypatch):
    import asyncio

    cli = RecordingCli({"network ls": (0, "hello-world__abc123_default\n", "")})
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    refs = asyncio.run(env._query_project_network_ids())

    assert refs == ["hello-world__abc123_default"]


def test_project_discovery_fails_closed_when_a_query_fails(tmp_path, monkeypatch):
    # An empty list must mean "confirmed nothing remains", never "could not
    # check" -- otherwise teardown would report a clean project on a dead CLI.
    import asyncio

    cli = RecordingCli({"network ls": (125, "", "no such podman")})
    monkeypatch.setattr(podman_runtime, "_run_cli", cli)
    env = _make_env(tmp_path)

    with pytest.raises(RuntimeError, match="could not list networks"):
        asyncio.run(env._query_project_network_ids())


# ---------------------------------------------------------------------------
# Runtime resolvability probe
# ---------------------------------------------------------------------------


class RecordingRun:
    def __init__(self, create_rc: int = 0, create_stderr: str = ""):
        self.calls: list[list[str]] = []
        self._create_rc = create_rc
        self._create_stderr = create_stderr

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc = self._create_rc if "create" in argv else 0
        return subprocess.CompletedProcess(
            argv, rc, stdout="", stderr=self._create_stderr if rc else ""
        )


def test_runtime_probe_is_image_free_and_cleans_up(monkeypatch):
    run = RecordingRun()
    monkeypatch.setattr(podman_runtime.subprocess, "run", run)

    assert_runtime_resolvable("runsc", "podman")

    create = next(call for call in run.calls if "create" in call)
    assert "--rootfs" in create  # no image pull
    assert "--runtime" in create and "runsc" in create
    assert "--network" in create and "none" in create
    rm = next(call for call in run.calls if "rm" in call)
    assert "--force" in rm
    # Removal targets the same fixed probe name the create registered.
    probe_name = create[create.index("--name") + 1]
    assert probe_name in rm


def test_runtime_probe_fails_closed_with_podman_diagnostic(monkeypatch):
    run = RecordingRun(
        create_rc=125,
        create_stderr='Error: default OCI runtime "runsc" not found',
    )
    monkeypatch.setattr(podman_runtime.subprocess, "run", run)

    with pytest.raises(RuntimeError) as excinfo:
        assert_runtime_resolvable("runsc", "podman")

    message = str(excinfo.value)
    assert "not found" in message
    assert "runsc-podman.sh" in message  # actionable hint


def test_start_time_assert_routes_through_the_probe(tmp_path, monkeypatch):
    seen = {}

    def fake_assert(runtime, cli="podman", timeout_sec=30):
        seen["args"] = (runtime, cli)

    monkeypatch.setattr(
        "pier.environments.gvisor.podman.assert_runtime_resolvable", fake_assert
    )
    env = _make_env(tmp_path)
    env._assert_runtime_registered()

    assert seen["args"] == ("runsc", env.podman_bin)


# ---------------------------------------------------------------------------
# Compose override -- SELinux relabel on the staging binds
# ---------------------------------------------------------------------------


def _override_binds(env) -> list[dict]:
    data = json.loads(Path(env._compose_override_path).read_text())
    return data["services"]["main"]["volumes"]


def test_override_stamps_selinux_relabel_by_default(tmp_path):
    # Podman does not relabel bind mounts; without this an SELinux-enforcing
    # host denies the sandbox its own staging directories.
    env = _make_env(tmp_path)
    env._prepare_gvisor()

    binds = _override_binds(env)
    assert len(binds) == 2
    assert all(bind["bind"] == {"selinux": "z"} for bind in binds)
    assert binds[0]["read_only"] is True


def test_override_relabel_honors_the_podman_knob(tmp_path, monkeypatch):
    monkeypatch.setenv("PIER_PODMAN_SELINUX_RELABEL", "Z")
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    assert all(b["bind"] == {"selinux": "Z"} for b in _override_binds(env))


def test_override_relabel_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PIER_PODMAN_SELINUX_RELABEL", "off")
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    assert all("bind" not in b for b in _override_binds(env))


def test_override_disables_the_selinux_process_label(tmp_path):
    # Podman labels every container process on an enforcing host, and runsc
    # aborts on a labeled spec ("SELinux is not supported"), leaving main
    # stuck in Created and `up --wait` blocked forever.
    env = _make_env(tmp_path)
    env._prepare_gvisor()

    data = json.loads(Path(env._compose_override_path).read_text())
    assert data["services"]["main"]["security_opt"] == [
        "no-new-privileges:true",
        "label=disable",
    ]


def test_docker_flavor_override_never_gains_a_selinux_option(tmp_path):
    # runsc advertises selinux:false to Docker, so Docker assigns no label to
    # disable; the docker flavor's override must stay byte-compatible.
    environment_dir = tmp_path / "denv"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "dtrial")
    trial_paths.mkdir()
    env = GVisorEnvironment(
        environment_dir=environment_dir,
        environment_name="hello",
        session_id="hello__2",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
    )
    env._prepare_gvisor()
    assert all("bind" not in b for b in _override_binds(env))


def test_override_path_is_appended_last_to_compose_paths(tmp_path):
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    assert env._docker_compose_paths[-1] == env._compose_override_path


# ---------------------------------------------------------------------------
# Compose driving -- podman-compose, no Docker socket
# ---------------------------------------------------------------------------


def test_compose_base_is_podman_compose_with_the_project_name(tmp_path):
    env = _make_env(tmp_path)
    base = env._compose_base()
    assert "podman-compose" in Path(base[0]).name
    assert "--project-name" in base
    assert env._project_name in base


def test_compose_env_never_carries_a_docker_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    env = _make_env(tmp_path)
    assert "DOCKER_HOST" not in env._compose_env()


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_checks_podman_then_the_runtime(monkeypatch):
    order = []

    monkeypatch.setattr(
        PodmanEnvironment,
        "preflight",
        classmethod(lambda cls: order.append("podman")),
    )
    monkeypatch.setattr(
        "pier.environments.gvisor.podman.assert_runtime_resolvable",
        lambda runtime, cli="podman", timeout_sec=30: order.append(
            ("runtime", runtime)
        ),
    )

    GVisorPodmanEnvironment.preflight()

    assert order == ["podman", ("runtime", "runsc")]


def test_preflight_never_touches_the_docker_daemon(monkeypatch):
    # The MRO's next preflight after GVisorPodman's would be GVisorEnvironment,
    # which asserts against `docker info`; the podman flavor must bypass it.
    monkeypatch.setattr(PodmanEnvironment, "preflight", classmethod(lambda cls: None))
    monkeypatch.setattr(
        "pier.environments.gvisor.podman.assert_runtime_resolvable",
        lambda *a, **k: None,
    )

    def boom(*args, **kwargs):  # pragma: no cover - the point is it never runs
        raise AssertionError("docker daemon consulted during podman preflight")

    monkeypatch.setattr("pier.environments.gvisor.runtime.engine_runtimes", boom)

    GVisorPodmanEnvironment.preflight()
