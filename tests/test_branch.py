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
from titanium.cli.branch import (
    _find_state_tar,
    _is_observation,
    _next_branch_name,
    _require_resumable_agent,
    _resolve_step,
    _steps,
)
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


def test_sealed_spec_fails_the_step_when_the_agent_dies_in_the_pipe(tmp_path):
    # tee is the last command in the pipe: without pipefail a crashed
    # agent (a bad flag above all) reads as rc 0 and grading proceeds.
    spec = _mini(tmp_path, resume=True).sealed_command_spec("do it", environment=None)
    command = spec.steps[-1].command
    assert command.startswith("set -o pipefail; ")
    assert "| tee" in command


def test_sealed_spec_default_has_no_resume(tmp_path):
    spec = _mini(tmp_path).sealed_command_spec("do the task", environment=None)
    assert "--resume" not in spec.steps[-1].command


def test_the_fork_is_the_default_install_source(tmp_path):
    run = _mini(tmp_path).install_spec().steps[1].run
    assert (
        "uv tool install git+https://github.com/omne-earth/mini-swe-agent@edge"
        in run
    )


def test_install_source_stays_overridable(tmp_path):
    source = "git+https://github.com/omne-earth/mini-swe-agent@abc123"
    run = _mini(tmp_path, install_source=source).install_spec().steps[1].run
    assert f"uv tool install {source}" in run
    # Upstream PyPI remains reachable, by explicit choice only.
    run = _mini(tmp_path, install_source="mini-swe-agent").install_spec().steps[1].run
    assert "uv tool install mini-swe-agent" in run


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


def _messages():
    """A three-step trajectory in the tool-message format, plus the two
    template messages the prune counts as observations but show does not
    count as steps."""
    def action(command, call):
        return {
            "role": "assistant",
            "content": "...",
            "extra": {"actions": [{"command": command, "tool_call_id": call}]},
        }

    def observation(call, rc, ts):
        return {
            "role": "tool",
            "tool_call_id": call,
            "content": "{}",
            "extra": {"returncode": rc, "timestamp": ts},
        }

    return [
        {"role": "system", "content": "be good"},
        {"role": "user", "content": "the task"},
        action("echo one", "call_aa1"),
        observation("call_aa1", 0, 100.0),
        action("echo two", "call_bb2"),
        observation("call_bb2", 0, 200.0),
        action("echo three", "call_cc3"),
        observation("call_cc3", 1, 300.0),
    ]


def test_steps_list_answered_actions_only():
    steps = _steps(_messages())
    assert [s["id"] for s in steps] == ["call_aa1", "call_bb2", "call_cc3"]
    assert [s["command"] for s in steps] == ["echo one", "echo two", "echo three"]
    assert steps[2]["returncode"] == 1
    # The index points at the observation itself: messages[:index+1] is
    # the trim that keeps the step and drops everything after it.
    assert _messages()[steps[1]["index"]]["tool_call_id"] == "call_bb2"


def test_resolve_step_matches_like_git():
    steps = _steps(_messages())
    assert _resolve_step(steps, "bb2")["id"] == "call_bb2"
    with pytest.raises(typer.BadParameter, match="no step id"):
        _resolve_step(steps, "zz9")
    with pytest.raises(typer.BadParameter, match="ambiguous"):
        _resolve_step(steps, "call_")


def test_resume_overrides_substitute_the_trajectory(tmp_path, monkeypatch):
    import tarfile

    parent_work = tmp_path / "parent" / "cella-env-x"
    parent_work.mkdir(parents=True)
    state_tar = _parent_state_tar(parent_work)
    (parent_work / "image-config.json").write_text(json.dumps({}))
    trimmed = tmp_path / "trimmed.json"
    trimmed.write_text('{"messages": []}')

    env = _cella_env(
        tmp_path,
        resume_state_tar=str(state_tar),
        resume_overrides={
            "./logs/agent/mini-swe-agent.trajectory.json": str(trimmed)
        },
    )
    monkeypatch.setattr(env, "preflight", lambda: None)
    env._paired = False
    env._start_blocking(force_build=False)

    with tarfile.open(env._base_tar) as tar:
        payload = tar.extractfile(
            "./logs/agent/mini-swe-agent.trajectory.json"
        ).read()
    assert payload == b'{"messages": []}'


def test_resume_overrides_refuse_a_path_the_tar_lacks(tmp_path, monkeypatch):
    from titanium.environments.cella.environment import CellaError

    parent_work = tmp_path / "parent" / "cella-env-x"
    parent_work.mkdir(parents=True)
    state_tar = _parent_state_tar(parent_work)
    (parent_work / "image-config.json").write_text(json.dumps({}))
    override = tmp_path / "x.json"
    override.write_text("{}")

    env = _cella_env(
        tmp_path,
        resume_state_tar=str(state_tar),
        resume_overrides={"./no/such/file.json": str(override)},
    )
    monkeypatch.setattr(env, "preflight", lambda: None)
    env._paired = False
    with pytest.raises(CellaError, match="absent from the parent's"):
        env._start_blocking(force_build=False)


def _trial_config(agent_name, **agent_kwargs):
    from titanium.models.trial.config import TrialConfig

    return TrialConfig.model_validate(
        {
            "trial_name": "t__ab",
            "trials_dir": "/tmp/x",
            "task": {"path": "/tmp/task"},
            "agent": {
                "name": agent_name,
                "model_name": "openai/gpt-5.5",
                "kwargs": agent_kwargs,
            },
            "environment": {"type": "cella"},
        }
    )


