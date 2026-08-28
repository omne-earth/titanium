#!/bin/bash
# Offline verifier: the base image ships python3 and the checks are
# stdlib-only, so no network is needed — this runs under
# allow_internet=false, where the stock fetch-uv-and-pytest verifier would
# fail at the first curl. Pattern from examples/smoke/fix-git-offline.
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
