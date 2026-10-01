#!/usr/bin/env bash
# The --at trim proof, with a real model: mini-swe-agent (the org fork,
# which has --resume) completes the 13-command ledger task in a parent
# leg run under --on-completion pause, `titanium branch show` lists the
# leg's steps by tool_call_id, and `titanium branch --at <step 7>`
# resumes a leg whose memory is rewound to step 7. The proof is:
#
#   1. show lists the steps the trajectory holds, ids and all,
#   2. the leg's trajectory extends EXACTLY the parent's first seven
#      steps -- the trim cut the later ones out of the resume, and
#   3. the leg's disk still carries step-12's ledger line at boot --
#      the trim rewinds memory, never the disk, as documented.
#
# Needs a model key and the fork; without them nothing is proven (exit 2).
#
#   TITANIUM_MODEL         provider/model, the Makefile's own resolution
#   MSWEA_INSTALL_SOURCE   uv tool install source for the fork
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/smoke/cella/branch-ledger}"
TITANIUM="$ROOT/.venv/bin/titanium"
CELLA="${CELLA_BIN:-$HOME/.cella/bin/cella}"
MACHINES="$HOME/.cella/machines"
MODEL="${TITANIUM_MODEL:-${OPENROUTER_MODEL:-}}"
# Install from edge's current sha, not the moving name: the agent
# install is a cached image layer keyed on the command string.
FORK_REPO="https://github.com/omne-earth/mini-swe-agent"
EDGE_SHA="$(git ls-remote "$FORK_REPO" edge 2>/dev/null | cut -f1)"
INSTALL_SOURCE="${MSWEA_INSTALL_SOURCE:-git+$FORK_REPO@${EDGE_SHA:-edge}}"
TRAJ_PATH="./logs/agent/mini-swe-agent.trajectory.json"

RUN_ID="$(date +%Y-%m-%d__%H-%M-%S)"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

# ---------------------------------------------------------------- preconditions

step "preconditions"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -x "$CELLA" ] || skip "no cella at $CELLA (run 'make .cella')"
[ -d "$TASK" ] || skip "no task at $TASK"
[ -n "$MODEL" ] || skip "no model: TITANIUM_MODEL unresolved (set OPENROUTER_MODEL via .secrets)"
if [ -z "${OPENROUTER_API_KEY:-}${MSWEA_API_KEY:-}${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}" ]; then
    skip "no model key in the environment (.secrets exports OPENROUTER_API_KEY through make)"
fi
note "task:   $TASK"
note "model:  $MODEL"
note "fork:   $INSTALL_SOURCE"
note "run id: $RUN_ID"

JOBS_DIR="${SMOKE_JOBS_DIR:-$ROOT/.run/jobs/openrouter/smoke-cella-branch-at}"
WORK="${SMOKE_CACHE_DIR:-$ROOT/.run/cache/openrouter/smoke-cella-branch-at}/$RUN_ID"
mkdir -p "$JOBS_DIR" "$WORK" || skip "could not create the jobs and cache directories"
note "jobs dir:  $JOBS_DIR/$RUN_ID"
note "cache dir: $WORK"

# The paused machines both legs leave are the proof, not a keepsake:
# diff against the pre-run set and destroy ours on the way out.
ls -1 "$MACHINES" 2>/dev/null | sort > "$WORK/machines-before.txt"
cleanup() {
    ls -1 "$MACHINES" 2>/dev/null | sort > "$WORK/machines-after.txt"
    comm -13 "$WORK/machines-before.txt" "$WORK/machines-after.txt" \
        > "$WORK/machines-new.txt"
    if [ -s "$WORK/machines-new.txt" ]; then
        while read -r m; do
            "$CELLA" destroy "$m" >/dev/null 2>&1 || true
        done < "$WORK/machines-new.txt"
    fi
}
trap cleanup EXIT

# -------------------------------------------------- the parent leg (completes)

step "parent leg: the full ledger, paused at the end"
"$TITANIUM" run \
    --agent mini-swe-agent \
    --model "$MODEL" \
    --ak "install_source=$INSTALL_SOURCE" \
    --env cella \
    --on-completion pause \
    --path "$TASK" \
    --jobs-dir "$JOBS_DIR" \
    --job-name "$RUN_ID" \
    --yes >"$WORK/parent.log" 2>&1
note "parent exit: $?"

PARENT_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    ! -name '.*' 2>/dev/null | head -1)"
[ -n "$PARENT_DIR" ] || fail "the parent left no trial directory under $JOBS_DIR/$RUN_ID"
note "parent dir: $PARENT_DIR"

PARENT_TAR="$(find "$PARENT_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$PARENT_TAR" ] || fail "the parent left no state tar: nothing to branch from"
tar -xOf "$PARENT_TAR" "$TRAJ_PATH" >"$WORK/parent-traj.json" 2>/dev/null \
    || fail "no trajectory at $TRAJ_PATH in the parent's state tar"

# ------------------------------------------------------------------- the ledger

