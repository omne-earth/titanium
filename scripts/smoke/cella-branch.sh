#!/usr/bin/env bash
# The branch continuity proof, model-free: a parent oracle trial appends
# one line to /app/branch-log.txt, `titanium branch` bakes a new leg from
# the parent's state tar, the leg's oracle appends again -- and the leg's
# own state extract shows both lines. Evidence became new life: the leg
# booted from the parent's disk, not from a fresh image.
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/smoke/cella-branch-marker}"
TITANIUM="$ROOT/.venv/bin/titanium"

MARKER_PATH="./app/branch-log.txt"
RUN_ID="cella-branch-$(date +%s)-$$"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

# ---------------------------------------------------------------- preconditions

step "preconditions"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -d "$TASK" ] || skip "no task at $TASK"
note "task:   $TASK"
note "run id: $RUN_ID"

WORK="$ROOT/.run/cella-branch/$(date +%Y%m%d_%H%M%S)-$$"
JOBS_DIR="$WORK/jobs"
mkdir -p "$JOBS_DIR" || skip "could not create a work directory"
note "work dir: $WORK"

# -------------------------------------------------------------- the parent leg

step "parent leg"
"$TITANIUM" run \
    --agent oracle \
    --env cella \
    --path "$TASK" \
    --jobs-dir "$JOBS_DIR" \
    --job-name "$RUN_ID" \
    --disable-verification \
    --yes >"$WORK/parent.log" 2>&1
PARENT_RC=$?
note "parent exit: $PARENT_RC"
if [ "$PARENT_RC" -ne 0 ]; then
    tail -20 "$WORK/parent.log"
    fail "the parent trial exited $PARENT_RC"
fi

PARENT_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    ! -name '.*' 2>/dev/null | head -1)"
[ -n "$PARENT_DIR" ] || fail "the parent left no trial directory under $JOBS_DIR/$RUN_ID"
note "parent dir: $PARENT_DIR"

PARENT_TAR="$(find "$PARENT_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$PARENT_TAR" ] || fail "the parent left no state tar: nothing to branch from"

PARENT_LINES="$(tar -xOf "$PARENT_TAR" "$MARKER_PATH" 2>/dev/null | grep -c '^leg')"
note "parent marker lines: $PARENT_LINES (expected 1)"
[ "$PARENT_LINES" = "1" ] || fail "the parent leg wrote $PARENT_LINES marker lines, expected 1"

# --------------------------------------------------------------- the branch leg

step "branch leg: titanium branch"
"$TITANIUM" branch -p "$PARENT_DIR" >"$WORK/branch.log" 2>&1
BRANCH_RC=$?
note "branch exit: $BRANCH_RC"
if [ "$BRANCH_RC" -ne 0 ]; then
    tail -20 "$WORK/branch.log"
    fail "titanium branch exited $BRANCH_RC"
fi

LEG_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    -name '*-branch-1' 2>/dev/null | head -1)"
[ -n "$LEG_DIR" ] || fail "no *-branch-1 leg directory beside the parent"
note "leg dir: $LEG_DIR"

# ------------------------------------------------------------------- continuity

step "continuity"
LEG_TAR="$(find "$LEG_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$LEG_TAR" ] || fail "the leg left no state tar of its own"
note "leg state tar: $LEG_TAR"

LEG_LINES="$(tar -xOf "$LEG_TAR" "$MARKER_PATH" 2>/dev/null | grep -c '^leg')"
note "leg marker lines: $LEG_LINES (expected 2: the parent's and its own)"
[ "$LEG_LINES" = "2" ] \
    || fail "the leg shows $LEG_LINES marker lines: it did not boot from the parent's disk"

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: the branch leg booted from the parent's disk and carried its write forward"
echo
echo "OVERALL: PASS"
exit 0
