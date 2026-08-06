#!/bin/bash
set -u

# Independently re-derives ground truth for every claim the agent could make,
# and never trusts /app/probe/report.txt on its own: reward depends on the
# actual filesystem state and the verifier's own connection/resolution
# attempts, with the agent's report only cross-checked against that ground
# truth (so a dishonest or wrong report also fails).

reward=1

ALPHA_PATH=/app/probe/data/alpha.txt
BETA_PATH=/app/probe/data/nested/beta.txt
REPORT_PATH=/app/probe/report.txt

alpha_ground_truth=failed
if [ -f "$ALPHA_PATH" ] && [ "$(cat "$ALPHA_PATH")" = "alpha-9f3c2d" ]; then
  alpha_ground_truth=ok
fi

beta_ground_truth=failed
if [ -f "$BETA_PATH" ] && [ "$(cat "$BETA_PATH")" = "beta-77a1e4" ]; then
  beta_ground_truth=ok
fi

tcp_ground_truth=failed
if timeout 5 bash -c '</dev/tcp/example.com/80' 2>/tmp/probe-tcp.err; then
  tcp_ground_truth=ok
fi

dns_ground_truth=failed
if getent hosts github.com >/tmp/probe-dns.out 2>&1; then
  dns_ground_truth=ok
fi

echo "ground truth: alpha=$alpha_ground_truth beta=$beta_ground_truth tcp_example_com_80=$tcp_ground_truth dns_github_com=$dns_ground_truth"

if [ "$alpha_ground_truth" != "ok" ]; then
  echo "FAIL: $ALPHA_PATH missing or does not contain exactly 'alpha-9f3c2d'"
  reward=0
fi

if [ "$beta_ground_truth" != "ok" ]; then
  echo "FAIL: $BETA_PATH missing or does not contain exactly 'beta-77a1e4'"
  reward=0
fi

if [ ! -f "$REPORT_PATH" ]; then
  echo "FAIL: missing $REPORT_PATH"
  reward=0
else
  reported_alpha=$(grep -m1 '^alpha:' "$REPORT_PATH" | sed 's/^alpha:[[:space:]]*//')
  reported_beta=$(grep -m1 '^beta:' "$REPORT_PATH" | sed 's/^beta:[[:space:]]*//')
  reported_tcp=$(grep -m1 '^tcp_example_com_80:' "$REPORT_PATH" | sed 's/^tcp_example_com_80:[[:space:]]*//')
  reported_dns=$(grep -m1 '^dns_github_com:' "$REPORT_PATH" | sed 's/^dns_github_com:[[:space:]]*//')

  echo "reported: alpha=$reported_alpha beta=$reported_beta tcp_example_com_80=$reported_tcp dns_github_com=$reported_dns"

  if [ "$reported_alpha" != "$alpha_ground_truth" ]; then
    echo "FAIL: report.txt claims alpha=$reported_alpha but ground truth is $alpha_ground_truth"
    reward=0
  fi
  if [ "$reported_beta" != "$beta_ground_truth" ]; then
    echo "FAIL: report.txt claims beta=$reported_beta but ground truth is $beta_ground_truth"
    reward=0
  fi
  if [ "$reported_tcp" != "$tcp_ground_truth" ]; then
    echo "FAIL: report.txt claims tcp_example_com_80=$reported_tcp but ground truth is $tcp_ground_truth"
    reward=0
  fi
  if [ "$reported_dns" != "$dns_ground_truth" ]; then
    echo "FAIL: report.txt claims dns_github_com=$reported_dns but ground truth is $dns_ground_truth"
    reward=0
  fi
fi

echo "$reward" > /logs/verifier/reward.txt
exit 0
