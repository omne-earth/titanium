"""Unit tests for scripts/gvisor_proof.py.

Covers the logic `scripts/proof-gvisor-env.sh` depends on: job/trial
discovery, result.json validation, trajectory discovery, and the fail-closed
behavior of each. No Docker, gVisor, or real agent is involved -- these are
plain filesystem/JSON fixtures.

`inspect_and_clean_project` itself (the one function that shells out to
`docker`) is exercised only at the decision-logic level, with the
`pier.environments.gvisor.runtime` primitives it calls monkeypatched at that
exact seam -- the module boundary between "trusted host command" and
"decision about what the result means" -- so the real classification and
fail-closed logic still runs, not a mock standing in for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gvisor_proof as gp


# ---------------------------------------------------------------------------
# Job / trial discovery
# ---------------------------------------------------------------------------


def test_resolve_job_dir_returns_existing_job(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "proof-gvisor-env-abc"
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text("{}")

    resolved = gp.resolve_job_dir(tmp_path / "jobs", "proof-gvisor-env-abc")

    assert resolved == job_dir


def test_resolve_job_dir_fails_closed_when_missing(tmp_path: Path):
    with pytest.raises(gp.ProofError, match="does not exist"):
        gp.resolve_job_dir(tmp_path / "jobs", "never-ran")


def test_resolve_job_dir_fails_closed_without_result_json(tmp_path: Path):
    job_dir = tmp_path / "jobs" / "half-finished"
    job_dir.mkdir(parents=True)

    with pytest.raises(gp.ProofError, match="no result.json"):
        gp.resolve_job_dir(tmp_path / "jobs", "half-finished")


def test_find_trial_dir_matches_exact_prefix(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "agent-behavior-probe__abc123").mkdir()
    (job_dir / "config.json").write_text("{}")

    trial_dir = gp.find_trial_dir(job_dir, "agent-behavior-probe")

    assert trial_dir.name == "agent-behavior-probe__abc123"


def test_find_trial_dir_does_not_match_a_task_name_that_is_a_prefix(tmp_path: Path):
    """A task named 'agent-behavior' must not match 'agent-behavior-probe__xyz'."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "agent-behavior-probe__abc123").mkdir()

    with pytest.raises(gp.ProofError, match="No trial directory"):
        gp.find_trial_dir(job_dir, "agent-behavior")


