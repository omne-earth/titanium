#!/usr/bin/env bash
# `make smoke-environment-completion`: the acceptance proof for the
# on_completion lifecycle policy.
#
#   titanium run                      (no flag, the default)
#       -> EnvironmentConfig.on_completion = teardown
#       -> BaseEnvironment.complete()
#       -> DockerEnvironment.stop()      compose down --rmi ...
#       -> the trial's project has no container left
#
#   titanium run --on-completion archive   (with delete still enabled)
#       -> EnvironmentConfig.on_completion = archive
#       -> BaseEnvironment.complete()
#       -> DockerEnvironment.archive()   compose stop
#       -> the trial's own container is still there, exited
#
# The proof is host-side: after each trial this asks the container engine
# what actually exists, filtered on that trial's exact Compose project label.
# Titanium's own logs and config are never evidence here, so an archive that
# only reports success still fails this smoke.
#
# The agent is `nop` and verification is disabled, so nothing here depends on
# a model or on the task being solved.
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
#
# Another engine is a parameter, not a fork of this script:
#   TITANIUM_SMOKE_ENGINE=podman TITANIUM_SMOKE_ENV=podman bash $0
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

ENGINE="${TITANIUM_SMOKE_ENGINE:-docker}"
ENV_NAME="${TITANIUM_SMOKE_ENV:-docker}"
TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/tasks/hello-world}"
TITANIUM="$ROOT/.venv/bin/titanium"
PY="$ROOT/.venv/bin/python"

# The label Compose stamps on every container it creates. The exact project
# of one trial is the identity this smoke trusts; names are never matched.
PROJECT_LABEL="com.docker.compose.project"
SERVICE_LABEL="com.docker.compose.service"
AGENT_SERVICE="main"

