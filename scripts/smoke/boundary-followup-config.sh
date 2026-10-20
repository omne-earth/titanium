#!/usr/bin/env bash
# The boundary-followup config proof -- fully offline, no model, no cella, no
# network, no paid run. It proves the configuration plumbing of the DeepSeek/
# Mistral follow-up, which is all that can be wrong without actually running the
# study:
#
#   1. the 240s HTTP timeout reaches mini-swe-agent as `-c model.http_timeout=240`
#      on the real agent command Titanium builds (not smuggled into model_kwargs);
#   2. the existing default is preserved -- with no http_timeout kwarg, Titanium
#      emits no model.http_timeout flag, so mini-swe-agent keeps its own 60s;
#   3. model/task selection -- `make boundary-followup` (dry run) selects exactly
#      the two openrouter/-prefixed models, the follow-up task, and 240s;
#   4. the follow-up task carries a three-hour agent budget while preserving the
#      base study's verifier timeout and isolation policy (allow_internet=false);
#   5. the original cella-boundary-study task is untouched (still 600s);
#   6. that budget resolves to exactly 10800s -- and the follow-up's own execution
#      config passes no timeout multiplier, so the old 18x (600s*18) cannot
#      re-stack on top of the baked-in 3 hours;
#   7. the installed mini-swe-agent build is pinnable (an unfixed build silently
#      ignores http_timeout), and an unpinned dry run warns about it;
#   8. a paid run (CONFIRM=1) refuses an empty or moving-ref (@edge) build and
#      accepts only a pinned commit sha.
#
# This does NOT exercise the model's own HTTP client -- that it honours
# model.http_timeout is proven by mini-swe-agent's unit tests
# (tests/models/test_openrouter_textbased_model.py in the fork).
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
FOLLOWUP_TASK="$ROOT/examples/tasks/cella-boundary-study-followup"
BASE_TASK="$ROOT/examples/tasks/cella-boundary-study"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; echo; echo "OVERALL: FAIL"; exit 1; }

step "preconditions"
[ -x "$PY" ] || skip "no venv python at $PY (run 'make sync')"
[ -d "$FOLLOWUP_TASK" ] || skip "no follow-up task at $FOLLOWUP_TASK"
command -v make >/dev/null || skip "make not on PATH"
note "root: $ROOT"

# ---------------------------------------------------------- the config flag path
step "1-2. http_timeout flows to -c model.http_timeout (and default is preserved)"
"$PY" - "$ROOT" <<'PYEOF' || fail "config-flag assertions failed (see above)"
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from titanium.agents.installed.mini_swe_agent import MiniSweAgent  # noqa: E402

model = "openrouter/deepseek/deepseek-v4.1-flash-1017"

with_timeout = MiniSweAgent(logs_dir=root / ".run" / "smoke-tmp", model_name=model, http_timeout=240)
flags = with_timeout._build_config_flags()
assert "-c model.http_timeout=240 " in flags, f"missing http_timeout flag in: {flags!r}"
assert "model.model_kwargs.http_timeout" not in flags, "http_timeout leaked into model_kwargs (API payload)"

default = MiniSweAgent(logs_dir=root / ".run" / "smoke-tmp", model_name=model)
assert "model.http_timeout" not in default._build_config_flags(), "default run should not pin http_timeout"

print("     ok: 240 emitted as a top-level model config flag; absent by default")
PYEOF
note "ok"

# ----------------------------------------------------- model/task selection (dry)
step "3. make boundary-followup (dry run) selects the two models, task, and 240s"
DRY="$(cd "$ROOT" && make boundary-followup 2>&1)" || fail "make boundary-followup dry run errored"
echo "$DRY" | sed 's/^/     /'
for needle in \
    "openrouter/deepseek/deepseek-v4.1-flash-1017" \
    "openrouter/mistralai/mistral-large-4-0" \
    "cella-boundary-study-followup" \
    "240s" \
    "CONFIRM=1"; do
    echo "$DRY" | grep -qF "$needle" || fail "dry run missing expected text: $needle"
done
# It must stay a dry run: no real titanium invocation without CONFIRM=1.
echo "$DRY" | grep -q "titanium run" && fail "dry run should not invoke 'titanium run'"
note "ok: exactly the follow-up models/task/timeout, and still a dry run"

# ------------------------------------------------------ the follow-up task budget
step "4. follow-up task: 3h agent budget, isolation + verifier preserved"
"$PY" - "$FOLLOWUP_TASK" "$BASE_TASK" <<'PYEOF' || fail "task.toml assertions failed (see above)"
import sys, tomllib
from pathlib import Path

followup = tomllib.loads((Path(sys.argv[1]) / "task.toml").read_text())
base = tomllib.loads((Path(sys.argv[2]) / "task.toml").read_text())

assert followup["agent"]["timeout_sec"] == 10800.0, followup["agent"]["timeout_sec"]
assert followup["environment"]["allow_internet"] is False
# Isolation / resources / verifier preserved against the base study.
for key in ("allow_internet", "cpus", "memory_mb", "storage_mb", "build_timeout_sec"):
    assert followup["environment"][key] == base["environment"][key], key
assert followup["verifier"]["timeout_sec"] == base["verifier"]["timeout_sec"]
assert followup["task"]["name"] != base["task"]["name"], "variant must have a distinct task name"
print("     ok: agent budget 10800s; verifier/isolation/resources match the base study")
PYEOF
note "ok"

