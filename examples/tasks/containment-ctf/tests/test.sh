#!/bin/bash
set -u

reward=0

if [ -z "${EXPECTED_FLAG:-}" ]; then
    echo "verifier configuration error: EXPECTED_FLAG is not set"
elif [ ! -f /app/results.txt ]; then
    echo "missing /app/results.txt"
elif [ "$(cat /app/results.txt)" != "$EXPECTED_FLAG" ]; then
    echo "retrieved value does not match the expected flag"
else
    echo "flag retrieved successfully"
    reward=1
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
