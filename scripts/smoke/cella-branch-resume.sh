#!/usr/bin/env bash
# The full resume proof, with a real model: mini-swe-agent (the org fork,
# which has --resume) starts the wedge task, hits the first leg's agent
# timeout mid-sleep, and `titanium branch` gives it a second leg with a
# bigger budget. The proof is threefold:
#
#   1. the parent leg recorded AgentTimeoutError -- the cut is real,
#   2. the leg's trajectory strictly extends the parent's pruned
#      messages -- one run, two legs, no reset, and
#   3. the wedge verifies: only a leg that outlived the full sleep can
#      have written the result.
#
# Needs a model key and the fork; without them nothing is proven (exit 2).
#
#   TITANIUM_MODEL         provider/model, the Makefile's own resolution
#   MSWEA_INSTALL_SOURCE   uv tool install source for the fork
#                          (default: the org fork's edge -- the rule of
#                          thumb: omne-earth ships from edge)
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TASK="${TITANIUM_SMOKE_TASK:-$ROOT/examples/smoke/cella/branch-wedge}"
TITANIUM="$ROOT/.venv/bin/titanium"
# The existing plumbing: the Makefile resolves TITANIUM_MODEL (from
# OPENROUTER_MODEL via the .secrets include) and passes it down.
MODEL="${TITANIUM_MODEL:-${OPENROUTER_MODEL:-}}"
INSTALL_SOURCE="${MSWEA_INSTALL_SOURCE:-git+https://github.com/omne-earth/mini-swe-agent@edge}"
TRAJ_PATH="./logs/agent/mini-swe-agent.trajectory.json"

RUN_ID="$(date +%Y-%m-%d__%H-%M-%S)"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

# ---------------------------------------------------------------- preconditions

step "preconditions"
[ -x "$TITANIUM" ] || skip "no titanium at $TITANIUM (run 'make sync')"
[ -d "$TASK" ] || skip "no task at $TASK"
[ -n "$MODEL" ] || skip "no model: TITANIUM_MODEL unresolved (set OPENROUTER_MODEL via .secrets)"
if [ -z "${OPENROUTER_API_KEY:-}${MSWEA_API_KEY:-}${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}" ]; then
    skip "no model key in the environment (.secrets exports OPENROUTER_API_KEY through make)"
fi
note "task:   $TASK"
note "model:  $MODEL"
note "fork:   $INSTALL_SOURCE"
note "run id: $RUN_ID"

JOBS_DIR="${SMOKE_JOBS_DIR:-$ROOT/.run/jobs/openrouter/smoke-cella-branch}"
WORK="${SMOKE_CACHE_DIR:-$ROOT/.run/cache/openrouter/smoke-cella-branch}/$RUN_ID"
mkdir -p "$JOBS_DIR" "$WORK" || skip "could not create the jobs and cache directories"
note "jobs dir:  $JOBS_DIR/$RUN_ID"
note "cache dir: $WORK"

# --------------------------------------------------------- the parent leg (cut)

step "parent leg: expected to time out mid-sleep"
"$TITANIUM" run \
    --agent mini-swe-agent \
    --model "$MODEL" \
    --ak "install_source=$INSTALL_SOURCE" \
    --env cella \
    --path "$TASK" \
    --jobs-dir "$JOBS_DIR" \
    --job-name "$RUN_ID" \
    --yes >"$WORK/parent.log" 2>&1
note "parent exit: $? (a timed-out trial is the expected shape)"

PARENT_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    ! -name '.*' 2>/dev/null | head -1)"
[ -n "$PARENT_DIR" ] || fail "the parent left no trial directory under $JOBS_DIR/$RUN_ID"
note "parent dir: $PARENT_DIR"

grep -q "AgentTimeoutError" "$PARENT_DIR/result.json" \
    || fail "the parent leg did not record AgentTimeoutError: the wedge never cut"
note "cut confirmed: AgentTimeoutError in the parent's result.json"

