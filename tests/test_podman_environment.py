import stat
import types
from pathlib import Path

import pytest

from pier.environments.factory import _load_environment_class
from pier.environments.podman.podman import PodmanEnvironment, _which_compose
from pier.models.environment_type import EnvironmentType


def _stub(**overrides):
    """Minimal object carrying just what _compose_base reads."""
    stub = types.SimpleNamespace(
        compose_cmd=["podman-compose"],
        in_pod="false",
        run_args="",
        session_id="DeepSWE Trial.01",
        _docker_compose_paths=[Path("/tmp/compose-base.yaml")],
    )
    for key, value in overrides.items():
        setattr(stub, key, value)
    stub._project_name = PodmanEnvironment._project_name.fget(stub)
    return stub


def test_registered_in_factory():
    assert EnvironmentType.PODMAN.value == "podman"
    assert _load_environment_class(EnvironmentType.PODMAN) is PodmanEnvironment
    assert PodmanEnvironment.type() == "podman"


def test_compose_base_targets_podman_compose():
    cmd = PodmanEnvironment._compose_base(_stub())
    assert cmd[0] == "podman-compose"
    assert "docker" not in cmd
    # podman-compose has no --project-directory; it is replaced by subprocess cwd.
    assert "--project-directory" not in cmd
    assert cmd[cmd.index("--project-name") + 1] == "deepswe-trial-01"
    assert cmd[cmd.index("-f") + 1] == "/tmp/compose-base.yaml"


def test_defaults_to_one_netns_per_service():
    # A shared pod has no per-service network, which breaks allow_internet=false
    # tasks: main runs network_mode: none while the egress proxy needs a net.
    assert "--in-pod=false" in PodmanEnvironment._compose_base(_stub())


def test_podman_run_args_are_attached_with_equals():
    # argparse rejects a '-'-leading value token, so it must be attached with '='.
    cmd = PodmanEnvironment._compose_base(
        _stub(run_args="--add-host=host.containers.internal:host-gateway")
    )
    assert "--podman-run-args=--add-host=host.containers.internal:host-gateway" in cmd
    assert "--podman-run-args" not in cmd


def test_no_option_value_is_a_bare_dash_prefixed_token():
    """Any argparse-hostile value must be attached with '=', not passed alone."""
    cmd = PodmanEnvironment._compose_base(
        _stub(run_args="--security-opt label=disable")
    )
    for previous, current in zip(cmd, cmd[1:]):
        if previous.startswith("-") and "=" not in previous:
            assert not current.startswith("-"), (
                f"{current!r} follows bare option {previous!r}; argparse will "
                "reject it as a missing argument"
            )


def test_daemon_os_is_reported_without_shelling_out():
    # Podman's info schema has no OSType field for the parent's template.
    assert PodmanEnvironment._detect_daemon_os() == "linux"


def test_windows_capability_is_off():
    caps = PodmanEnvironment.capabilities.fget(object())
    assert caps.windows is False
    assert caps.filtered_egress is True
    assert caps.docker_compose is True


@pytest.mark.parametrize(
    "info,expected",
    [
        ("v2|[cpu io memory pids]", (True, True)),
        ("v2|[io pids]", (False, False)),
        ("v2|[cpu io pids]", (True, False)),
        ("v1|[cpu memory]", (False, False)),
    ],
)
def test_resource_capabilities_track_cgroup_delegation(monkeypatch, info, expected):
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout=info, returncode=0),
    )
    caps = PodmanEnvironment.resource_capabilities()
    assert (caps.cpu_limit, caps.memory_limit) == expected


