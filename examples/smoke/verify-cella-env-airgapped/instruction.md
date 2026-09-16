# In-guest verification of the airgapped cella environment

Read this first: **no model runs this task.** On the cella rung an
agent lives inside the sealed guest, and this guest has no network
interface at all (`allow_internet = false` is the topology
`--net none`), so no inference API is reachable. The smoke runs with
the oracle agent, which applies the checked-in `solution/solve.sh`.
This file specifies the report that solution must produce and the
offline verifier pins.

The probe writes `/app/report.json` with exactly these keys:

- `egress_tcp_denied`: TCP to `1.1.1.1:443` (short timeout) did NOT
  succeed (expected: true)
- `net_interfaces`: sorted entries of `/sys/class/net` (expected: `lo`
  alone — a topology, not a firewall)
- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd`, put
  there by the rootfs conversion's provisioning)
- `uid`: numeric user id (expected: 0)
- `writable_workdir` / `writable_tmp`: create-and-delete works in
  `/app` and `/tmp` (expected: true)
- `cpu_hypervisor`: `/proc/cpuinfo` lists `hypervisor` (expected:
  true — a KVM guest)
- `nproc`: CPU count (expected: 1 — cella machines run one vCPU)
- `mem_total_kb`: `MemTotal` in kB (the task declares 1024 MB)
- `fs_total_kb`: the root filesystem's size in kB via statvfs (the
  task declares 4096 MB; the ext4 is built at that capacity)
- `root_device_is_vda`: `/proc/cmdline` names `root=/dev/vda`
  (expected: true — cella boots a kernel and one virtio disk)
- `kernel_release`: `uname -r`, stripped (informational)

Rules: short timeouts on network probes, permission errors are data,
and the report is valid JSON even when a probe fails.