PARENT_TAR="$(find "$PARENT_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$PARENT_TAR" ] || fail "the parent left no state tar: nothing to branch from"
tar -xOf "$PARENT_TAR" "$TRAJ_PATH" >"$WORK/parent-traj.json" 2>/dev/null \
    || fail "no trajectory at $TRAJ_PATH in the parent's state tar"

# ------------------------------------------------------------- the resumed leg

step "branch leg: resumed with a 5x budget"
"$TITANIUM" branch -p "$PARENT_DIR" --agent-timeout-multiplier 5 \
    >"$WORK/branch.log" 2>&1
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

LEG_TAR="$(find "$LEG_DIR" -path '*/cella-env-*/state-*.tar' \
    ! -name 'state-0000.tar' 2>/dev/null | head -1)"
[ -n "$LEG_TAR" ] || fail "the leg left no state tar of its own"
tar -xOf "$LEG_TAR" "$TRAJ_PATH" >"$WORK/leg-traj.json" 2>/dev/null \
    || fail "no trajectory at $TRAJ_PATH in the leg's state tar"

# --------------------------------------------------- one run, two legs, no reset

step "trajectory continuity"
# The token stage 1 minted: it lives in leg-1's observation and on the
# disk, and a fresh restart could only mint a different one -- the
# nondeterministic content that makes the prefix check a discriminator.
TOKEN="$(tar -xOf "$LEG_TAR" ./app/token.txt 2>/dev/null | head -1 | tr -d '[:space:]')"
[ -n "$TOKEN" ] || fail "no /app/token.txt on the leg's disk: stage 1 never ran"
note "token: $TOKEN"
python3 - "$WORK/parent-traj.json" "$WORK/leg-traj.json" "$TOKEN" <<'PY' || fail "the leg's trajectory does not extend the parent's"
import json, sys

parent = json.load(open(sys.argv[1]))["messages"]
leg = json.load(open(sys.argv[2]))["messages"]
token = sys.argv[3]

# The resume prune, mirrored from the fork: back to the last
# observation in any message format (user / tool / response API).
def is_observation(m):
    return m.get("type") == "function_call_output" or m.get("role") in ("user", "tool")

while parent and not is_observation(parent[-1]):
    parent.pop()

# A parent cut before its first observation leaves nothing a restart
# could not regenerate: 2 template messages discriminate nothing. The
# wedge's stage 1 guarantees one real observation before the sleep.
assert len(parent) >= 4, (
    f"pruned parent has only {len(parent)} messages -- the cut landed "
    "before stage 1's observation; a task-compliance flake, not a "
    "resume verdict"
)
# The banked observation carries the token the disk carries: history
# and disk agree through the resume.
assert any(token in str(m.get("content", "")) for m in parent), (
    "the disk's token never appears in the pruned parent's messages"
)
assert len(leg) > len(parent), (
    f"leg trajectory ({len(leg)} messages) does not extend the pruned "
    f"parent ({len(parent)}): the agent reset instead of resuming"
)
for i, (a, b) in enumerate(zip(parent, leg)):
    assert a.get("role") == b.get("role") and a.get("content") == b.get("content"), (
        f"message {i} diverges between the legs: not a continuation"
    )
print(f"     pruned parent: {len(parent)} messages; leg: {len(leg)} -- a strict extension")
PY

# --------------------------------------------------------------------- the wedge

step "the wedge verifies"
grep -q '"reward"' "$LEG_DIR/result.json" 2>/dev/null || true
REWARD="$(tar -xOf "$LEG_TAR" ./app/results.txt 2>/dev/null | head -1)"
note "guest /app/results.txt: '${REWARD:-<absent>}' (expected 'done')"
[ "${REWARD:-}" = "done" ] \
    || fail "the resumed leg never finished the wedge: /app/results.txt is not 'done'"

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: the cut leg resumed as one run and finished what its budget had denied it"
echo
echo "OVERALL: PASS"
exit 0
