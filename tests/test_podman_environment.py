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