RUN_ID="lifecycle-$(date +%s)-$$"
WORK=""
FAILURES=0
# Recorded before each trial launches: an interruption mid-trial still leaves
# the trial directory on disk, so its project name stays recoverable for
# cleanup. Tracking only the names each case resolves at its end would miss it.
SMOKE_JOB_DIRS=()
# Set once the archive case has something the engine is still holding, so a
# mid-flight failure can print the exact targeted cleanup.
PRESERVED_PROJECT=""

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
pass() { echo "PASS: $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; exit 1; }
check_failed() { echo "  RESULT: FAIL -- $*"; FAILURES=$((FAILURES + 1)); }

# Only what this run created, matched on its own Compose project labels.
# Never a prune, never a wildcard, never an unrelated project.
remove_project() {
    local project="$1"
    [ -n "$project" ] || return 0
    local ids nets
    ids=$("$ENGINE" ps -a --filter "label=$PROJECT_LABEL=$project" \
        --format '{{.ID}}' 2>/dev/null)
    # shellcheck disable=SC2086
    [ -n "$ids" ] && "$ENGINE" rm -f $ids >/dev/null 2>&1
    nets=$("$ENGINE" network ls --filter "label=$PROJECT_LABEL=$project" \
        --format '{{.Name}}' 2>/dev/null)
    # shellcheck disable=SC2086
    [ -n "$nets" ] && "$ENGINE" network rm $nets >/dev/null 2>&1
    # Compose names the built image after the project, so the archive case and
    # any interrupted case would each leave one behind. The project name
    # carries this trial's unique session id, so a shared base image such as
    # ubuntu:24.04 can never match this prefix.
    local imgs
    imgs=$("$ENGINE" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep -E "(^|/)${project}[-_]" || true)
    # shellcheck disable=SC2086
    [ -n "$imgs" ] && "$ENGINE" rmi -f $imgs >/dev/null 2>&1
    return 0
}

cleanup() {
    local job trial project
    # Both cases, and a case interrupted mid-trial: each job directory is
    # rescanned for whatever trial directories exist right now.
    for job in ${SMOKE_JOB_DIRS[@]+"${SMOKE_JOB_DIRS[@]}"}; do
        [ -d "$job" ] || continue
        for trial in "$job"/*/; do
            [ -d "$trial" ] || continue
            project="$(project_for_trial_name "$(basename "${trial%/}")")" || continue
            remove_project "$project"
        done
    done
    remove_project "$PRESERVED_PROJECT"
    PRESERVED_PROJECT=""
    [ -n "$WORK" ] && [ -d "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------- preconditions

step "preconditions"
command -v "$ENGINE" >/dev/null 2>&1 || skip "no '$ENGINE' on PATH"
"$ENGINE" info >/dev/null 2>&1 || skip "the '$ENGINE' engine is not usable by $(id -un)"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -x "$PY" ] || skip "no interpreter at $PY (run 'make sync')"
[ -d "$TASK" ] || skip "no task at $TASK"
note "engine:      $ENGINE ($("$ENGINE" --version 2>/dev/null | head -1))"
note "titanium env: $ENV_NAME"
note "task:        $TASK"
note "run id:      $RUN_ID"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/titanium-completion-smoke.XXXXXX")" \
    || skip "could not create a work directory"

# Every container the engine already holds for *any* Compose project. A
# container found later must not be one of these: that is what keeps a stale
# container from a previous run from passing the archive case.
PRE_EXISTING_IDS="$("$ENGINE" ps -a --filter "label=$PROJECT_LABEL" --format '{{.ID}}' 2>/dev/null)"
note "pre-existing compose containers on this host: $(echo "$PRE_EXISTING_IDS" | grep -c . )"

# ---------------------------------------------------------------------- helpers

# The trial directory name is the session id; the Compose project is that name
# put through titanium's own sanitizer. Identity only, never evidence.
project_for_trial_name() {
    [ -n "$1" ] || return 1
    PYTHONPATH="$ROOT/src" "$PY" -c \
        'import sys; from titanium.environments.docker.docker import _sanitize_docker_compose_project_name as s; print(s(sys.argv[1]))' \
        "$1"
}

project_for_job_dir() {
    local job_dir="$1"
    local trial_dir
    trial_dir="$(find "$job_dir" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | head -1)"
    [ -n "$trial_dir" ] || return 1
    project_for_trial_name "$(basename "$trial_dir")"
}

containers_for_project() {
    "$ENGINE" ps -a --filter "label=$PROJECT_LABEL=$1" --format '{{.ID}}' 2>/dev/null | grep . || true
}

agent_container_for_project() {
    "$ENGINE" ps -a \
        --filter "label=$PROJECT_LABEL=$1" \
        --filter "label=$SERVICE_LABEL=$AGENT_SERVICE" \
        --format '{{.ID}}' 2>/dev/null | grep . || true
}

# One real trial through the normal CLI. Everything after this asks the
# engine, not titanium.
run_trial() {
    local case_name="$1"
    shift
    local jobs_dir="$WORK/$case_name/jobs"
    mkdir -p "$jobs_dir"
    JOB_DIR="$jobs_dir/$RUN_ID-$case_name"
    SMOKE_JOB_DIRS+=("$JOB_DIR")
    "$TITANIUM" run \
        --agent nop \
        --env "$ENV_NAME" \
        --path "$TASK" \
        --jobs-dir "$jobs_dir" \
        --job-name "$RUN_ID-$case_name" \
        --disable-verification \
        --yes \
        "$@" >"$WORK/$case_name.log" 2>&1
    TRIAL_RC=$?
    return 0
}

# ------------------------------------------------------------ case 1: teardown

step "CASE: teardown (default, no flag)"
note "running a real trial ..."
run_trial teardown
TEARDOWN_RC="$TRIAL_RC"
TEARDOWN_PROJECT="$(project_for_job_dir "$JOB_DIR")"

if [ -z "$TEARDOWN_PROJECT" ]; then
    tail -20 "$WORK/teardown.log"
    fail "the teardown trial produced no trial directory under $JOB_DIR"
fi

TEARDOWN_FOUND="$(containers_for_project "$TEARDOWN_PROJECT")"
echo "  CASE: teardown"
echo "  project/session: $TEARDOWN_PROJECT"
echo "  trial rc: $TEARDOWN_RC"
echo "  containers after trial: $(echo "$TEARDOWN_FOUND" | grep -c .) [$(echo "$TEARDOWN_FOUND" | tr '\n' ' ')]"
echo "  expected: absent"

if [ "$TEARDOWN_RC" -ne 0 ]; then
    tail -20 "$WORK/teardown.log"
    check_failed "the teardown trial exited $TEARDOWN_RC"
elif [ -n "$TEARDOWN_FOUND" ]; then
    check_failed "the default teardown left containers behind for $TEARDOWN_PROJECT"
else
    echo "  RESULT: PASS"
fi

# ------------------------------------------------- case 2: archive + delete on

step "CASE: archive + delete"
note "running a real trial with --on-completion archive ..."
CASE_START_EPOCH="$(date +%s)"
run_trial archive --on-completion archive
ARCHIVE_RC="$TRIAL_RC"
ARCHIVE_PROJECT="$(project_for_job_dir "$JOB_DIR")"

if [ -z "$ARCHIVE_PROJECT" ]; then
    tail -20 "$WORK/archive.log"
    fail "the archive trial produced no trial directory under $JOB_DIR"
fi
PRESERVED_PROJECT="$ARCHIVE_PROJECT"

ARCHIVE_ID="$(agent_container_for_project "$ARCHIVE_PROJECT")"
ARCHIVE_COUNT="$(echo "$ARCHIVE_ID" | grep -c .)"
ARCHIVE_STATE=""
ARCHIVE_CREATED=""
ARCHIVE_LABEL=""
if [ "$ARCHIVE_COUNT" = "1" ]; then
    ARCHIVE_STATE="$("$ENGINE" inspect -f '{{.State.Status}}' "$ARCHIVE_ID" 2>/dev/null)"
    ARCHIVE_CREATED="$("$ENGINE" inspect -f '{{.Created}}' "$ARCHIVE_ID" 2>/dev/null)"
    ARCHIVE_LABEL="$("$ENGINE" inspect -f "{{index .Config.Labels \"$PROJECT_LABEL\"}}" "$ARCHIVE_ID" 2>/dev/null)"
fi

echo "  CASE: archive+delete"
echo "  project/session: $ARCHIVE_PROJECT"
echo "  trial rc: $ARCHIVE_RC"
echo "  matching container: ${ARCHIVE_ID:-<none>} (count $ARCHIVE_COUNT)"
echo "  engine state: ${ARCHIVE_STATE:-<none>}"
echo "  created: ${ARCHIVE_CREATED:-<none>}"
echo "  expected: present + stopped"

ARCHIVE_OK=1
if [ "$ARCHIVE_RC" -ne 0 ]; then
    tail -20 "$WORK/archive.log"
    check_failed "the archive trial exited $ARCHIVE_RC"
    ARCHIVE_OK=0
fi
# present: archive must win over the still-enabled delete.
if [ "$ARCHIVE_COUNT" != "1" ]; then
    check_failed "expected exactly 1 '$AGENT_SERVICE' container for $ARCHIVE_PROJECT, found $ARCHIVE_COUNT"
    ARCHIVE_OK=0
fi
# stopped, not merely existing: a still-running container is not an archive.
if [ -n "$ARCHIVE_STATE" ] && [ "$ARCHIVE_STATE" = "running" ]; then
    check_failed "the preserved container is still running"
    ARCHIVE_OK=0
elif [ -n "$ARCHIVE_STATE" ] && [ "$ARCHIVE_STATE" != "exited" ] && [ "$ARCHIVE_STATE" != "created" ]; then
    check_failed "the preserved container is in state '$ARCHIVE_STATE', expected exited"
    ARCHIVE_OK=0
fi
# this run's container, not a survivor of an earlier one.
if [ -n "$ARCHIVE_ID" ] && echo "$PRE_EXISTING_IDS" | grep -qx "$ARCHIVE_ID"; then
    check_failed "the container existed before this smoke started -- stale, not archived"
    ARCHIVE_OK=0
fi
if [ -n "$ARCHIVE_CREATED" ]; then
    created_epoch="$(date -d "$ARCHIVE_CREATED" +%s 2>/dev/null || echo 0)"
    if [ "$created_epoch" -lt "$CASE_START_EPOCH" ]; then
        check_failed "the container predates this case ($ARCHIVE_CREATED) -- stale, not archived"
        ARCHIVE_OK=0
    fi
fi
# the identity is the exact project of this trial, not a fuzzy name match.
if [ -n "$ARCHIVE_LABEL" ] && [ "$ARCHIVE_LABEL" != "$ARCHIVE_PROJECT" ]; then
    check_failed "container project label '$ARCHIVE_LABEL' != this trial's '$ARCHIVE_PROJECT'"
    ARCHIVE_OK=0
fi
# the teardown case must not have preserved anything under its own project.
if [ "$ARCHIVE_PROJECT" = "$TEARDOWN_PROJECT" ]; then
    check_failed "both cases used the same project -- the cases are not independent"
    ARCHIVE_OK=0
fi

[ "$ARCHIVE_OK" = "1" ] && echo "  RESULT: PASS"

# The evidence above is recorded; only now is the preserved container removed.
step "targeted cleanup"
note "removing only project $PRESERVED_PROJECT"
if [ "$FAILURES" -ne 0 ]; then
    note "cleanup command if this run is left behind:"
    note "  $ENGINE rm -f \$($ENGINE ps -aq --filter label=$PROJECT_LABEL=$PRESERVED_PROJECT)"
fi
cleanup
note "done"

# ------------------------------------------------------------------- verdict

step "verdict"
if [ "$FAILURES" -eq 0 ]; then
    pass "on_completion=teardown tore down, on_completion=archive preserved a stopped container"
    echo
    echo "OVERALL: PASS"
    exit 0
fi
echo
echo "OVERALL: FAIL ($FAILURES failed assertion(s))"
exit 1