def test_resource_capabilities_fall_back_when_podman_is_unavailable(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(subprocess, "run", boom)
    caps = PodmanEnvironment.resource_capabilities()
    assert (caps.cpu_limit, caps.memory_limit) == (True, True)


def test_bind_mounts_are_tagged_for_selinux_relabel():
    env = types.SimpleNamespace(
        _mounts_json=[
            {"type": "bind", "source": "/jobs/agent", "target": "/logs/agent"},
            {"type": "volume", "source": "cache", "target": "/cache"},
        ]
    )
    PodmanEnvironment._apply_selinux_relabel(env)

    assert env._mounts_json[0]["bind"]["selinux"] == "z"
    # 'Z' (private category) would lock out the separate verifier container.
    assert env._mounts_json[0]["bind"]["selinux"] != "Z"
    assert "bind" not in env._mounts_json[1]


def test_selinux_relabel_respects_an_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("PIER_PODMAN_SELINUX_RELABEL", "none")
    env = types.SimpleNamespace(
        _mounts_json=[{"type": "bind", "source": "/a", "target": "/b"}]
    )
    PodmanEnvironment._apply_selinux_relabel(env)
    assert "bind" not in env._mounts_json[0]


def test_existing_mount_options_are_not_clobbered():
    env = types.SimpleNamespace(
        _mounts_json=[
            {
                "type": "bind",
                "source": "/a",
                "target": "/b",
                "bind": {"create_host_path": False},
            }
        ]
    )
    PodmanEnvironment._apply_selinux_relabel(env)
    assert env._mounts_json[0]["bind"] == {"create_host_path": False, "selinux": "z"}


def _fake_exe(directory: Path, name: str) -> Path:
    exe = directory / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


@pytest.fixture
def compose_dirs(monkeypatch, tmp_path):
    """Isolated PATH dir and interpreter bin dir for _which_compose tests."""
    path_dir = tmp_path / "on-path"
    venv_bin = tmp_path / "venv-bin"
    path_dir.mkdir()
    venv_bin.mkdir()
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr("sys.executable", str(venv_bin / "python"))
    return path_dir, venv_bin


def test_which_compose_prefers_path(compose_dirs):
    # PATH wins so PIER_PODMAN_COMPOSE overrides behave like normal lookups.
    path_dir, venv_bin = compose_dirs
    on_path = _fake_exe(path_dir, "podman-compose")
    _fake_exe(venv_bin, "podman-compose")
    assert _which_compose("podman-compose") == str(on_path)


def test_which_compose_falls_back_to_interpreter_bin_dir(compose_dirs):
    # .venv/bin is not on PATH when pier is invoked as `.venv/bin/pier`.
    _, venv_bin = compose_dirs
    in_venv = _fake_exe(venv_bin, "podman-compose")
    assert _which_compose("podman-compose") == str(in_venv)


def test_which_compose_returns_none_when_absent_everywhere(compose_dirs):
    assert _which_compose("podman-compose") is None


def test_preflight_finds_compose_next_to_interpreter(monkeypatch, compose_dirs):
    # Must not demand an install when podman-compose is in pier's own venv.
    path_dir, venv_bin = compose_dirs
    _fake_exe(path_dir, "podman")
    _fake_exe(venv_bin, "podman-compose")

    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    PodmanEnvironment.preflight()  # must not raise SystemExit


@pytest.mark.asyncio
async def test_chown_to_host_user_is_a_noop_under_rootless():
    # The parent's chown maps through the userns into the subuid range,
    # costing the host write access to its own artifacts dir.
    calls = []

    class Probe(PodmanEnvironment):
        async def _podman(self, args, **kwargs):  # pragma: no cover
            calls.append(args)

    await PodmanEnvironment._chown_to_host_user(
        Probe.__new__(Probe), "/logs/artifacts", recursive=True
    )
    assert calls == []


# ---------------------------------------------------------------------------
# Cgroup limit verification -- enforce-what-you-declared, host-side
# ---------------------------------------------------------------------------


def _cgroup(tmp_path, cpu_max=None, memory_max=None) -> Path:
    cg = tmp_path / "libpod-fake.scope"
    cg.mkdir()
    if cpu_max is not None:
        (cg / "cpu.max").write_text(cpu_max + "\n")
    if memory_max is not None:
        (cg / "memory.max").write_text(memory_max + "\n")
    return cg


def test_cgroup_problem_none_when_limits_are_applied(tmp_path):
    cg = _cgroup(tmp_path, cpu_max="200000 100000", memory_max=str(512 * 1024 * 1024))
    assert PodmanEnvironment._cgroup_limit_problem(cg, "cpu", 2) is None
    assert PodmanEnvironment._cgroup_limit_problem(cg, "memory", 512) is None


def test_cgroup_problem_flags_a_silently_dropped_limit(tmp_path):
    # Rootless v1 / undelegated / runsc-ignored: the file reads "max".
    cg = _cgroup(tmp_path, cpu_max="max 100000", memory_max="max")
    assert "no quota" in PodmanEnvironment._cgroup_limit_problem(cg, "cpu", 2)
    assert "no limit" in PodmanEnvironment._cgroup_limit_problem(cg, "memory", 512)


def test_cgroup_problem_flags_a_wrong_value(tmp_path):
    cg = _cgroup(tmp_path, memory_max=str(64 * 1024 * 1024))
    assert "declares 512" in PodmanEnvironment._cgroup_limit_problem(cg, "memory", 512)


def test_cgroup_problem_tolerates_unit_rounding(tmp_path):
    # Compose "512M" parsed decimally lands ~2.4% under the binary value.
    cg = _cgroup(tmp_path, memory_max=str(512 * 1000 * 1000))
    assert PodmanEnvironment._cgroup_limit_problem(cg, "memory", 512) is None


def test_cgroup_problem_reports_unreadable_files(tmp_path):
    cg = _cgroup(tmp_path)  # neither file present
    assert "not readable" in PodmanEnvironment._cgroup_limit_problem(cg, "cpu", 2)


@pytest.mark.asyncio
async def test_limit_mode_fails_the_start_when_enforcement_is_absent(tmp_path):
    from pier.models.trial.config import ResourceMode

    env = PodmanEnvironment.__new__(PodmanEnvironment)
    env.environment_name = "unit"
    env._cpu_resource_mode = ResourceMode.LIMIT
    env._memory_resource_mode = ResourceMode.LIMIT
    env._resource_limit_value = lambda resource, auto_mode: (
        2 if resource == "cpu" else None
    )
    cg = _cgroup(tmp_path, cpu_max="max 100000")

    async def fake_dir():
        return cg

    env._main_cgroup_dir = fake_dir
    with pytest.raises(RuntimeError, match="not enforced"):
        await env._verify_resource_limits()


@pytest.mark.asyncio
async def test_auto_mode_warns_instead_of_failing(tmp_path):
    import logging

    from pier.models.trial.config import ResourceMode

    env = PodmanEnvironment.__new__(PodmanEnvironment)
    env.environment_name = "unit"
    env.logger = logging.getLogger("test_auto_mode_warns")
    env._cpu_resource_mode = ResourceMode.AUTO
    env._memory_resource_mode = ResourceMode.AUTO
    env._resource_limit_value = lambda resource, auto_mode: 512
    cg = _cgroup(tmp_path, cpu_max="max 100000", memory_max="max")

    async def fake_dir():
        return cg

    env._main_cgroup_dir = fake_dir
    await env._verify_resource_limits()  # must not raise


@pytest.mark.asyncio
async def test_no_declared_limits_skips_container_resolution():
    env = PodmanEnvironment.__new__(PodmanEnvironment)
    env._resource_limit_value = lambda resource, auto_mode: None

    async def boom():  # pragma: no cover
        raise AssertionError("must not resolve the container")

    env._main_cgroup_dir = boom
    await env._verify_resource_limits()


def test_cgroup_fallback_can_fail_closed(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setenv("PIER_PODMAN_CGROUP_FAIL_CLOSED", "1")
    caps = PodmanEnvironment.resource_capabilities()
    assert (caps.cpu_limit, caps.memory_limit) == (False, False)


# ---------------------------------------------------------------------------
# Exec fidelity -- programmatic execs must be pipe-clean, not pty-mangled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_programmatic_exec_disables_tty_allocation(monkeypatch, tmp_path):
    import asyncio as _asyncio

    spawned = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        spawned.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    env = PodmanEnvironment.__new__(PodmanEnvironment)
    env.environment_name = "unit"
    env.environment_dir = tmp_path
    env._compose_base = lambda: ["podman-compose"]
    env._compose_env = lambda: {}

    await env._run_docker_compose_command(["exec", "main", "bash", "-c", "true"])
    await env._run_docker_compose_command(["exec", "-T", "main", "true"])
    await env._run_docker_compose_command(["down"])

    assert spawned[0][:3] == ["podman-compose", "exec", "-T"]
    assert spawned[1].count("-T") == 1
    assert "-T" not in spawned[2]
