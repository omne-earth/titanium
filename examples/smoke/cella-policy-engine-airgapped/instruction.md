# In-guest probe of the airgapped cella environment

Read this first: **no model runs this task.** On the cella rung an
agent would have to live inside the sealed guest, and this guest has no
network interface at all (`allow_internet = false` is the topology
`--net none`), so no inference API is reachable from where an agent
would stand. The smoke runs with the oracle agent, which applies the
checked-in `solution/solve.sh`. This file is not a briefing to a model;
it is the specification of the report that solution must produce and
the offline verifier pins.

The probe runs inside a cella micro-VM and writes its findings to
`/app/report.json`, with exactly these keys:

- `egress_tcp_denied`: a TCP connection attempt to `1.1.1.1:443` with a
  short timeout did NOT succeed (expected: true)
- `dns`: whether name resolution works for example.com (recorded, not
  asserted: with no nic there is nothing to resolve through, but how
  the resolver fails is data)
- `uid`: the numeric user id (expected: 0)
- `net_interfaces`: the sorted entries of `/sys/class/net` (expected:
  `lo` alone and no `eth0` — a `--net none` guest has no nic; this is a
  topology, not a firewall)
- `writable_workdir`: a file can be created and deleted in `/app`
  (expected: true)
- `writable_tmp`: same for `/tmp` (expected: true)
- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd` — the
  rootfs conversion provisions the distro's own systemd as PID 1)
- `cpu_hypervisor`: `/proc/cpuinfo` lists the `hypervisor` flag
  (expected: true — this is a KVM guest)
- `nproc`: the CPU count the guest sees (expected: 1 — cella machines
  run one vCPU)
- `mem_total_kb`: `MemTotal` from `/proc/meminfo`, in kB (the task
  declares 1024 MB)
- `kernel_release`: `uname -r`, stripped (informational)

Rules the solution follows, and any replacement must too: short
timeouts on every network probe (a missing route may hang, not refuse),
permission errors treated as data rather than failures, and the report
is valid JSON even when a probe fails.