def test_find_trial_dir_fails_closed_on_no_match(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(gp.ProofError, match="No trial directory"):
        gp.find_trial_dir(job_dir, "agent-behavior-probe")


def test_find_trial_dir_fails_closed_on_ambiguous_match(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "agent-behavior-probe__aaa").mkdir()
    (job_dir / "agent-behavior-probe__bbb").mkdir()

    with pytest.raises(gp.ProofError, match="found 2"):
        gp.find_trial_dir(job_dir, "agent-behavior-probe")


def test_load_json_fails_closed_on_missing_file(tmp_path: Path):
    with pytest.raises(gp.ProofError, match="does not exist"):
        gp.load_json(tmp_path / "missing.json")


def test_load_json_fails_closed_on_invalid_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")

    with pytest.raises(gp.ProofError, match="not valid JSON"):
        gp.load_json(bad)


# ---------------------------------------------------------------------------
# Semantic result validation
# ---------------------------------------------------------------------------


def _job_result(
    *,
    n_errored=0,
    n_cancelled=0,
    n_completed=1,
    evals=None,
):
    if evals is None:
        evals = {"claude-code__claude-haiku-4-5__adhoc": {"metrics": [{"mean": 1.0}]}}
    return {
        "stats": {
            "n_completed_trials": n_completed,
            "n_errored_trials": n_errored,
            "n_cancelled_trials": n_cancelled,
            "evals": evals,
        }
    }


def test_validate_job_result_accepts_a_clean_success():
    report = gp.validate_job_result(_job_result())

    assert report.n_completed_trials == 1
    assert report.n_errored_trials == 0
    assert report.mean_reward == 1.0
    assert report.evals_key == "claude-code__claude-haiku-4-5__adhoc"


def test_validate_job_result_rejects_errored_trials():
    with pytest.raises(gp.ProofError, match="1 errored trial"):
        gp.validate_job_result(_job_result(n_errored=1))


def test_validate_job_result_rejects_cancelled_trials():
    with pytest.raises(gp.ProofError, match="1 cancelled trial"):
        gp.validate_job_result(_job_result(n_cancelled=1))


def test_validate_job_result_rejects_zero_completed_trials():
    with pytest.raises(gp.ProofError, match="0 completed trial"):
        gp.validate_job_result(_job_result(n_completed=0))


def test_validate_job_result_rejects_no_eval_groups():
    with pytest.raises(gp.ProofError, match="no eval groups"):
        gp.validate_job_result(_job_result(evals={}))


def test_validate_job_result_rejects_multiple_eval_groups():
    evals = {
        "claude-code__m__adhoc": {"metrics": [{"mean": 1.0}]},
        "oracle__adhoc": {"metrics": [{"mean": 1.0}]},
    }
    with pytest.raises(gp.ProofError, match="exactly one eval group"):
        gp.validate_job_result(_job_result(evals=evals))


def test_validate_job_result_rejects_missing_mean_metric():
    evals = {"claude-code__m__adhoc": {"metrics": [{"stddev": 0.0}]}}
    with pytest.raises(gp.ProofError, match="no 'mean' metric"):
        gp.validate_job_result(_job_result(evals=evals))


def test_validate_job_result_rejects_reward_below_threshold():
    evals = {"claude-code__m__adhoc": {"metrics": [{"mean": 0.0}]}}
    with pytest.raises(gp.ProofError, match="below the required"):
        gp.validate_job_result(_job_result(evals=evals))


def test_validate_trial_errors_accepts_a_clean_trial():
    gp.validate_trial_errors({"trial_name": "t", "exception_info": None})


def test_validate_trial_errors_rejects_an_unexpected_exception():
    trial_result = {
        "trial_name": "t",
        "exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "boom",
        },
    }
    with pytest.raises(gp.ProofError, match="recorded an exception"):
        gp.validate_trial_errors(trial_result)


def test_validate_trial_errors_expects_a_matching_failure():
    trial_result = {
        "trial_name": "t",
        "exception_info": {
            "exception_type": "NotImplementedError",
            "exception_message": "does not implement the 'podman' container engine",
        },
    }
    # Should not raise: the failure matches what was expected.
    gp.validate_trial_errors(trial_result, expect_error_substring="podman")


def test_validate_trial_errors_fails_closed_when_expected_failure_did_not_happen():
    trial_result = {"trial_name": "t", "exception_info": None}
    with pytest.raises(gp.ProofError, match="expected to fail"):
        gp.validate_trial_errors(trial_result, expect_error_substring="podman")


def test_validate_trial_errors_fails_closed_on_wrong_failure_message():
    trial_result = {
        "trial_name": "t",
        "exception_info": {
            "exception_type": "TimeoutError",
            "exception_message": "agent timed out",
        },
    }
    with pytest.raises(gp.ProofError, match="does not mention"):
        gp.validate_trial_errors(trial_result, expect_error_substring="podman")


# ---------------------------------------------------------------------------
# Negative-test validation (e.g. Podman-backed gVisor must fail explicitly)
# ---------------------------------------------------------------------------


def test_validate_negative_test_accepts_a_matching_failure():
    log_text = (
        "NotImplementedError: The gVisor environment does not implement the "
        "'podman' container engine yet."
    )
    # Should not raise.
    gp.validate_negative_test(
        log_text, exit_code=1, must_contain=("podman", "does not implement")
    )


def test_validate_negative_test_fails_closed_if_process_unexpectedly_succeeded():
    with pytest.raises(gp.ProofError, match="exit nonzero"):
        gp.validate_negative_test("all good", exit_code=0, must_contain=("podman",))


def test_validate_negative_test_fails_closed_on_wrong_failure_text():
    with pytest.raises(gp.ProofError, match="missing expected text"):
        gp.validate_negative_test(
            "TimeoutError: agent timed out", exit_code=1, must_contain=("podman",)
        )


def test_validate_negative_test_is_case_insensitive():
    gp.validate_negative_test(
        "REJECTED: PODMAN is not supported", exit_code=1, must_contain=("podman",)
    )


