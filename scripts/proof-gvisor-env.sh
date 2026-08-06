#!/usr/bin/env bash
set -euo pipefail

# Orchestrates `make proof-gvisor-env`. Actual parsing/validation/decision
# logic lives in scripts/gvisor_proof.py (unit-tested in
# tests/test_gvisor_proof.py); this script only sequences `pier run` and a
# handful of host commands, and fails closed at every step.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${PROOF_AGENT:=claude-code}"
: "${PROOF_MODEL:=claude-haiku-4-5-20251001}"
: "${PROOF_TASK:=examples/tasks/agent-behavior-probe}"
: "${PROOF_GATE_TASK:=examples/tasks/hello-world-no-internet}"
: "${PROOF_JOBS_DIR:=jobs}"
: "${PROOF_ARTIFACTS_ROOT:=proof-artifacts/gvisor-env}"
: "${PROOF_ENGINE_CLI:=docker}"
: "${PROOF_TIMEOUT_MULTIPLIER:=}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
JOB_NAME="proof-gvisor-env-${RUN_ID}"
GATE_JOB_NAME="${JOB_NAME}-gate"
PODMAN_JOB_NAME="${JOB_NAME}-podman-check"

ARTIFACTS_DIR="${PROOF_ARTIFACTS_ROOT}/${RUN_ID}"
mkdir -p "$ARTIFACTS_DIR"

COMMANDS_LOG="$ARTIFACTS_DIR/commands.txt"
HOST_PREFLIGHT_LOG="$ARTIFACTS_DIR/host-preflight.txt"
RUNTIME_INSPECTION_LOG="$ARTIFACTS_DIR/runtime-inspection.json"
CLEANUP_LOG="$ARTIFACTS_DIR/cleanup-verification.txt"
CHECKSUMS_PATH="$ARTIFACTS_DIR/checksums.sha256.json"

: > "$COMMANDS_LOG"

log_cmd() {
  printf '+ %s\n' "$*" >> "$COMMANDS_LOG"
  "$@"
}

run_pier() { log_cmd uv run pier "$@"; }
run_proof_py() { log_cmd uv run python scripts/gvisor_proof.py "$@"; }

# Defense in depth: refuse to finish if any credential env var Claude Code
# reads ended up copied verbatim into a preserved artifact. Never prints the
# value itself.
redact_check() {
  for var in ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN; do
    local val="${!var:-}"
    if [ -n "$val" ] && grep -R -l -F -- "$val" "$ARTIFACTS_DIR" >/dev/null 2>&1; then
      echo "proof-gvisor-env: refusing to continue -- a value from \$$var was found inside $ARTIFACTS_DIR" >&2
      exit 1
    fi
  done
}

on_exit() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    echo "proof-gvisor-env: FAILED (exit $exit_code). Evidence preserved under $ARTIFACTS_DIR" >&2
  else
    echo "proof-gvisor-env: OK. Evidence written to $ARTIFACTS_DIR"
  fi
  exit "$exit_code"
}
trap on_exit EXIT

echo "== proof-gvisor-env run $RUN_ID ==" | tee -a "$COMMANDS_LOG"
echo "Artifacts: $ARTIFACTS_DIR"

