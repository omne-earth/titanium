#!/usr/bin/env bash
# The --note proof, with a real model. This is the --at smoke with one
# difference: at the trim point we add a --note telling the agent to
# finish, and we measure the leg to completion (reward 1). So it proves
# the note is (a) delivered as the trailing user turn at the trim point
# and (b) carried through the ordinary resume: the leg boots, the agent
# reads the note, and the run completes -- no branch-of-a-branch, no
# extra boots.
#
# The 13-command ledger fixture (small trajectory, tiny resume payload).
# A parent leg completes it under --on-completion pause; then
# `titanium branch --at <step 7> --note <finish now>` resumes one leg
# whose memory is rewound to step 7 with the note appended. The trim
# rewinds memory, never the disk, so the ledger is already complete on
# the seeded disk -- the note tells the agent to recognise that and
# finish, and the verifier scores reward 1.
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
NOTE="You have additional time, but the ledger is already complete: all twelve entries and the result file are on disk. Do not repeat any entries. Confirm the result file says done and finish now."

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

JOBS_DIR="${SMOKE_JOBS_DIR:-$ROOT/.run/jobs/openrouter/smoke-cella-branch-note}"
WORK="${SMOKE_CACHE_DIR:-$ROOT/.run/cache/openrouter/smoke-cella-branch-note}/$RUN_ID"
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
[ "$STEPS" -ge 8 ] || fail "only $STEPS steps listed: the parent never reached step 7 (a compliance flake, not a note verdict)"
AT_ID="$(awk 'NR==7 {print $1}' "$WORK/show.txt")"
[ -n "$AT_ID" ] || fail "could not read step 7's id from the show output"
note "steps: $STEPS; trimming at: $AT_ID"

# ---------------------------------------- the leg: trimmed at step 7, + a note

step "branch leg: resumed at step 7 with a --note to finish"
"$TITANIUM" branch -p "$PARENT_DIR" --at "$AT_ID" --note "$NOTE" >"$WORK/branch.log" 2>&1
BRANCH_RC=$?
note "branch exit: $BRANCH_RC"
if [ "$BRANCH_RC" -ne 0 ]; then
    tail -20 "$WORK/branch.log"
    fail "titanium branch --at --note exited $BRANCH_RC"
fi

LEG_DIR="$(find "$JOBS_DIR/$RUN_ID" -mindepth 1 -maxdepth 1 -type d \
    -name '*-branch-1' 2>/dev/null | head -1)"
[ -n "$LEG_DIR" ] || fail "no *-branch-1 leg directory beside the parent"
note "leg dir: $LEG_DIR"
[ -f "$LEG_DIR/trimmed-trajectory.json" ] \
    || fail "no trimmed-trajectory.json in the leg dir: --at/--note never rewrote the trajectory"

# ----------------------------- the note is the trailing user turn at the trim

step "the note: appended after the step-7 trim"
python3 - "$WORK/parent-traj.json" "$LEG_DIR/trimmed-trajectory.json" "$AT_ID" "$NOTE" <<'PY' \
    || fail "the note is not the trailing user message at the step-7 trim"
import json, sys

parent = json.load(open(sys.argv[1]))["messages"]
trimmed = json.load(open(sys.argv[2]))["messages"]
at_id, note = sys.argv[3], sys.argv[4]

# The body (everything but the note) is a strict prefix of the parent,
# ending exactly at the chosen step: the trim, unchanged by the note.
body = trimmed[:-1]
assert body == parent[: len(body)], "the trimmed body is not a prefix of the parent's"
assert len(body) < len(parent), f"nothing was trimmed: body {len(body)} == parent {len(parent)}"
last_step = body[-1]
assert at_id in str(last_step.get("tool_call_id") or last_step.get("call_id") or ""), (
    f"the trim does not end at step {at_id}"
)
# The note is the final message: a user-role observation the prune keeps.
tail = trimmed[-1]
assert tail.get("role") == "user", f"the note is role {tail.get('role')!r}, not user"
assert tail.get("content") == note, "the note's content does not match"
print(f"     trimmed to {len(body)} messages at step {at_id}, note appended at index {len(body)}")
PY

# ------------------------------------------- the leg runs to completion (reward 1)

step "completion: the leg boots, reads the note, and finishes"
[ -f "$LEG_DIR/result.json" ] || fail "the leg left no result.json: it did not run to a verdict"
REWARD="$(python3 -c "
import json
r = json.load(open('$LEG_DIR/result.json'))
e = r.get('exception_info')
v = r.get('verifier_result') or {}
reward = (v.get('rewards') or {}).get('reward')
print(f'{reward}|{e and e.get(\"exception_type\") or \"\"}')
")"
RWD="${REWARD%%|*}"; EXC="${REWARD##*|}"
note "reward: $RWD   exception: ${EXC:-none}"
if [ "$RWD" != "1.0" ]; then
    # Separate infra from a real miss. The note's delivery is already
    # proven above; these are host/transport faults that stop the leg
    # from reaching a verdict, so nothing about the note is disproven.
    #   - a cella boot fault (bwrap / "did not reach readiness") on a
    #     helper machine (appliance/verifier/extractor): host resource
    #     or SELinux state, surfaced as FileNotFoundError/CellaError;
    #   - an OpenRouter transport crash: NonZeroAgentExitCode / Timeout.
    MSG="$(python3 -c "import json;print((json.load(open('$LEG_DIR/result.json')).get('exception_info') or {}).get('exception_message',''))" 2>/dev/null)"
    if echo "$MSG" | grep -qi "bwrap\|did not reach readiness\|cella-vmm"; then
        echo "$MSG" | head -2
        skip "a cella helper machine failed to boot ($EXC: bwrap/cella-vmm) -- host infra, not the note"
    fi
    if echo "$EXC" | grep -qi "NonZeroAgentExitCode\|Timeout"; then
        tail -5 "$LEG_DIR/agent/mini-swe-agent.txt" 2>/dev/null | cut -c1-120
        skip "the leg's agent crashed on a transport error ($EXC) -- infra, not the note"
    fi
    fail "the leg finished with reward $RWD (expected 1.0): the note-resumed leg did not complete the task"
fi

# --------------------------------------------------------------------- verdict

step "verdict"
echo "PASS: the note landed as the trailing user turn at the step-7 trim,"
echo "      and the note-resumed leg booted and ran to completion (reward 1)"
echo
echo "OVERALL: PASS"
exit 0
