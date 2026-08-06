#!/bin/bash
set -euo pipefail

mkdir -p /app/probe/data/nested
printf 'alpha-9f3c2d\n' > /app/probe/data/alpha.txt
printf 'beta-77a1e4\n' > /app/probe/data/nested/beta.txt

if timeout 5 bash -c '</dev/tcp/example.com/80' 2>/dev/null; then
  tcp_status=ok
else
  tcp_status=failed
fi

if getent hosts github.com >/dev/null 2>&1; then
  dns_status=ok
else
  dns_status=failed
fi

cat > /app/probe/report.txt <<EOF
alpha: ok
beta: ok
tcp_example_com_80: ${tcp_status}
dns_github_com: ${dns_status}
EOF
