#!/bin/bash
set -u

# Reproduction task: the agent is expected to wait indefinitely and hit its
# agent timeout. The recorded AgentTimeoutError is the artifact of interest.
reward=0

if [ -f /app/results.txt ] && [ "$(cat /app/results.txt)" = "done" ]; then
    reward=1
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
