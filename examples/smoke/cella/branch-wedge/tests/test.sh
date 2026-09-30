#!/bin/bash
set -u

# The wedge: only a leg that outlived the full sleep can have written this.
reward=0

if [ -f /app/results.txt ] && [ "$(cat /app/results.txt)" = "done" ]; then
    reward=1
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