# ---------------------------------------------------------------------------
# 0. Credentials present (never printed, never copied into artifacts).
# ---------------------------------------------------------------------------
if [ "$PROOF_AGENT" = "claude-code" ]; then
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
     && [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
    cat >&2 <<'EOF'
proof-gvisor-env: none of ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or
ANTHROPIC_AUTH_TOKEN is set in this shell. The Claude Code agent needs one of
these to authenticate inside the sandbox. Run `claude setup-token` and
`export CLAUDE_CODE_OAUTH_TOKEN=...`, or export ANTHROPIC_API_KEY, then retry.
EOF
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 1. Trusted host-side preflight.
# ---------------------------------------------------------------------------
{
  echo "== host preflight =="
  COMMIT_HASH="$(git rev-parse HEAD)"
  echo "commit: $COMMIT_HASH"

  echo
  echo "\$ docker info --format '{{.DefaultRuntime}}'"
  default_runtime="$(docker info --format '{{.DefaultRuntime}}')"
  echo "$default_runtime"
  if [ "$default_runtime" != "runc" ]; then
    echo "FAIL: Docker's default runtime is '$default_runtime', expected 'runc'" >&2
    exit 1
  fi

  echo
  echo "\$ docker info --format '{{json .Runtimes}}'"
  runtimes_json="$(docker info --format '{{json .Runtimes}}')"
  echo "$runtimes_json"
  if ! printf '%s' "$runtimes_json" | uv run python -c \
    "import json,sys; sys.exit(0 if 'runsc' in json.load(sys.stdin) else 1)"; then
    echo "FAIL: 'runsc' is not registered with the Docker daemon" >&2
    exit 1
  fi

  echo
  echo "\$ getenforce"
  if command -v getenforce >/dev/null 2>&1; then
    se_status="$(getenforce)"
    echo "$se_status"
    if [ "$se_status" != "Enforcing" ]; then
      echo "FAIL: SELinux is '$se_status', expected 'Enforcing'" >&2
      exit 1
    fi
  else
    echo "getenforce not available on this host; SELinux check skipped"
  fi

  echo
  echo "\$ git diff --exit-code origin/edge...HEAD -- src/pier/environments/docker/docker.py src/pier/environments/docker/__init__.py"
  if ! git diff --exit-code origin/edge...HEAD -- \
      src/pier/environments/docker/docker.py \
      src/pier/environments/docker/__init__.py; then
    echo "FAIL: normal Docker source differs from origin/edge" >&2
    exit 1
  fi
  echo "(no diff)"

  echo
  echo "gvisor is registered as a first-class environment type:"
  uv run python -c "
from pier.models.environment_type import EnvironmentType
from pier.environments.factory import _ENVIRONMENT_REGISTRY, _load_environment_class
from pier.environments.gvisor.environment import GVisorEnvironment
assert EnvironmentType.GVISOR in _ENVIRONMENT_REGISTRY
assert _load_environment_class(EnvironmentType.GVISOR) is GVisorEnvironment
print('  EnvironmentType.GVISOR ->', _load_environment_class(EnvironmentType.GVISOR))
"
} > "$HOST_PREFLIGHT_LOG" 2>&1
cat "$HOST_PREFLIGHT_LOG"

COMMIT_HASH="$(git rev-parse HEAD)"

# ---------------------------------------------------------------------------
# 2. Podman-backed gVisor must fail explicitly. An unsupported engine is
#    rejected synchronously while GVisorEnvironment.__init__ resolves the
#    engine CLI -- before any Docker/Compose call, and before any trial
#    directory or result.json exists -- so `pier run` itself exits nonzero
#    and prints the exception; it is not recorded as a per-trial error.
#    No containers or networks are ever created, so there is nothing to
#    clean up for this step.
# ---------------------------------------------------------------------------
echo "== podman-backed gVisor must fail explicitly ==" | tee -a "$COMMANDS_LOG"
set +e
run_pier run -p "$PROOF_GATE_TASK" -a oracle -e gvisor --ek engine=podman \
  --job-name "$PODMAN_JOB_NAME" -y \
  > "$ARTIFACTS_DIR/podman-negative-test.log" 2>&1
podman_run_exit=$?
set -e
echo "(pier run exited $podman_run_exit)" >> "$ARTIFACTS_DIR/podman-negative-test.log"

run_proof_py validate-negative-log --log "$ARTIFACTS_DIR/podman-negative-test.log" \
  --exit-code "$podman_run_exit" \
  --must-contain podman --must-contain "does not implement" \
  >> "$ARTIFACTS_DIR/podman-negative-test.log"
cat "$ARTIFACTS_DIR/podman-negative-test.log"
redact_check

# The failed job still creates jobs/$PODMAN_JOB_NAME/ (an empty trial dir,
# no result.json, no containers or networks). It carries no evidence beyond
# what's already captured above; remove it since it's a leftover from this
# proof run and its name is exact and fully under this script's control.
rm -rf "${PROOF_JOBS_DIR:?}/${PODMAN_JOB_NAME:?}"

# ---------------------------------------------------------------------------
# 3. Cheap gate: existing deterministic no-network task, Oracle, under gvisor.
# ---------------------------------------------------------------------------
echo "== cheap gate: $PROOF_GATE_TASK via oracle under gvisor ==" | tee -a "$COMMANDS_LOG"
run_pier run -p "$PROOF_GATE_TASK" -a oracle -e gvisor --job-name "$GATE_JOB_NAME" -y \
  > "$ARTIFACTS_DIR/gate-run.log" 2>&1
cat "$ARTIFACTS_DIR/gate-run.log"
redact_check

gate_job_dir="$(run_proof_py job-dir --jobs-dir "$PROOF_JOBS_DIR" --job-name "$GATE_JOB_NAME")"
run_proof_py validate-job --job-dir "$gate_job_dir" > "$ARTIFACTS_DIR/gate-result-validation.json"
cat "$ARTIFACTS_DIR/gate-result-validation.json"
cp "$gate_job_dir/result.json" "$ARTIFACTS_DIR/gate-job-result.json"

# ---------------------------------------------------------------------------
# 4. Main proof: a real, model-backed agent trajectory under gvisor.
#    keep_containers=true so the containers survive the run for host-side
#    runtime inspection; this script removes them itself afterward (step 6),
#    scoped to exactly this trial's Compose project.
# ---------------------------------------------------------------------------
echo "== main proof: $PROOF_AGENT ($PROOF_MODEL) on $PROOF_TASK under gvisor ==" | tee -a "$COMMANDS_LOG"
timeout_args=()
if [ -n "$PROOF_TIMEOUT_MULTIPLIER" ]; then
  timeout_args=(--timeout-multiplier "$PROOF_TIMEOUT_MULTIPLIER")
fi
run_pier run -p "$PROOF_TASK" -a "$PROOF_AGENT" -m "$PROOF_MODEL" -e gvisor \
  --ek keep_containers=true --job-name "$JOB_NAME" -y "${timeout_args[@]}" \
  > "$ARTIFACTS_DIR/main-run.log" 2>&1
cat "$ARTIFACTS_DIR/main-run.log"
redact_check

# ---------------------------------------------------------------------------
# 5. Locate the job/trial robustly (by the exact --job-name passed above,
#    never by scanning for the newest timestamp), and validate result.json
#    and the trajectory.
# ---------------------------------------------------------------------------
job_dir="$(run_proof_py job-dir --jobs-dir "$PROOF_JOBS_DIR" --job-name "$JOB_NAME")"
task_basename="$(basename "$PROOF_TASK")"
trial_dir="$(run_proof_py find-trial-dir --job-dir "$job_dir" --task-name "$task_basename")"
trial_name="$(basename "$trial_dir")"

run_proof_py validate-job --job-dir "$job_dir" > "$ARTIFACTS_DIR/result-validation.json"
cat "$ARTIFACTS_DIR/result-validation.json"

run_proof_py validate-trial --trial-dir "$trial_dir" > "$ARTIFACTS_DIR/trial-validation.json"
cat "$ARTIFACTS_DIR/trial-validation.json"

trajectory_path="$(run_proof_py find-trajectory --trial-dir "$trial_dir")"
echo "trajectory: $trajectory_path" | tee -a "$COMMANDS_LOG"

cp "$job_dir/result.json" "$ARTIFACTS_DIR/job-result.json"
cp "$job_dir/config.json" "$ARTIFACTS_DIR/job-config.json"
cp "$trial_dir/result.json" "$ARTIFACTS_DIR/trial-result.json"
cp "$trajectory_path" "$ARTIFACTS_DIR/trajectory.json"

# ---------------------------------------------------------------------------
# 6. Trusted host-side runtime inspection, then exact-project cleanup.
# ---------------------------------------------------------------------------
project_name="$(run_proof_py project-name --trial-name "$trial_name")"
echo "compose project: $project_name" | tee -a "$COMMANDS_LOG"

run_proof_py inspect-and-clean --project "$project_name" --engine "$PROOF_ENGINE_CLI" \
  --expected-runtime runsc --out "$RUNTIME_INSPECTION_LOG"
cat "$RUNTIME_INSPECTION_LOG"

# ---------------------------------------------------------------------------
# 7. Cleanup verification, independent of step 6's own bookkeeping: no
#    exact-project containers, no exact-project networks, no runsc processes.
#    Also spot-checks the gate project for completeness (step 2's podman
#    rejection never created a project to check -- see the comment there).
# ---------------------------------------------------------------------------
gate_trial_dir="$(run_proof_py find-trial-dir --job-dir "$gate_job_dir" --task-name "$(basename "$PROOF_GATE_TASK")")"
gate_project="$(run_proof_py project-name --trial-name "$(basename "$gate_trial_dir")")"

cleanup_failed=0
{
  echo "== cleanup verification =="
  for pair in "main:$project_name" "gate:$gate_project"; do
    name="${pair%%:*}"
    proj="${pair#*:}"
    echo
    echo "-- $name project ($proj) --"
    echo "\$ docker ps -a --filter label=com.docker.compose.project=$proj --format '{{.Names}}'"
    leftover_containers="$(docker ps -a --filter "label=com.docker.compose.project=$proj" --format '{{.Names}}')"
    echo "${leftover_containers:-<none>}"
    echo "\$ docker network ls --filter label=com.docker.compose.project=$proj --format '{{.Name}}'"
    leftover_networks="$(docker network ls --filter "label=com.docker.compose.project=$proj" --format '{{.Name}}')"
    echo "${leftover_networks:-<none>}"
    if [ -n "$leftover_containers" ] || [ -n "$leftover_networks" ]; then
      echo "FAIL: leftover resources for project $proj"
      cleanup_failed=1
    fi
  done

  echo
  echo "\$ pgrep -x runsc"
  if runsc_pids="$(pgrep -x runsc)"; then
    echo "$runsc_pids"
    echo "FAIL: runsc process(es) still running after cleanup"
    cleanup_failed=1
  else
    echo "no runsc processes found"
  fi
} > "$CLEANUP_LOG" 2>&1
cat "$CLEANUP_LOG"
if [ "$cleanup_failed" -ne 0 ]; then
  echo "proof-gvisor-env: cleanup verification failed; see $CLEANUP_LOG" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 8. Assemble the manifest and checksums last, over the finished bundle.
# ---------------------------------------------------------------------------
cat > "$ARTIFACTS_DIR/manifest.json" <<EOF
{
  "run_id": "$RUN_ID",
  "job_name": "$JOB_NAME",
  "gate_job_name": "$GATE_JOB_NAME",
  "podman_check_job_name": "$PODMAN_JOB_NAME",
  "task": "$PROOF_TASK",
  "gate_task": "$PROOF_GATE_TASK",
  "agent": "$PROOF_AGENT",
  "model": "$PROOF_MODEL",
  "compose_project": "$project_name",
  "trial_name": "$trial_name",
  "commit": "$COMMIT_HASH",
  "generated_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

redact_check
run_proof_py manifest --root "$ARTIFACTS_DIR" --out "$CHECKSUMS_PATH"
cat "$CHECKSUMS_PATH"

echo "== proof-gvisor-env succeeded ==" | tee -a "$COMMANDS_LOG"