# ------------------------------------------------------------ the original frozen
step "5. the original cella-boundary-study task is unchanged (still 600s)"
"$PY" - "$BASE_TASK" <<'PYEOF' || fail "base task.toml changed unexpectedly"
import sys, tomllib
from pathlib import Path
base = tomllib.loads((Path(sys.argv[1]) / "task.toml").read_text())
assert base["agent"]["timeout_sec"] == 600.0, base["agent"]["timeout_sec"]
assert base["task"]["name"] == "titanium/cella-boundary-study"
print("     ok: base study still 600s under its original name")
PYEOF
note "ok"

# --------------------------------------- the budget resolves to exactly 3 hours
step "6. agent budget resolves to exactly 10800s (no stacked multiplier)"
"$PY" - "$FOLLOWUP_TASK" <<'PYEOF' || fail "budget resolution assertions failed (see above)"
import sys, tomllib
from pathlib import Path
from titanium.models.trial.config import TrialConfig

followup = tomllib.loads((Path(sys.argv[1]) / "task.toml").read_text())
budget = followup["agent"]["timeout_sec"]
fields = TrialConfig.model_fields
# Defaults that boundary-followup relies on: nothing multiplies the budget.
assert fields["timeout_multiplier"].default == 1.0, fields["timeout_multiplier"].default
assert fields["agent_timeout_multiplier"].default is None, fields["agent_timeout_multiplier"].default
# Mirror trial._resolve_step_timeout with the target's inputs (no override, no
# max, default multipliers): min(base, inf) * 1.0.
effective = min(budget, float("inf")) * 1.0
assert effective == 10800.0, effective
print(f"     ok: {budget}s * 1.0 = {effective}s = 3h exactly")
PYEOF
# And nothing boundary-followup actually passes to a run wires a multiplier.
# Narrow to the follow-up's own execution config -- the extra-args variable, the
# boundary-followup recipe, and the base run command it calls -- so an unrelated
# target that legitimately uses a multiplier cannot trip this.
BF_CONFIG="$(
    grep -E '^(BOUNDARY_FOLLOWUP_EXTRA_ARGS|BOUNDARY_FOLLOWUP_HTTP_TIMEOUT|TITANIUM_RUN)\b' "$ROOT/Makefile"
    awk '/^boundary-followup:/{f=1} /^smoke-boundary-followup:/{f=0} f' "$ROOT/Makefile"
)"
printf '%s' "$BF_CONFIG" | grep -qE -- '--(agent-)?timeout-multiplier' \
    && fail "the boundary-followup execution config passes a timeout multiplier -- it could re-stack the old 18x" \
    || note "ok: boundary-followup passes no --timeout-multiplier / --agent-timeout-multiplier"

# --------------------------------------- the mini-swe-agent build is pinnable
step "7. the mini-swe-agent build is pinnable (and unpinned runs warn)"
PINNED="$(cd "$ROOT" && make boundary-followup BOUNDARY_FOLLOWUP_MSWEA_SOURCE='git+https://github.com/omne-earth/mini-swe-agent@deadbeef' 2>&1)" \
    || fail "pinned dry run errored"
echo "$PINNED" | grep -qF "git+https://github.com/omne-earth/mini-swe-agent@deadbeef" \
    || fail "pinned dry run did not surface the pinned mswea source"
echo "$DRY" | grep -q "WARNING: the 240s timeout takes" \
    || fail "unpinned dry run did not warn that an unfixed build ignores http_timeout"
note "ok: pinned source surfaced; unpinned run warns"

# ------------------------------- a paid run demands a pinned commit (item: guard)
step "8. CONFIRM=1 refuses an unpinned / moving-ref build, accepts a pinned sha"
# TITANIUM=/bin/true neutralises any actual invocation; the refusal cases exit
# before the run loop anyway, and the acceptance case uses an empty model list so
# the loop is a no-op -- nothing is installed or launched here.
run_bf() { (cd "$ROOT" && make boundary-followup CONFIRM=1 TITANIUM=/bin/true "$@" 2>&1); }

run_bf >/tmp/bf-guard-empty.out; rc=$?
[ "$rc" -ne 0 ] || fail "CONFIRM=1 with an empty source was not refused (exit $rc)"
grep -q "refusing a paid run" /tmp/bf-guard-empty.out || fail "empty-source refusal message missing"

run_bf BOUNDARY_FOLLOWUP_MSWEA_SOURCE='git+https://github.com/omne-earth/mini-swe-agent@edge' \
    >/tmp/bf-guard-edge.out; rc=$?
[ "$rc" -ne 0 ] || fail "CONFIRM=1 with a moving @edge ref was not refused (exit $rc)"
grep -q "refusing a paid run" /tmp/bf-guard-edge.out || fail "@edge refusal message missing"

# A full-sha pin passes the guard; empty model list keeps the loop a no-op.
run_bf BOUNDARY_FOLLOWUP_MSWEA_SOURCE='git+https://github.com/omne-earth/mini-swe-agent@0123456789abcdef0123456789abcdef01234567' \
    BOUNDARY_FOLLOWUP_MODELS='' >/tmp/bf-guard-sha.out; rc=$?
[ "$rc" -eq 0 ] || { cat /tmp/bf-guard-sha.out; fail "CONFIRM=1 with a pinned sha was rejected (exit $rc)"; }
grep -q "refusing a paid run" /tmp/bf-guard-sha.out && fail "a pinned sha was wrongly refused"
grep -qiE 'boundary-followup MODEL=|titanium run' /tmp/bf-guard-sha.out && fail "guard-accept path unexpectedly launched a run"
note "ok: empty and @edge refused (exit!=0); pinned sha accepted without launching"

echo
echo "OVERALL: PASS"
exit 0
