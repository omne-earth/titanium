# The cella-runner inner probe, cella flavor

Read this first: this is the **inner task** for the `cella-runner`
reflexive runner with `cella` as the inner environment
(docs/runners/CELLA-RUNNER.md §5.2). It runs inside a sealed cella
micro-VM that itself runs inside a cella micro-VM. The runner bakes the
whole workspace into the outer VM and boots it; a systemd oneshot runs
`titanium run --env cella` against this task, one level down. An escape
from the inner VM lands in the outer guest, never on the host.

The smoke runs with the oracle agent, which applies the checked-in
`solution/solve.sh`. This file specifies the report that solution must
produce and the offline verifier pins.

The probe reads the VM boundary from the inside and writes
`/app/report.json` with exactly these keys:

- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd` — the inner
  guest is a whole VM with its own init, not a container)
- `cpu_hypervisor`: the `hypervisor` flag in `/proc/cpuinfo` (expected:
  true — a VM under KVM)
- `kvm_device`: `/dev/kvm` exists (expected: false — the inner guest
  hosts no guests; the depth stops here)
- `net_interfaces`: sorted `/sys/class/net` entries (expected: `lo` only —
  `allow_internet = false` is `--net none`)
- `egress_tcp_denied`: a TCP connect to `1.1.1.1:443` with a short timeout
  did NOT succeed (expected: true)
- `uid`: the numeric user id
- `kernel_release`: `uname -r`, stripped (informational)

Rules: short timeouts on the network probe, permission errors are data, and
the report is valid JSON even when a probe fails.