def _receipt_tar(tmp_path, requirements_line):
    import io
    import tarfile

    state_tar = tmp_path / "state-parent.tar"
    payload = (
        f"[tool]\nrequirements = [{requirements_line}]\n"
        'entrypoints = [\n { name = "mini", install-path = "/home/t/.local/bin/mini" },\n]\n'
    ).encode()
    with tarfile.open(state_tar, "w") as tar:
        info = tarfile.TarInfo(
            "./home/titanium/.local/share/uv/tools/mini-swe-agent/uv-receipt.toml"
        )
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return state_tar


def test_branch_refuses_a_registry_installed_parent(tmp_path):
    tar = _receipt_tar(tmp_path, '{ name = "mini-swe-agent" }')
    with pytest.raises(typer.BadParameter, match="no --resume"):
        _require_resumable_agent(_trial_config("mini-swe-agent"), tar)


def test_branch_accepts_a_git_installed_parent_and_other_agents(tmp_path):
    tar = _receipt_tar(
        tmp_path,
        '{ name = "mini-swe-agent", git = "https://github.com/omne-earth/mini-swe-agent?rev=abc" }',
    )
    _require_resumable_agent(_trial_config("mini-swe-agent"), tar)
    # A non-mini agent never opens the tar: an oracle parent branches
    # whatever its disk holds.
    _require_resumable_agent(_trial_config("oracle"), tmp_path / "absent.tar")


def test_branch_names_extend_the_lineage(tmp_path):
    assert _next_branch_name("foo__ab", tmp_path) == "foo__ab-branch-1"
    (tmp_path / "foo__ab-branch-1").mkdir()
    # A branch of a branch stays in the same series.
    assert _next_branch_name("foo__ab-branch-1", tmp_path) == "foo__ab-branch-2"


def test_resumed_state_reemits_image_config(tmp_path, monkeypatch):
    # A leg must write its own image-config.json beside its state tars,
    # so a branch *of the leg* finds it -- otherwise branch-of-a-branch
    # fails the "predates branch support" precondition.
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

    emitted = env._work / "image-config.json"
    assert emitted.is_file()
    assert json.loads(emitted.read_text()) == {"WorkingDir": "/app"}


# --------------------------------------------------- CLI: --note / --at+note


def _cli_parent_dir(tmp_path, messages):
    """A minimal parent trial dir branch_command can resume from: config,
    a state tar carrying the trajectory, and the image config beside it.
    Agent is oracle, so the resumable-agent receipt gate is skipped."""
    import io
    import tarfile

    trial = tmp_path / "job" / "t__ab"
    env_dir = trial / "cella-env-x"
    env_dir.mkdir(parents=True)
    traj = json.dumps(
        {"messages": messages, "trajectory_format": "mini-swe-agent-1.1"}
    ).encode()
    with tarfile.open(env_dir / "state-parent.tar", "w") as tar:
        info = tarfile.TarInfo("./logs/agent/mini-swe-agent.trajectory.json")
        info.size = len(traj)
        tar.addfile(info, io.BytesIO(traj))
    (env_dir / "image-config.json").write_text(json.dumps({}))
    (trial / "config.json").write_text(_trial_config("oracle").model_dump_json())
    return trial


def _run_branch(monkeypatch, args):
    from typer.testing import CliRunner

    import titanium.cli.branch as branch_mod

    # Stop before the trial runs: close the coroutine the callback hands to
    # run_async so the trajectory is rewritten but nothing boots.
    def _noop(coro):
        coro.close()

    monkeypatch.setattr(branch_mod, "run_async", _noop)
    return CliRunner().invoke(branch_mod.branch_app, args)


def test_note_appends_a_trailing_user_message(tmp_path, monkeypatch):
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "did x"},
        {"role": "user", "content": "obs"},
    ]
    trial = _cli_parent_dir(tmp_path, messages)
    result = _run_branch(monkeypatch, ["-p", str(trial), "--note", "remember X"])
    assert result.exit_code == 0, result.output

    leg = json.loads(
        (tmp_path / "job" / "t__ab-branch-1" / "trimmed-trajectory.json").read_text()
    )["messages"]
    # The note is the last message, a user-role observation (so the fork's
    # prune keeps it), and nothing before it changed.
    assert leg[:-1] == messages
    assert leg[-1] == {"role": "user", "content": "remember X"}
    assert _is_observation(leg[-1])


def test_at_then_note_trims_first_then_appends(tmp_path, monkeypatch):
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "step 1"},
        {"role": "tool", "content": "obs 1", "tool_call_id": "call_aaa"},
        {"role": "assistant", "content": "step 2"},
        {"role": "tool", "content": "obs 2", "tool_call_id": "call_bbb"},
    ]
    trial = _cli_parent_dir(tmp_path, messages)
    result = _run_branch(
        monkeypatch, ["-p", str(trial), "--at", "call_aaa", "--note", "hi"]
    )
    assert result.exit_code == 0, result.output

    leg = json.loads(
        (tmp_path / "job" / "t__ab-branch-1" / "trimmed-trajectory.json").read_text()
    )["messages"]
    # Trimmed to the first observation (index 3), then the note appended.
    assert [m.get("content") for m in leg] == ["s", "task", "step 1", "obs 1", "hi"]
    assert leg[-1] == {"role": "user", "content": "hi"}
