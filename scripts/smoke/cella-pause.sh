#!/usr/bin/env bash
# The pause acceptance proof: one real cella trial with
# `--on-completion pause`, then two host-side facts:
#
#   1. the member machine survives, still and NOT archived -- its
#      manifest never latched `state: archived`, so `cella branch`
#      of it yields a fresh-bootable copy, and
#   2. the trial's work dir holds the branch leg's evidence: the
#      member state tar and the persisted image-config.json.
#
# The agent is `oracle` and verification is off, so nothing here
# depends on a model.
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/smoke/cella-branch-marker}"
TITANIUM="$ROOT/.venv/bin/titanium"
CELLA="${CELLA_BIN:-$HOME/.cella/bin/cella}"
MACHINES="$HOME/.cella/machines"

RUN_ID="cella-pause-$(date +%s)-$$"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

# ---------------------------------------------------------------- preconditions

step "preconditions"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -x "$CELLA" ] || skip "no cella at $CELLA (run 'make .cella')"
[ -d "$TASK" ] || skip "no task at $TASK"
note "task:   $TASK"
note "run id: $RUN_ID"

WORK="$ROOT/.run/cella-pause/$(date +%Y%m%d_%H%M%S)-$$"
JOBS_DIR="$WORK/jobs"
mkdir -p "$JOBS_DIR" || skip "could not create a work directory"
note "work dir: $WORK"

# The machines that already exist are not ours; only the diff is evidence.
ls -1 "$MACHINES" 2>/dev/null | sort > "$WORK/machines-before.txt"

cleanup() {
    # Best-effort: the paused machines this run left are the proof, not
    # a keepsake -- destroy them so re-runs stay clean.
    if [ -s "$WORK/machines-new.txt" ]; then
        while read -r m; do
            "$CELLA" destroy "$m" >/dev/null 2>&1 || true
        done < "$WORK/machines-new.txt"
    fi
}
trap cleanup EXIT

# ------------------------------------------------------------------- the trial

step "trial: --on-completion pause"
"$TITANIUM" run \
    --agent oracle \
    --env cella \
    --path "$TASK" \
    --jobs-dir "$JOBS_DIR" \
    --job-name "$RUN_ID" \
    --on-completion pause \
    --disable-verification \
    --yes >"$WORK/trial.log" 2>&1
TRIAL_RC=$?
note "trial exit: $TRIAL_RC"
if [ "$TRIAL_RC" -ne 0 ]; then
    tail -20 "$WORK/trial.log"
    fail "the pause trial exited $TRIAL_RC"
fi

TRIAL_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    ! -name '.*' 2>/dev/null | head -1)"
[ -n "$TRIAL_DIR" ] || fail "the trial left no trial directory under $JOBS_DIR/$RUN_ID"
note "trial dir: $TRIAL_DIR"

# `titanium run` exits 0 even when the trial recorded an exception; the
# trial's own record is the fact.
if [ -f "$TRIAL_DIR/exception.txt" ]; then
    sed -n 1,3p "$TRIAL_DIR/exception.txt"
    fail "the trial recorded an exception"
fi

# -------------------------------------------------------- the surviving machine

step "the paused machine"
ls -1 "$MACHINES" 2>/dev/null | sort > "$WORK/machines-after.txt"
comm -13 "$WORK/machines-before.txt" "$WORK/machines-after.txt" \
    > "$WORK/machines-new.txt"
NEW_COUNT="$(grep -c . "$WORK/machines-new.txt" || true)"
note "machines surviving the trial: $NEW_COUNT"
[ "$NEW_COUNT" -ge 1 ] || fail "pause left no machine behind: teardown ran instead"

while read -r m; do
    MANIFEST="$MACHINES/$m/manifest.json"
    [ -f "$MANIFEST" ] || fail "surviving machine $m has no manifest"
    if grep -q '"state"[[:space:]]*:[[:space:]]*"archived"' "$MANIFEST"; then
        fail "machine $m latched state=archived: that is archive, not pause"
    fi
    note "still and branchable: $m"
done < "$WORK/machines-new.txt"

# ------------------------------------------------------------------ the evidence

step "the branch evidence"
STATE_TAR="$(find "$TRIAL_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$STATE_TAR" ] || fail "no member state tar under $TRIAL_DIR/cella-env-*/"
[ -s "$STATE_TAR" ] || fail "the member state tar is empty: the state extract never ran"
note "state tar: $STATE_TAR ($(stat -c %s "$STATE_TAR") bytes)"

IMAGE_CONFIG="$(dirname "$STATE_TAR")/image-config.json"
[ -f "$IMAGE_CONFIG" ] || fail "no image-config.json beside the state tar"
note "image config: $IMAGE_CONFIG"

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: pause kept the machine still (never archived) and left the leg's evidence"
echo
echo "OVERALL: PASS"
exit 0
