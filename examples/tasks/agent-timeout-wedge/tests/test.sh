#!/bin/bash
set -u

# This task exists to reproduce an agent timeout, not to be solved. The agent is
# expected to block and hit its agent timeout before writing results. Reward is
# 0 unless it somehow completes; the recorded AgentTimeoutError is the artifact
# of interest.
reward=0

if [ -f /app/results.txt ] && [ "$(cat /app/results.txt)" = "done" ]; then
    reward=1
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
