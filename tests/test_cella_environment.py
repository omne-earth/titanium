"""Unit tests for the cella environment plumbing.

First resident: the ``allow_internet`` -> network topology mapping.
The flag is harbor's knob and defines which machine gets created, not
a posture inside one; these tests pin the mapping as total and closed.
"""

from titanium.environments.cella.environment import (
    NetworkTopology,
    network_topology,
)


def test_allow_internet_false_is_no_nic_at_all():
    topology = network_topology(False)
    assert topology == NetworkTopology(net="none", open_gateway=False, judged=False)


def test_allow_internet_true_is_a_judged_world_nic():
    topology = network_topology(True)
    assert topology == NetworkTopology(net="world", open_gateway=True, judged=True)


def test_the_judgment_machinery_exists_exactly_when_a_nic_does():
    # No third topology: a machine either has no network and no judge,
    # or a world nic whose every crossing is judged. Never a nic with
    # no judge, never a judge with nothing to judge.
    for allow_internet in (False, True):
        topology = network_topology(allow_internet)
        assert topology.judged == (topology.net != "none")
        assert topology.open_gateway == topology.judged


# ---------------------------------------------------------------------------
# The environment class: pure parts, no cella and no podman
# ---------------------------------------------------------------------------

import tarfile
from pathlib import Path

import pytest

from titanium.environments.cella.environment import (
    CellaEnvironment,
    _flavor_name,
)
from titanium.environments.cella.flavor import validate_flavor_name
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.paths import TrialPaths


def _make_env(tmp_path, allow_internet=False):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM debian:12-slim\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return CellaEnvironment(
        environment_dir=environment_dir,
        environment_name="cella-task",
        session_id="cella-task__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=allow_internet),
    )


def test_the_environment_registers_and_declares_itself(tmp_path):
    env = _make_env(tmp_path)
    assert env.type() == "cella"
    assert env.capabilities.disable_internet
    assert env.capabilities.preinstall_agents
    assert not env.capabilities.mounted
    assert env.resource_capabilities().memory_limit
    assert not env.resource_capabilities().cpu_limit


def test_the_www_leg_refuses_loudly_for_now(tmp_path):
    with pytest.raises(NotImplementedError, match="-www leg"):
        _make_env(tmp_path, allow_internet=True)


def test_flavor_names_are_safe_and_distinct():
    name = _flavor_name("Task__Trial!weird name", 3)
    # The machine-name contract is the strict one: lowercase letters,
    # digits, and dashes only.
    assert name == "titanium-task-trial-weird-name-c0003"
    assert validate_flavor_name(name) == name
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert _flavor_name("t", 1) != _flavor_name("t", 2)


@pytest.mark.asyncio
async def test_uploads_queue_and_last_write_wins(tmp_path):
    env = _make_env(tmp_path)
    source = tmp_path / "solve.sh"
    source.write_text("echo one")
    source.chmod(0o755)
    await env.upload_file(source, "/solution/solve.sh")
    source.write_text("echo two")
    await env.upload_file(source, "/solution/solve.sh")
    assert len(env._pending) == 1
    entry = env._pending[0]
    assert entry.contents == b"echo two"
    assert entry.mode == 0o755


@pytest.mark.asyncio
async def test_upload_dir_walks_and_preserves_links(tmp_path):
    env = _make_env(tmp_path)
    tree = tmp_path / "tests-dir"
    (tree / "sub").mkdir(parents=True)
    (tree / "test.sh").write_text("#!/bin/bash\n")
    (tree / "sub" / "helper.py").write_text("x = 1\n")
    (tree / "link.sh").symlink_to("test.sh")
    await env.upload_dir(tree, "/tests")
    paths = {entry.path for entry in env._pending}
    assert paths == {"/tests/test.sh", "/tests/sub/helper.py", "/tests/link.sh"}
    link = next(e for e in env._pending if e.path == "/tests/link.sh")
    assert link.target == "test.sh"


def test_job_files_render_the_cycle(tmp_path):
    env = _make_env(tmp_path)
    env._image_config = {"WorkingDir": "/app", "Env": ["PATH=/usr/bin"]}
    files = env._job_files("bash solve.sh", None, {"K": "a b"}, None)
    by_path = {entry.path: entry for entry in files}
    command = by_path["/titanium/command.sh"].contents.decode()
    assert command == "bash solve.sh\n"
    job = by_path["/titanium/job.sh"].contents.decode()
    assert "cd /app" in job
    assert "export PATH=/usr/bin" in job
    assert "export K='a b'" in job
    # rc is written last, after the outputs are synced: its presence
    # is the host's completion signal, so it must prove the rest.
    assert "echo $rc > /titanium/result/rc" in job
    assert job.index("sync") < job.index("echo $rc")
    assert "systemctl poweroff" in job
    assert "runuser" not in job  # root by default
    unit = by_path["/etc/systemd/system/titanium-exec.service"].contents.decode()
    assert "ExecStart=/bin/bash /titanium/job.sh" in unit
    wants = by_path["/etc/systemd/system/multi-user.target.wants/titanium-exec.service"]
    assert wants.target == "../titanium-exec.service"


def test_job_files_drop_privilege_when_asked(tmp_path):
    env = _make_env(tmp_path)
    files = env._job_files("id", None, None, "agent")
    job = next(e for e in files if e.path == "/titanium/job.sh").contents.decode()
    assert "runuser -u agent -- bash /titanium/command.sh" in job


def _state_tar(tmp_path, entries) -> Path:
    path = tmp_path / "state.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo("./" + name)
            info.size = len(data)
            import io

            archive.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.asyncio
async def test_downloads_read_the_evidence_tree(tmp_path):
    env = _make_env(tmp_path)
    env._base_tar = _state_tar(
        tmp_path,
        [
            ("app/report.json", b"{}"),
            ("logs/verifier/reward.txt", b"1\n"),
            ("logs/verifier/sub/detail.txt", b"d"),
        ],
    )
    target = tmp_path / "out" / "report.json"
    await env.download_file("/app/report.json", target)
    assert target.read_bytes() == b"{}"
    with pytest.raises(FileNotFoundError):
        await env.download_file("/absent", tmp_path / "x")

    logs = tmp_path / "logs-out"
    await env.download_dir("/logs/verifier", logs)
    assert (logs / "reward.txt").read_bytes() == b"1\n"
    assert (logs / "sub" / "detail.txt").read_bytes() == b"d"
    # An absent directory downloads as empty, not as an error.
    empty = tmp_path / "empty-out"
    await env.download_dir("/nothing", empty)
    assert empty.is_dir() and not any(empty.iterdir())
