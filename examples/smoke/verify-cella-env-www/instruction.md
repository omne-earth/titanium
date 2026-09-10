# In-guest verification of the judged cella world nic

Read this first: **no model runs this task.** An agent would live
inside the guest, and this task's `cella.policy` grants exactly one
destination — no inference API is reachable. The smoke runs with the
oracle agent, which applies the checked-in `solution/solve.sh`. This
file specifies the report that solution must produce and the offline
verifier pins.

The machine has a world nic (`allow_internet = true`), the gateway is
open, and every crossing parks for titanium's policy engine enforcing
`environment/cella.policy` (arp both ways, `1.1.1.1:443/tcp` both
ways, nothing else).

The probe writes `/app/report.json` with exactly these keys:

- `granted_tcp_ok`: TCP to `1.1.1.1:443` succeeded (expected: true —
  retried for up to 60 s across the boot and the park-judge-release
  round trips)
- `denied_tcp_blocked`: TCP to `8.8.8.8:443` did NOT succeed
  (expected: true — refused in-frame, on the record)
- `net_interfaces`: sorted entries of `/sys/class/net` (expected:
  `eth0` beside `lo` — this guest HAS a network; it is judged)
- `kernel_ip_config`: `/proc/cmdline` carries cella's own
  `ip=192.168.210.2` autoconfiguration (expected: true — the address
  is the world plane's, stated by cella, not by the task)
- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd`)
- `uid`: numeric user id (expected: 0)
- `nproc`: CPU count (expected: 1)
- `mem_total_kb`: `MemTotal` in kB (the task declares 1024 MB)
- `kernel_release`: `uname -r`, stripped (informational)

Rules: bounded retries with short per-attempt timeouts on the granted
probe, a short timeout on the denied probe, permission errors are
data, and the report is valid JSON even when a probe fails.
