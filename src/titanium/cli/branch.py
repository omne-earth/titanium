"""``titanium branch``: continue a timed-out cella trial as a new leg.

A parent trial run with ``--env cella --on-completion pause`` (or any run
whose extracted state tar survives in its ``cella-env-*/`` work dir) can
be branched: a new machine bakes from the parent's state tar (the
verifier pattern -- new life from evidence, never a mutated machine),
the agent resumes its own on-disk trajectory pruned back to the last
observation, and the leg runs under a fresh timeout. To the agent it
reads as one longer run, gap in wall-clock notwithstanding.

``titanium branch show`` lists the parent's steps git-log style, keyed
by each observation's tool_call_id, and ``--at <id>`` trims the resumed
trajectory back to that observation instead of the last one. The trim
rewinds the agent's memory only: the disk in the state tar is from the
end of the parent leg, and the leg resumes on it as-is.

Cella-only by design: no other rung leaves a whole-tree state tar to
bake the next leg from.
"""

import datetime
import json
import re
import tarfile
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from typer import Option

from titanium.cli.utils import run_async
from titanium.models.environment_type import EnvironmentType
from titanium.models.trial.config import TrialConfig

console = Console()

branch_app = typer.Typer(invoke_without_command=True, no_args_is_help=False)

# Where the agent's own trajectory lives on the guest disk: the file the
# patched mini-swe-agent --resume reads and keeps appending to.
TRAJECTORY_GUEST_PATH = "./logs/agent/mini-swe-agent.trajectory.json"


def _find_state_tar(trial_dir: Path) -> Path:
    """The parent's member state tar: the newest ``state-*.tar`` that is
    not the pre-boot base (``state-0000.tar``), searched across the
    trial's ``cella-env-*`` work dirs."""
    candidates = [
        tar
        for work in sorted(trial_dir.glob("cella-env-*"))
        for tar in work.glob("state-*.tar")
        if tar.name != "state-0000.tar"
    ]
    if not candidates:
        raise typer.BadParameter(
            f"no member state tar under {trial_dir}/cella-env-*/: the parent "
            "trial never reached its state extract, so there is no evidence "
            "to branch from"
        )
    return max(candidates, key=lambda tar: tar.stat().st_mtime)


def _next_branch_name(parent_name: str, trials_dir: Path) -> str:
    """``<parent>-branch-N`` for the first free N, legs numbered from 1.
    Branching a branch extends the same lineage: ``foo-branch-2``, not
    ``foo-branch-1-branch-1``."""
    base = re.sub(r"-branch-\d+$", "", parent_name)
    n = 1
    while (trials_dir / f"{base}-branch-{n}").exists():
        n += 1
    return f"{base}-branch-{n}"


def _read_trajectory(state_tar: Path) -> dict[str, Any]:
    """The parent's trajectory, read from the state tar itself: the copy
    the leg will actually resume, not a host-side mirror."""
    with tarfile.open(state_tar, "r:") as tar:
        try:
            member = tar.extractfile(TRAJECTORY_GUEST_PATH)
        except KeyError:
            member = None
        if member is None:
            raise typer.BadParameter(
                f"no trajectory at {TRAJECTORY_GUEST_PATH} in {state_tar}: "
                "the parent leg never wrote one, so there is no step list"
            )
        return json.loads(member.read())


def _is_observation(message: dict[str, Any]) -> bool:
    """Mirrors the fork's resume prune: an observation in any message
    format (user / tool / response API)."""
    return message.get("type") == "function_call_output" or message.get("role") in (
        "user",
        "tool",
    )


