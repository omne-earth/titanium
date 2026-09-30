"""``titanium branch``: continue a timed-out cella trial as a new leg.

A parent trial run with ``--env cella --on-completion pause`` (or any run
whose extracted state tar survives in its ``cella-env-*/`` work dir) can
be branched: a new machine bakes from the parent's state tar (the
verifier pattern -- new life from evidence, never a mutated machine),
the agent resumes its own on-disk trajectory pruned back to the last
observation, and the leg runs under a fresh timeout. To the agent it
reads as one longer run, gap in wall-clock notwithstanding.

Cella-only by design: no other rung leaves a whole-tree state tar to
bake the next leg from.
"""

import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from typer import Option

from titanium.cli.utils import run_async
from titanium.models.environment_type import EnvironmentType
from titanium.models.trial.config import TrialConfig

console = Console()


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


def branch_command(
    trial_path: Annotated[
        Path,
        Option(
            "-p",
            "--trial-path",
            help="Path to the parent trial directory (containing config.json "
            "and the cella-env-* work dir with its state tar).",
        ),
    ],
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
    from titanium.trial.trial import Trial

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

    trials_dir = trial_dir.parent
    leg_name = _next_branch_name(config.trial_name, trials_dir)

    config.trials_dir = trials_dir
    config.trial_name = leg_name
    config.environment.kwargs["resume_state_tar"] = str(state_tar)
    # The agent continues its own on-disk trajectory (the parent leg's
    # file rides in the state tar), pruned back to the last observation.
    config.agent.kwargs["resume"] = True
    if agent_timeout_multiplier is not None:
        config.agent_timeout_multiplier = agent_timeout_multiplier

    console.print(
        f"Branching [bold]{trial_dir.name}[/bold] -> [bold green]{leg_name}[/bold green] "
        f"from {state_tar.name}"
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