step "branch show: the step list"
"$TITANIUM" branch show -p "$PARENT_DIR" >"$WORK/show.txt" 2>&1 \
    || { cat "$WORK/show.txt"; fail "titanium branch show failed"; }
cat "$WORK/show.txt"
STEPS="$(wc -l < "$WORK/show.txt")"
# 13 compliant commands; anything under 8 cannot carry a step-7 trim.
# A short list is a task-compliance flake, but with no step 7 there is
# no trim to prove either way.
[ "$STEPS" -ge 8 ] || fail "only $STEPS steps listed: the parent never reached step 7 (a compliance flake, not a trim verdict)"
AT_ID="$(awk 'NR==7 {print $1}' "$WORK/show.txt")"
[ -n "$AT_ID" ] || fail "could not read step 7's id from the show output"
note "steps: $STEPS; trimming at: $AT_ID"

# --------------------------------------------------------- the trimmed leg

step "branch leg: resumed at step 7"
"$TITANIUM" branch -p "$PARENT_DIR" --at "$AT_ID" >"$WORK/branch.log" 2>&1
BRANCH_RC=$?
note "branch exit: $BRANCH_RC"
if [ "$BRANCH_RC" -ne 0 ]; then
    tail -20 "$WORK/branch.log"
    fail "titanium branch --at exited $BRANCH_RC"
fi

LEG_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    -name '*-branch-1' 2>/dev/null | head -1)"
[ -n "$LEG_DIR" ] || fail "no *-branch-1 leg directory beside the parent"
note "leg dir: $LEG_DIR"
[ -f "$LEG_DIR/trimmed-trajectory.json" ] \
    || fail "no trimmed-trajectory.json in the leg dir: --at never trimmed"

LEG_TAR="$(find "$LEG_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$LEG_TAR" ] || fail "the leg left no state tar of its own"
tar -xOf "$LEG_TAR" "$TRAJ_PATH" >"$WORK/leg-traj.json" 2>/dev/null \
    || fail "no trajectory at $TRAJ_PATH in the leg's state tar"
# The leg's base tar (state-0000) is the parent's disk as seeded: the
# place to see that the trim left the disk at the parent's tip.
LEG_BASE="$(find "$LEG_DIR" -path '*/cella-env-*/state-0000.tar' 2>/dev/null | head -1)"
[ -n "$LEG_BASE" ] || fail "the leg has no seeded base tar"

# ---------------------------------------------- memory rewound, disk at the tip

step "the trim: memory rewound to step 7, disk still at the tip"
tar -xOf "$LEG_BASE" ./app/ledger.txt >"$WORK/ledger-at-boot.txt" 2>/dev/null \
    || fail "no /app/ledger.txt on the leg's seeded disk"
grep -q '^step-12$' "$WORK/ledger-at-boot.txt" \
    || fail "step-12 missing from the seeded disk: the parent never finished, or the trim touched the disk"
note "seeded disk carries all $(wc -l < "$WORK/ledger-at-boot.txt") ledger lines (the tip, as documented)"

python3 - "$WORK/parent-traj.json" "$LEG_DIR/trimmed-trajectory.json" \
    "$WORK/leg-traj.json" "$AT_ID" <<'PY' || fail "the leg does not extend the step-7 trim"
import json, sys

parent = json.load(open(sys.argv[1]))["messages"]
trimmed = json.load(open(sys.argv[2]))["messages"]
leg = json.load(open(sys.argv[3]))["messages"]
at_id = sys.argv[4]

# The trim cut something real and ends at exactly the chosen step.
assert len(trimmed) < len(parent), (
    f"trimmed ({len(trimmed)}) is not shorter than the parent "
    f"({len(parent)}): nothing was cut"
)
last = trimmed[-1]
assert at_id in str(last.get("tool_call_id") or last.get("call_id") or ""), (
    f"the trimmed trajectory does not end at step {at_id}"
)
assert trimmed == parent[: len(trimmed)], (
    "the trimmed trajectory is not a prefix of the parent's: the trim rewrote history"
)

# The leg extends the trim, not the full parent: step 8+ of the parent
# is gone from the leg's memory.
assert len(leg) > len(trimmed), (
    f"leg ({len(leg)}) does not extend the trim ({len(trimmed)}): the agent reset"
)
for i, (a, b) in enumerate(zip(trimmed, leg)):
    assert a.get("role") == b.get("role") and a.get("content") == b.get("content"), (
        f"message {i} diverges between the trim and the leg: not a continuation"
    )
cut = parent[len(trimmed)]
resumed = leg[len(trimmed)]
assert str(cut.get("content")) != str(resumed.get("content")) or str(
    cut.get("extra")
) != str(resumed.get("extra")), (
    "the leg's first new message is byte-identical to the parent's cut "
    "step 8: the trim resumed the untrimmed history"
)
print(f"     parent: {len(parent)} messages; trimmed: {len(trimmed)}; leg: {len(leg)} -- a strict extension of the trim")
PY

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: show listed the ledger, --at rewound the memory to step 7, and the leg grew from there"
echo
echo "OVERALL: PASS"
exit 0