def _steps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per observation that answers an agent action, keyed by
    the id already in the file (tool_call_id / call_id). The system
    prompt and the task instruction are observations to the prune but
    not steps: they answer nothing."""
    steps: list[dict[str, Any]] = []
    last_action: dict[str, Any] | None = None
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" or message.get("type") == "function_call":
            last_action = message
            continue
        if not _is_observation(message) or last_action is None:
            continue
        extra = message.get("extra") or {}
        actions = (last_action.get("extra") or {}).get("actions") or [{}]
        steps.append(
            {
                "id": message.get("tool_call_id") or message.get("call_id") or f"#{len(steps) + 1}",
                "index": index,
                "timestamp": extra.get("timestamp"),
                "returncode": extra.get("returncode"),
                "command": actions[0].get("command", "?"),
            }
        )
    return steps


def _resolve_step(steps: list[dict[str, Any]], at: str) -> dict[str, Any]:
    """Git-style resolution: any unambiguous substring of a step id."""
    matches = [s for s in steps if at in s["id"]]
    if not matches:
        raise typer.BadParameter(
            f"--at {at!r} matches no step id; `titanium branch show` lists them"
        )
    if len(matches) > 1:
        raise typer.BadParameter(
            f"--at {at!r} is ambiguous: "
            + ", ".join(s["id"] for s in matches)
        )
    return matches[0]


_UV_RECEIPT_SUFFIX = "/.local/share/uv/tools/mini-swe-agent/uv-receipt.toml"


def _require_resumable_agent(config: TrialConfig, state_tar: Path) -> None:
    """The leg reuses the parent's baked disk, binary and all. Upstream
    PyPI mini-swe-agent has no --resume: the leg's agent would die on
    the flag and the verifier would re-grade the parent's finished disk
    -- a hollow PASS. The ground truth is the uv install receipt on
    that disk (the config's install_source only says what was asked
    for), so refuse when the receipt shows a registry install rather
    than a git build. A parent without a receipt is left alone."""
    if config.agent.name != "mini-swe-agent":
        return
    receipt = None
    with tarfile.open(state_tar, "r:") as tar:
        for member in tar:
            if member.name.endswith(_UV_RECEIPT_SUFFIX) and member.isfile():
                receipt = tar.extractfile(member).read().decode(errors="replace")
                break
    if receipt is not None and "git" not in receipt.split("entrypoints")[0]:
        raise typer.BadParameter(
            "the parent's disk carries a registry-installed mini-swe-agent, "
            "which has no --resume: the leg cannot continue its trajectory. "
            "Re-run the parent with the fork (the default install source), "
            "not an upstream PyPI install_source"
        )


_TRIAL_PATH_OPTION = Option(
    "-p",
    "--trial-path",
    help="Path to the parent trial directory (containing config.json "
    "and the cella-env-* work dir with its state tar).",
)


def _parent_state_tar(trial_path: Path) -> tuple[TrialConfig, Path]:
    trial_dir = Path(trial_path)
    config_path = trial_dir / "config.json"
    if not config_path.is_file():
        raise typer.BadParameter(f"no config.json in {trial_dir}")

    config = TrialConfig.model_validate_json(config_path.read_text())
    if config.environment.type is not EnvironmentType.CELLA:
        raise typer.BadParameter(
            "branch is cella-only: the leg bakes from the parent's whole-tree "
            f"state tar, which the '{config.environment.type.value if config.environment.type else 'custom'}' "
            "environment does not leave behind"
        )

    state_tar = _find_state_tar(trial_dir)
    if not (state_tar.parent / "image-config.json").is_file():
        raise typer.BadParameter(
            f"no image-config.json beside {state_tar}: the parent run "
            "predates branch support; re-run it first"
        )
    return config, state_tar


@branch_app.command(name="show")
def show_command(
    trial_path: Annotated[Path, _TRIAL_PATH_OPTION],
) -> None:
    """List the parent leg's steps, one line per observation."""
    _, state_tar = _parent_state_tar(trial_path)
    steps = _steps(_read_trajectory(state_tar).get("messages", []))
    if not steps:
        console.print("no steps: the trajectory holds no answered action yet")
        return
    for step in steps:
        ts = step["timestamp"]
        when = (
            datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            if ts is not None
            else "??:??:??"
        )
        rc = step["returncode"]
        command = " ".join(str(step["command"]).split())
        # Plain echo, not rich: one step per line whatever the terminal
        # width, so `awk '{print $1}'` on a pipe stays honest.
        typer.echo(f"{step['id']}  {when}  rc={rc!s:<4} {command[:72]}")


@branch_app.callback()
def branch_command(
    ctx: typer.Context,
    trial_path: Annotated[Path | None, _TRIAL_PATH_OPTION] = None,
    at: Annotated[
        str | None,
        Option(
            "--at",
            help="Trim the resumed trajectory back to this step (any "
            "unambiguous substring of a step id from `branch show`). "
            "Default: the parent's last observation. The disk is not "
            "rewound: it stays as the parent leg left it.",
            show_default=False,
        ),
    ] = None,
    agent_timeout_multiplier: Annotated[
        float | None,
        Option(
            "--agent-timeout-multiplier",
            help="Agent timeout multiplier for the new leg (default: the "
            "parent's own).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Continue a cella trial from its preserved state, as a new leg."""
    if ctx.invoked_subcommand is not None:
        return
    if trial_path is None:
        raise typer.BadParameter("-p/--trial-path is required")

    from titanium.trial.trial import Trial

    trial_dir = Path(trial_path)
    config, state_tar = _parent_state_tar(trial_dir)

    _require_resumable_agent(config, state_tar)

    trials_dir = trial_dir.parent
    leg_name = _next_branch_name(config.trial_name, trials_dir)
    leg_dir = trials_dir / leg_name

    config.trials_dir = trials_dir
    config.trial_name = leg_name
    config.environment.kwargs["resume_state_tar"] = str(state_tar)
    # The agent continues its own on-disk trajectory (the parent leg's
    # file rides in the state tar), pruned back to the last observation.
    config.agent.kwargs["resume"] = True
    if agent_timeout_multiplier is not None:
        config.agent_timeout_multiplier = agent_timeout_multiplier

    trimmed_to = ""
    if at is not None:
        trajectory = _read_trajectory(state_tar)
        messages = trajectory.get("messages", [])
        step = _resolve_step(_steps(messages), at)
        trajectory["messages"] = messages[: step["index"] + 1]
        trimmed = leg_dir / "trimmed-trajectory.json"
        leg_dir.mkdir(parents=True, exist_ok=True)
        trimmed.write_text(json.dumps(trajectory))
        # The environment substitutes the trimmed file for the tar's own
        # copy while seeding the leg's base tar; the guest-side resume
        # prune then finds a trajectory already ending at an observation.
        config.environment.kwargs["resume_overrides"] = {
            TRAJECTORY_GUEST_PATH: str(trimmed)
        }
        trimmed_to = f" at step {step['id']}"
        console.print(
            "[yellow]note:[/yellow] the trim rewinds the agent's memory, "
            "not the disk -- the state tar is from the end of the parent "
            "leg, and later steps' effects are still on it"
        )

    console.print(
        f"Branching [bold]{trial_dir.name}[/bold] -> [bold green]{leg_name}[/bold green] "
        f"from {state_tar.name}{trimmed_to}"
    )

    async def _run() -> None:
        trial = await Trial.create(config)
        result = await trial.run()
        outcome = "failed"
        if result.exception_info is None:
            outcome = "completed"
        console.print(
            f"Leg {outcome}: results in [bold]{trials_dir / leg_name}[/bold]"
        )

    run_async(_run())
