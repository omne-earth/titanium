# In-guest probe of the judged cella world nic

Read this first: **no model runs this task.** An agent would have to
live inside the guest, and this task's `cella.policy` grants exactly
one destination — no inference API is reachable. The smoke runs with
the oracle agent, which applies the checked-in `solution/solve.sh`.
This file specifies the report that solution must produce and the
offline verifier pins.

The machine has a world nic (`allow_internet = true`), the gateway is
open, and every crossing parks for titanium's policy engine, which
enforces `environment/cella.policy`:

    allow outgoing arp
    allow incoming arp
    allow outgoing 1.1.1.1:443/tcp
    allow incoming 1.1.1.1:443/tcp

The probe writes `/app/report.json` with exactly these keys:

- `granted_tcp_ok`: a TCP connection to `1.1.1.1:443` succeeded
  (expected: true — the grant releases it; retried for up to 60 s,
  because the address arrives via systemd-networkd and each new flow
  waits one park-judge-release round trip)
- `denied_tcp_blocked`: a TCP connection to `8.8.8.8:443` did NOT
  succeed (expected: true — no grant names it, so the engine refuses
  it in-frame)
- `dns`: whether name resolution works for example.com (recorded, not
  asserted: no resolver is granted, and how it fails is data)
- `uid`: the numeric user id (expected: 0)
- `net_interfaces`: the sorted entries of `/sys/class/net` (expected:
  a real ethernet interface beside `lo` — this guest HAS a network;
  it is judged, not absent)
- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd`)
- `nproc`: the CPU count (expected: 1)
- `kernel_release`: `uname -r`, stripped (informational)

Rules the solution follows, and any replacement must too: bounded
retries with short per-attempt timeouts on the granted probe, a short
timeout on the denied probe, permission errors treated as data, and
the report is valid JSON even when a probe fails.

To regenerate the policy from observation instead of writing it by
hand: `make smoke-cella-policy-engine-www DRY_RUN=true` runs the same
trial with the engine in collection mode — every crossing releases and
lands in `cella.policy` as a grant — then copies the collected file
back here for review.
