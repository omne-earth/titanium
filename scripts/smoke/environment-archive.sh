#!/usr/bin/env bash
# The archive acceptance proof: one real trial with `--on-completion archive`,
# then the published tar is read back with `tar` to confirm the guest's own
# write survived into it.
#
# Nothing here knows which engine or runtime is underneath. The environment is
# a parameter and every assertion below is ordinary file and tar I/O against
# the artifact Titanium published:
#
#   TITANIUM_SMOKE_ENV=gvisor-podman bash $0
#
# The agent is `oracle`, which runs the task's solution script verbatim, and
# verification is disabled, so nothing here depends on a model.
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

ENV_NAME="${TITANIUM_SMOKE_ENV:-podman}"
TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/smoke/archive-marker}"
TITANIUM="$ROOT/.venv/bin/titanium"

# What the task's solution writes inside the guest, and what it writes there.
# The archive is only real if the path and its contents both come back out.
MARKER_PATH="app/archive-marker.txt"
MARKER_CONTENT="archived-guest-state"

RUN_ID="archive-$(date +%s)-$$"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

# ---------------------------------------------------------------- preconditions

step "preconditions"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -d "$TASK" ] || skip "no task at $TASK"
note "titanium env: $ENV_NAME"
note "task:         $TASK"
note "run id:       $RUN_ID"

WORK="$ROOT/.run/environment-archive/$(date +%Y%m%d_%H%M%S)-$$"
JOBS_DIR="$WORK/jobs"
mkdir -p "$JOBS_DIR" || skip "could not create a work directory"
note "work dir:     $WORK (kept: the exported tar is the artifact)"

# ------------------------------------------------------------------- the trial

step "trial: --on-completion archive"
note "running a real trial ..."
"$TITANIUM" run \
    --agent oracle \
    --env "$ENV_NAME" \
    --path "$TASK" \
    --jobs-dir "$JOBS_DIR" \
    --job-name "$RUN_ID" \
    --on-completion archive \
    --disable-verification \
    --yes >"$WORK/trial.log" 2>&1
TRIAL_RC=$?
note "trial exit: $TRIAL_RC"
if [ "$TRIAL_RC" -ne 0 ]; then
    tail -20 "$WORK/trial.log"
    fail "the archive trial exited $TRIAL_RC"
fi

# ----------------------------------------------------------------- the artifact

step "artifact"
TRIAL_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    ! -name '.*' 2>/dev/null | head -1)"
[ -n "$TRIAL_DIR" ] || fail "the trial left no trial directory under $JOBS_DIR/$RUN_ID"

TAR="$TRIAL_DIR/archive/environment.tar"
note "expected: $TAR"
[ -f "$TAR" ] || fail "no archive at $TAR"
note "exists:   yes ($(stat -c %s "$TAR") bytes)"

# ------------------------------------------------------- the archived guest state

step "archived guest state"
tar -tf "$TAR" >"$WORK/tar-listing.txt" 2>/dev/null \
    || fail "the published archive is not a readable tar"
note "entries: $(grep -c . "$WORK/tar-listing.txt")"

if ! MARKER_TEXT="$(tar -xOf "$TAR" "$MARKER_PATH" 2>/dev/null)"; then
    fail "the guest-written $MARKER_PATH is not in the archive"
fi
note "archived: $MARKER_PATH"

FOUND="${MARKER_TEXT%%$'\n'*}"
note "contents: '$FOUND' (expected '$MARKER_CONTENT')"
[ "$FOUND" = "$MARKER_CONTENT" ] \
    || fail "archived marker reads '$FOUND', expected '$MARKER_CONTENT'"

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: the $ENV_NAME archive carries the guest's own write at $MARKER_PATH"
echo
echo "OVERALL: PASS"
exit 0
