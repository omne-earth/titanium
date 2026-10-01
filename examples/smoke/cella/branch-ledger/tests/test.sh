#!/bin/bash
set -u

# The ledger is complete when all twelve entries are present in order
# and the result latch was written last.
reward=0

if [ -f /app/results.txt ] && [ "$(cat /app/results.txt)" = "done" ]; then
    expected="$(for n in $(seq 1 12); do echo "step-$n"; done)"
    if [ "$(grep -o 'step-[0-9]*' /app/ledger.txt 2>/dev/null | head -12)" = "$expected" ]; then
        reward=1
    fi
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