# ---------------------------------------------------------------------------
# Trajectory discovery
# ---------------------------------------------------------------------------


def test_find_trajectory_returns_path_when_valid(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    trajectory = agent_dir / "trajectory.json"
    trajectory.write_text(
        json.dumps({"schema_version": "ATIF-v1.7", "steps": [{"step_id": 1}]})
    )

    found = gp.find_trajectory(tmp_path)

    assert found == trajectory


def test_find_trajectory_fails_closed_when_missing(tmp_path: Path):
    (tmp_path / "agent").mkdir()

    with pytest.raises(gp.ProofError, match="No trajectory file"):
        gp.find_trajectory(tmp_path)


def test_find_trajectory_fails_closed_on_oracle_style_run(tmp_path: Path):
    """Oracle writes agent/oracle.txt, never agent/trajectory.json."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "oracle.txt").write_text("solved")

    with pytest.raises(gp.ProofError, match="No trajectory file"):
        gp.find_trajectory(tmp_path)


def test_find_trajectory_fails_closed_on_empty_steps(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "trajectory.json").write_text(
        json.dumps({"schema_version": "ATIF-v1.7", "steps": []})
    )

    with pytest.raises(gp.ProofError, match="no steps"):
        gp.find_trajectory(tmp_path)


def test_find_trajectory_fails_closed_on_missing_schema_version(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "trajectory.json").write_text(json.dumps({"steps": [{"step_id": 1}]}))

    with pytest.raises(gp.ProofError, match="no schema_version"):
        gp.find_trajectory(tmp_path)


# ---------------------------------------------------------------------------
# Checksums / manifest
# ---------------------------------------------------------------------------


def test_build_checksums_is_stable_and_relative(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("world")

    checksums = gp.build_checksums(tmp_path)

    assert set(checksums) == {"a.txt", "nested/b.txt"}
    assert checksums["a.txt"] == gp.sha256_file(tmp_path / "a.txt")
    # sha256("hello")
    assert (
        checksums["a.txt"]
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_project_name_for_trial_matches_the_environment_sanitizer():
    from pier.environments.docker.docker import _sanitize_docker_compose_project_name

    trial_name = "agent-behavior-probe__AbC123"

    assert gp.project_name_for_trial(trial_name) == (
        _sanitize_docker_compose_project_name(trial_name)
    )


# ---------------------------------------------------------------------------
# Runtime inspection and exact-project cleanup (decision logic only;
# the `docker`-calling primitives are monkeypatched at the module seam).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_and_clean_project_passes_when_main_is_runsc_and_proxy_is_runc(
    monkeypatch,
):
    import pier.environments.gvisor.runtime as gvisor_runtime

    project = "proof-gvisor-env-abc"
    names = {
        "main-id": f"/{project}-main-1",
        "proxy-id": f"/{project}-pier-egress-proxy-1",
    }
    runtimes = {"main-id": "runsc", "proxy-id": "runc"}
    removed: dict[str, list[str]] = {"containers": [], "networks": []}

    async def fake_project_container_ids(proj, cli):
        assert proj == project
        return [] if removed["containers"] else list(names)

    async def fake_project_network_ids(proj, cli):
        return [] if removed["networks"] else ["net-1"]

    async def fake_container_runtime(container_id, cli):
        return runtimes[container_id]

    async def fake_remove_containers(ids, cli):
        removed["containers"].extend(ids)

    async def fake_remove_networks(ids, cli):
        removed["networks"].extend(ids)

    async def fake_container_name(container_id, cli):
        return names[container_id]

    monkeypatch.setattr(
        gvisor_runtime, "project_container_ids", fake_project_container_ids
    )
    monkeypatch.setattr(gvisor_runtime, "project_network_ids", fake_project_network_ids)
    monkeypatch.setattr(gvisor_runtime, "container_runtime", fake_container_runtime)
    monkeypatch.setattr(gvisor_runtime, "remove_containers", fake_remove_containers)
    monkeypatch.setattr(gvisor_runtime, "remove_networks", fake_remove_networks)
    monkeypatch.setattr(gp, "_container_name", fake_container_name)

    report = await gp.inspect_and_clean_project(project)

    assert report.main_runtime == "runsc"
    assert report.proxy_runtime == "runc"
    assert sorted(report.removed_containers) == sorted(names)
    assert report.remaining_containers == []
    assert report.remaining_networks == []


@pytest.mark.asyncio
async def test_inspect_and_clean_project_fails_closed_if_main_is_not_runsc(
    monkeypatch,
):
    import pier.environments.gvisor.runtime as gvisor_runtime

    project = "proof-gvisor-env-abc"

    async def fake_project_container_ids(proj, cli):
        return ["main-id"]

    async def fake_container_runtime(container_id, cli):
        return "runc"  # wrong: should be runsc

    async def fake_container_name(container_id, cli):
        return f"/{project}-main-1"

    monkeypatch.setattr(
        gvisor_runtime, "project_container_ids", fake_project_container_ids
    )
    monkeypatch.setattr(gvisor_runtime, "container_runtime", fake_container_runtime)
    monkeypatch.setattr(gp, "_container_name", fake_container_name)

    with pytest.raises(gp.ProofError, match="expected 'runsc'"):
        await gp.inspect_and_clean_project(project)


@pytest.mark.asyncio
async def test_inspect_and_clean_project_fails_closed_if_proxy_is_also_runsc(
    monkeypatch,
):
    import pier.environments.gvisor.runtime as gvisor_runtime

    project = "proof-gvisor-env-abc"
    names = {
        "main-id": f"/{project}-main-1",
        "proxy-id": f"/{project}-pier-egress-proxy-1",
    }
    runtimes = {"main-id": "runsc", "proxy-id": "runsc"}

    async def fake_project_container_ids(proj, cli):
        return list(names)

    async def fake_container_runtime(container_id, cli):
        return runtimes[container_id]

    async def fake_container_name(container_id, cli):
        return names[container_id]

    monkeypatch.setattr(
        gvisor_runtime, "project_container_ids", fake_project_container_ids
    )
    monkeypatch.setattr(gvisor_runtime, "container_runtime", fake_container_runtime)
    monkeypatch.setattr(gp, "_container_name", fake_container_name)

    with pytest.raises(gp.ProofError, match="Egress proxy"):
        await gp.inspect_and_clean_project(project)


@pytest.mark.asyncio
async def test_inspect_and_clean_project_fails_closed_with_no_containers(
    monkeypatch,
):
    import pier.environments.gvisor.runtime as gvisor_runtime

    async def fake_project_container_ids(proj, cli):
        return []

    monkeypatch.setattr(
        gvisor_runtime, "project_container_ids", fake_project_container_ids
    )

    with pytest.raises(gp.ProofError, match="No containers found"):
        await gp.inspect_and_clean_project("proof-gvisor-env-abc")


@pytest.mark.asyncio
async def test_inspect_and_clean_project_fails_closed_if_cleanup_leaves_resources(
    monkeypatch,
):
    import pier.environments.gvisor.runtime as gvisor_runtime

    project = "proof-gvisor-env-abc"
    names = {"main-id": f"/{project}-main-1"}
    runtimes = {"main-id": "runsc"}

    async def fake_project_container_ids(proj, cli):
        # Cleanup never actually removes anything -- simulates a `docker rm`
        # that reports success but the daemon didn't comply.
        return list(names)

    async def fake_project_network_ids(proj, cli):
        return []

    async def fake_container_runtime(container_id, cli):
        return runtimes[container_id]

    async def fake_container_name(container_id, cli):
        return names[container_id]

    async def fake_remove_containers(ids, cli):
        return None

    async def fake_remove_networks(ids, cli):
        return None

    monkeypatch.setattr(
        gvisor_runtime, "project_container_ids", fake_project_container_ids
    )
    monkeypatch.setattr(gvisor_runtime, "project_network_ids", fake_project_network_ids)
    monkeypatch.setattr(gvisor_runtime, "container_runtime", fake_container_runtime)
    monkeypatch.setattr(gvisor_runtime, "remove_containers", fake_remove_containers)
    monkeypatch.setattr(gvisor_runtime, "remove_networks", fake_remove_networks)
    monkeypatch.setattr(gp, "_container_name", fake_container_name)

    with pytest.raises(gp.ProofError, match="left resources behind"):
        await gp.inspect_and_clean_project(project)
