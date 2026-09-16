#!/bin/bash
# Offline verifier: stdlib-only checks (the pattern from
# examples/smoke/fix-git-offline). The verifier's own machine is also
# judged under the same cella.policy, which is itself part of the
# proof: its granted re-probe must be released too.
mkdir -p /logs/verifier

if python3 - <<'PY'
import sys
sys.path.insert(0, "/tests")
import test_outputs as t

failures = []
for name in sorted(n for n in dir(t) if n.startswith("test_")):
    try:
        getattr(t, name)()
    except AssertionError as e:
        failures.append(f"{name}: {e}")

for f in failures:
    print(f)
sys.exit(1 if failures else 0)
PY
then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
