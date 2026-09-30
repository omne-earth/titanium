"""The branch leg: `titanium branch` continues a paused cella trial.

Covers the three seams: the agent's resume flag (sealed spec), the
environment's resume_state_tar (evidence in, no build), and the CLI's
parent-dir resolution.
"""

import json
from pathlib import Path

import pytest
import typer

from titanium.agents.installed.mini_swe_agent import MiniSweAgent
from titanium.cli.branch import _find_state_tar, _next_branch_name
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.paths import TrialPaths


# ------------------------------------------------------------- agent seam


@pytest.fixture(autouse=True)
def _mswea_key(monkeypatch):
    monkeypatch.setenv("MSWEA_API_KEY", "test-key")


def _mini(tmp_path, **kw) -> MiniSweAgent:
    return MiniSweAgent(logs_dir=tmp_path, model_name="openai/gpt-5.5", **kw)


def test_sealed_spec_resumes_the_on_disk_trajectory(tmp_path):
    agent = _mini(tmp_path, resume=True)
    spec = agent.sealed_command_spec("do the task", environment=None)
    command = spec.steps[-1].command
    assert "--resume=/logs/agent/mini-swe-agent.trajectory.json" in command
    assert "--output=/logs/agent/mini-swe-agent.trajectory.json" in command


def test_sealed_spec_default_has_no_resume(tmp_path):
    spec = _mini(tmp_path).sealed_command_spec("do the task", environment=None)
    assert "--resume" not in spec.steps[-1].command


def test_install_source_overrides_the_pypi_package(tmp_path):
    source = "git+https://github.com/omne-earth/mini-swe-agent@feat/cella-branch"
    run = _mini(tmp_path, install_source=source).install_spec().steps[1].run
    assert f"uv tool install {source}" in run
    assert "uv tool install mini-swe-agent" not in run


# ------------------------------------------------------- environment seam


def _cella_env(tmp_path, **kw):
    from titanium.environments.cella.environment import CellaEnvironment

    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return CellaEnvironment(
        environment_dir=environment_dir,
        environment_name="probe",
        session_id="probe__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
        **kw,
    )


def _parent_state_tar(parent_work):
    """A parent state tar: the agent's evidence plus the orchestrator's
    completion latch a resumed leg must not inherit."""
    import io
    import tarfile

    state_tar = parent_work / "state-parent.tar"
    with tarfile.open(state_tar, "w") as tar:
        for name, payload in [
            ("./app/branch-log.txt", b"leg\n"),
            ("./logs/agent/mini-swe-agent.trajectory.json", b"{}"),
            ("./titanium/result/done", b""),
            ("./titanium/result/agent/rc", b"0\n"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return state_tar


def test_resumed_state_seeds_base_tar_and_scrubs_the_latch(tmp_path, monkeypatch):
    import tarfile

    parent_work = tmp_path / "parent" / "cella-env-x"
    parent_work.mkdir(parents=True)
    state_tar = _parent_state_tar(parent_work)
    (parent_work / "image-config.json").write_text(
        json.dumps({"WorkingDir": "/app"})
    )

    env = _cella_env(tmp_path, resume_state_tar=str(state_tar))
    monkeypatch.setattr(env, "preflight", lambda: None)
    env._paired = False
    env._start_blocking(force_build=False)

    assert env._base_tar is not None and env._base_tar != state_tar
    with tarfile.open(env._base_tar) as tar:
        names = tar.getnames()
    # The evidence rides along -- the trajectory above all.
    assert "./app/branch-log.txt" in names
    assert "./logs/agent/mini-swe-agent.trajectory.json" in names
    # The parent's completion latch does not: resumed verbatim, the
    # guest would see a finished run and reset without a single phase.
    assert not any(n.startswith("./titanium/result") for n in names)
    assert env._image_config == {"WorkingDir": "/app"}


def test_resume_refuses_a_tar_without_image_config(tmp_path, monkeypatch):
    from titanium.environments.cella.environment import CellaError

    parent_work = tmp_path / "parent" / "cella-env-x"
    parent_work.mkdir(parents=True)
    state_tar = _parent_state_tar(parent_work)

    env = _cella_env(tmp_path, resume_state_tar=str(state_tar))
    monkeypatch.setattr(env, "preflight", lambda: None)
    env._paired = False
    with pytest.raises(CellaError, match="image-config.json"):
        env._start_blocking(force_build=False)


# --------------------------------------------------------------- CLI seam


def test_find_state_tar_skips_the_pre_boot_base(tmp_path):
    work = tmp_path / "cella-env-a"
    work.mkdir()
    (work / "state-0000.tar").write_bytes(b"base")
    member = work / "state-member.tar"
    member.write_bytes(b"member")
    assert _find_state_tar(tmp_path) == member


def test_find_state_tar_refuses_an_unextracted_parent(tmp_path):
    (tmp_path / "cella-env-a").mkdir()
    with pytest.raises(typer.BadParameter, match="no member state tar"):
        _find_state_tar(tmp_path)


def test_branch_names_extend_the_lineage(tmp_path):
    assert _next_branch_name("foo__ab", tmp_path) == "foo__ab-branch-1"
    (tmp_path / "foo__ab-branch-1").mkdir()
    # A branch of a branch stays in the same series.
    assert _next_branch_name("foo__ab-branch-1", tmp_path) == "foo__ab-branch-2"
