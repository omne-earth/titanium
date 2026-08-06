"""Discovery, validation, and reporting logic for `make proof-gvisor-env`.

`scripts/proof-gvisor-env.sh` orchestrates the actual `pier run` invocations
and host commands; it shells out to this module's CLI for everything that
needs real parsing or decision logic (job/trial discovery, result.json
validation, trajectory discovery, checksum manifests, and the host-side
runtime inspection + exact-project cleanup). Keeping that logic here, in
plain importable functions, is what makes it unit-testable without spinning
up Docker, gVisor, or a real agent -- see tests/test_gvisor_proof.py.

Nothing here trusts text an agent wrote inside the sandbox. Job/trial
validation reads only Pier's own result.json (written by the trusted host
process, never by the agent). Runtime inspection and cleanup call the same
host-side, `docker inspect`/`docker ps`-based primitives that
GVisorEnvironment itself uses for verification and fallback teardown
(`pier.environments.gvisor.runtime`), re-deriving the facts independently
from the host rather than trusting that a green trial implies gVisor was
actually used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ProofError(RuntimeError):
    """A proof-harness check failed.

    Caught only at the CLI boundary (`main`) and reported with a nonzero
    exit code and a message on stderr -- never swallowed, never downgraded
    to a warning.
    """


# ---------------------------------------------------------------------------
# Job / trial discovery
# ---------------------------------------------------------------------------


def resolve_job_dir(jobs_dir: Path, job_name: str) -> Path:
    """Return the job directory for an explicit, already-known job name.

    The harness always passes `--job-name` explicitly to `pier run`, so
    locating the job never depends on scanning *jobs_dir* for the newest
    timestamp -- that would be racy against concurrent jobs and silently
    wrong if a previous proof run's directory was left behind.
    """
    job_dir = jobs_dir / job_name
    if not job_dir.is_dir():
        raise ProofError(
            f"Expected job directory {job_dir} (job name {job_name!r}) does "
            "not exist. `pier run` may have failed before creating it."
        )
    if not (job_dir / "result.json").exists():
        raise ProofError(
            f"Job directory {job_dir} exists but has no result.json; the job "
            "likely crashed before finishing."
        )
    return job_dir


def find_trial_dir(job_dir: Path, task_name: str) -> Path:
    """Find the single trial directory for *task_name* under *job_dir*.

    Trial directories are named ``f"{task_name}__{random_suffix}"``. Matching
    on the exact prefix plus the ``__`` separator avoids matching a task name
    that is itself a prefix of another task's name.
    """
    prefix = f"{task_name}__"
    candidates = sorted(
        p for p in job_dir.iterdir() if p.is_dir() and p.name.startswith(prefix)
    )
    if not candidates:
        raise ProofError(
            f"No trial directory matching {prefix!r}* found under {job_dir}."
        )
    if len(candidates) > 1:
        names = [c.name for c in candidates]
        raise ProofError(
            f"Expected exactly one trial directory matching {prefix!r}* under "
            f"{job_dir}, found {len(candidates)}: {names}."
        )
    return candidates[0]


def load_json(path: Path) -> Any:
    if not path.exists():
        raise ProofError(f"Expected file {path} does not exist.")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ProofError(f"{path} is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Semantic result validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobValidationReport:
    evals_key: str
    n_completed_trials: int
    n_errored_trials: int
    n_cancelled_trials: int
    mean_reward: float


def validate_job_result(
    job_result: dict[str, Any],
    *,
    min_mean_reward: float = 1.0,
) -> JobValidationReport:
    """Validate a job's result.json against the proof's success criteria.

    Checks, in order: zero errored trials, zero cancelled trials, at least
    one completed trial, exactly one eval group, and a reward mean that
    indicates success. Raises :class:`ProofError` naming exactly which check
    failed.
    """
    stats = job_result.get("stats") or {}

    n_errored = stats.get("n_errored_trials", 0)
    if n_errored:
        raise ProofError(f"Job reports {n_errored} errored trial(s); expected 0.")

    n_cancelled = stats.get("n_cancelled_trials", 0)
    if n_cancelled:
        raise ProofError(f"Job reports {n_cancelled} cancelled trial(s); expected 0.")

    n_completed = stats.get("n_completed_trials", 0)
    if n_completed < 1:
        raise ProofError(
            f"Job reports {n_completed} completed trial(s); expected at least 1."
        )

    evals = stats.get("evals") or {}
    if not evals:
        raise ProofError(
            "Job reports no eval groups; nothing to validate reward against."
        )
    if len(evals) != 1:
        raise ProofError(
            f"Expected exactly one eval group, found {len(evals)}: {sorted(evals)}."
        )
    ((evals_key, eval_stats),) = evals.items()

    metrics = eval_stats.get("metrics") or []
    mean_reward = None
    for metric in metrics:
        if isinstance(metric, dict) and "mean" in metric:
            mean_reward = metric["mean"]
            break
    if mean_reward is None:
        raise ProofError(
            f"Eval group {evals_key!r} has no 'mean' metric in {metrics!r}."
        )
    if mean_reward < min_mean_reward:
        raise ProofError(
            f"Eval group {evals_key!r} reports mean reward {mean_reward}, "
            f"below the required {min_mean_reward}."
        )

    return JobValidationReport(
        evals_key=evals_key,
        n_completed_trials=n_completed,
        n_errored_trials=n_errored,
        n_cancelled_trials=n_cancelled,
        mean_reward=mean_reward,
    )


def validate_trial_errors(
    trial_result: dict[str, Any],
    *,
    expect_error_substring: str | None = None,
) -> None:
    """Validate a single trial's exception state.

    With *expect_error_substring* set, this instead asserts the trial *did*
    fail with a matching exception message -- used for the
    Podman-must-fail-explicitly negative test, where an errored trial with
    the right message is the success condition.
    """
    trial_name = trial_result.get("trial_name", "<unknown trial>")
    exception_info = trial_result.get("exception_info")

    if expect_error_substring is None:
        if exception_info is not None:
            raise ProofError(
                f"Trial {trial_name} recorded an exception: "
                f"{exception_info.get('exception_type')}: "
                f"{exception_info.get('exception_message')}"
            )
        return

    if exception_info is None:
        raise ProofError(
            f"Trial {trial_name} completed without error, but was expected "
            f"to fail with a message containing {expect_error_substring!r}."
        )
    message = exception_info.get("exception_message", "")
    if expect_error_substring.lower() not in message.lower():
        raise ProofError(
            f"Trial {trial_name} failed as expected, but its exception "
            f"message does not mention {expect_error_substring!r}: {message}"
        )


# ---------------------------------------------------------------------------
# Trajectory discovery
# ---------------------------------------------------------------------------


def validate_negative_test(
    log_text: str,
    *,
    exit_code: int,
    must_contain: tuple[str, ...],
) -> None:
    """Validate a deliberately-failing ``pier run`` invocation.

    An unsupported environment engine (e.g. Podman) is rejected synchronously
    while ``GVisorEnvironment.__init__`` resolves the engine CLI, before any
    trial directory or ``result.json`` exists to inspect -- Pier does not
    catch environment-construction failures per-trial, so ``pier run`` itself
    exits nonzero and prints the exception. This checks that outcome
    directly: a nonzero exit code, and every string in *must_contain* present
    in the captured output (case-insensitive).
    """
    if exit_code == 0:
        raise ProofError(
            "Expected `pier run` to exit nonzero (the engine should be "
            f"rejected before any container is created), got exit code "
            f"{exit_code}."
        )
    lowered = log_text.lower()
    missing = [needle for needle in must_contain if needle.lower() not in lowered]
    if missing:
        raise ProofError(
            f"`pier run` failed as expected (exit {exit_code}), but its "
            f"output is missing expected text: {missing}."
        )


def find_trajectory(trial_dir: Path) -> Path:
    """Return the ATIF trajectory a real, model-backed agent writes.

    Installed agents like Claude Code write ``<trial_dir>/agent/trajectory.json``
    after the run (see ``populate_context_post_run``). Oracle does not
    produce one at all, so its absence here is a strong signal that the
    wrong agent ran -- not something to warn about and continue past.
    """
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    if not trajectory_path.exists():
        raise ProofError(
            f"No trajectory file at {trajectory_path}. A real, model-backed "
            "agent run must produce one; Oracle does not."
        )
    data = load_json(trajectory_path)
    if not data.get("schema_version"):
        raise ProofError(f"Trajectory file {trajectory_path} has no schema_version.")
    if not data.get("steps"):
        raise ProofError(
            f"Trajectory file {trajectory_path} has no steps; the agent may "
            "not have actually run."
        )
    return trajectory_path


# ---------------------------------------------------------------------------
# Checksums / manifest
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_checksums(root: Path) -> dict[str, str]:
    """Return ``{relative_posix_path: sha256}`` for every file under *root*."""
    checksums: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            checksums[path.relative_to(root).as_posix()] = sha256_file(path)
    return checksums


def project_name_for_trial(trial_name: str) -> str:
    """The exact Compose ``--project-name`` a trial's environment carries.

    Delegates to the same sanitizer ``GVisorEnvironment``/``DockerEnvironment``
    use (``session_id`` is the trial name), so the proof harness can label
    and clean up by project without duplicating that string-sanitization
    logic here.
    """
    from pier.environments.docker.docker import _sanitize_docker_compose_project_name

    return _sanitize_docker_compose_project_name(trial_name)


# ---------------------------------------------------------------------------
# Host-side runtime inspection and exact-project cleanup
# ---------------------------------------------------------------------------

_MAIN_CONTAINER_RE = re.compile(r"-main-\d+$")


@dataclass(frozen=True)
class RuntimeInspectionReport:
    project: str
    containers: dict[str, str]
    main_runtime: str | None
    proxy_runtime: str | None
    removed_containers: list[str]
    removed_networks: list[str]
    remaining_containers: list[str]
    remaining_networks: list[str]


async def _container_name(container_id: str, engine_cli: str) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            engine_cli,
            "inspect",
            "--format",
            "{{.Name}}",
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
    except Exception:
        return None
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip().lstrip("/")


async def inspect_and_clean_project(
    project: str,
    *,
    engine_cli: str = "docker",
    expected_main_runtime: str = "runsc",
) -> RuntimeInspectionReport:
    """Verify runtime placement, then clean up exactly this project.

    Confirms the ``main`` service's container is running under
    *expected_main_runtime* and, if a trusted egress proxy container is
    present, that it is *not*. Then removes exactly this Compose project's
    containers and networks (by exact project label -- never a name prefix
    or a guess) and verifies nothing of this project's remains.

    Reuses the same host-side primitives GVisorEnvironment's own
    verification and fallback teardown use
    (:mod:`pier.environments.gvisor.runtime`) rather than re-implementing
    ``docker`` CLI parsing: this re-derives the facts independently from the
    host, it does not trust the environment's own internal verification.
    """
    from pier.environments.agent_setup import EGRESS_PROXY_SERVICE
    from pier.environments.gvisor import runtime as gvisor_runtime

    container_ids = await gvisor_runtime.project_container_ids(project, engine_cli)
    if not container_ids:
        raise ProofError(
            f"No containers found for Compose project {project!r}; nothing to "
            "verify. The environment may not have started, or "
            "`--ek keep_containers=true` was not honored."
        )

    containers: dict[str, str] = {}
    main_runtime: str | None = None
    proxy_runtime: str | None = None
    for container_id in container_ids:
        name = await _container_name(container_id, engine_cli)
        actual_runtime = await gvisor_runtime.container_runtime(
            container_id, engine_cli
        )
        label = name or container_id
        containers[label] = actual_runtime or "unknown"
        if _MAIN_CONTAINER_RE.search(label):
            main_runtime = actual_runtime
        elif EGRESS_PROXY_SERVICE in label:
            proxy_runtime = actual_runtime

    if main_runtime is None:
        raise ProofError(
            f"Could not identify the 'main' container among "
            f"{sorted(containers)} for project {project!r}."
        )
    if main_runtime != expected_main_runtime:
        raise ProofError(
            f"'main' container for project {project!r} runs under "
            f"{main_runtime!r}, expected {expected_main_runtime!r}."
        )
    if proxy_runtime is not None and proxy_runtime == expected_main_runtime:
        raise ProofError(
            f"Egress proxy container for project {project!r} runs under "
            f"{expected_main_runtime!r}; it must stay on Docker's default "
            "runtime."
        )

    network_ids = await gvisor_runtime.project_network_ids(project, engine_cli)

    await gvisor_runtime.remove_containers(container_ids, engine_cli)
    await gvisor_runtime.remove_networks(network_ids, engine_cli)

    remaining_containers = await gvisor_runtime.project_container_ids(
        project, engine_cli
    )
    remaining_networks = await gvisor_runtime.project_network_ids(project, engine_cli)
    if remaining_containers or remaining_networks:
        raise ProofError(
            f"Cleanup left resources behind for project {project!r}: "
            f"containers={remaining_containers} networks={remaining_networks}."
        )

    return RuntimeInspectionReport(
        project=project,
        containers=containers,
        main_runtime=main_runtime,
        proxy_runtime=proxy_runtime,
        removed_containers=container_ids,
        removed_networks=network_ids,
        remaining_containers=remaining_containers,
        remaining_networks=remaining_networks,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_job_dir(args: argparse.Namespace) -> int:
    job_dir = resolve_job_dir(Path(args.jobs_dir), args.job_name)
    print(job_dir)
    return 0


def _cmd_find_trial_dir(args: argparse.Namespace) -> int:
    trial_dir = find_trial_dir(Path(args.job_dir), args.task_name)
    print(trial_dir)
    return 0


def _cmd_validate_job(args: argparse.Namespace) -> int:
    job_result = load_json(Path(args.job_dir) / "result.json")
    report = validate_job_result(job_result, min_mean_reward=args.min_mean_reward)
    print(json.dumps(asdict(report), indent=2))
    return 0


def _cmd_validate_trial(args: argparse.Namespace) -> int:
    trial_result = load_json(Path(args.trial_dir) / "result.json")
    validate_trial_errors(
        trial_result, expect_error_substring=args.expect_error_substring
    )
    print(json.dumps({"trial_name": trial_result.get("trial_name"), "ok": True}))
    return 0


def _cmd_validate_negative_log(args: argparse.Namespace) -> int:
    log_text = Path(args.log).read_text(errors="replace")
    validate_negative_test(
        log_text,
        exit_code=args.exit_code,
        must_contain=tuple(args.must_contain),
    )
    print(json.dumps({"exit_code": args.exit_code, "ok": True}))
    return 0


def _cmd_find_trajectory(args: argparse.Namespace) -> int:
    trajectory_path = find_trajectory(Path(args.trial_dir))
    print(trajectory_path)
    return 0


def _cmd_project_name(args: argparse.Namespace) -> int:
    print(project_name_for_trial(args.trial_name))
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    checksums = build_checksums(Path(args.root))
    Path(args.out).write_text(json.dumps(checksums, indent=2, sort_keys=True) + "\n")
    print(f"{len(checksums)} file(s) checksummed into {args.out}")
    return 0


def _cmd_inspect_and_clean(args: argparse.Namespace) -> int:
    report = asyncio.run(
        inspect_and_clean_project(
            args.project,
            engine_cli=args.engine,
            expected_main_runtime=args.expected_runtime,
        )
    )
    payload = json.dumps(asdict(report), indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n")
    print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gvisor_proof",
        description=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    job_dir = subparsers.add_parser(
        "job-dir", help="Resolve and print the job directory for a job name."
    )
    job_dir.add_argument("--jobs-dir", required=True)
    job_dir.add_argument("--job-name", required=True)
    job_dir.set_defaults(func=_cmd_job_dir)

    find_trial = subparsers.add_parser(
        "find-trial-dir", help="Find and print the trial directory for a task."
    )
    find_trial.add_argument("--job-dir", required=True)
    find_trial.add_argument("--task-name", required=True)
    find_trial.set_defaults(func=_cmd_find_trial_dir)

    validate_job = subparsers.add_parser(
        "validate-job", help="Semantically validate a job's result.json."
    )
    validate_job.add_argument("--job-dir", required=True)
    validate_job.add_argument("--min-mean-reward", type=float, default=1.0)
    validate_job.set_defaults(func=_cmd_validate_job)

    validate_trial = subparsers.add_parser(
        "validate-trial", help="Validate a single trial's exception state."
    )
    validate_trial.add_argument("--trial-dir", required=True)
    validate_trial.add_argument("--expect-error-substring", default=None)
    validate_trial.set_defaults(func=_cmd_validate_trial)

    validate_negative_log = subparsers.add_parser(
        "validate-negative-log",
        help="Validate a deliberately-failing `pier run` invocation's output.",
    )
    validate_negative_log.add_argument("--log", required=True)
    validate_negative_log.add_argument("--exit-code", type=int, required=True)
    validate_negative_log.add_argument("--must-contain", action="append", required=True)
    validate_negative_log.set_defaults(func=_cmd_validate_negative_log)

    find_traj = subparsers.add_parser(
        "find-trajectory", help="Find and print the agent's trajectory.json."
    )
    find_traj.add_argument("--trial-dir", required=True)
    find_traj.set_defaults(func=_cmd_find_trajectory)

    project_name = subparsers.add_parser(
        "project-name",
        help="Print the exact Compose project name for a trial name.",
    )
    project_name.add_argument("--trial-name", required=True)
    project_name.set_defaults(func=_cmd_project_name)

    manifest = subparsers.add_parser(
        "manifest", help="Write a JSON {path: sha256} manifest for a directory."
    )
    manifest.add_argument("--root", required=True)
    manifest.add_argument("--out", required=True)
    manifest.set_defaults(func=_cmd_manifest)

    inspect_and_clean = subparsers.add_parser(
        "inspect-and-clean",
        help="Verify runtime placement, then remove exactly this project's "
        "containers and networks.",
    )
    inspect_and_clean.add_argument("--project", required=True)
    inspect_and_clean.add_argument("--engine", default="docker")
    inspect_and_clean.add_argument("--expected-runtime", default="runsc")
    inspect_and_clean.add_argument("--out", default=None)
    inspect_and_clean.set_defaults(func=_cmd_inspect_and_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ProofError as exc:
        print(f"proof-gvisor-env: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
