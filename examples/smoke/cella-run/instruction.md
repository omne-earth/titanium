# The cella-run inner probe

Read this first: this is the **inner task** for the `cella-run` reflexive
runner (docs/runners/CELLA-RUN.md). It runs inside a docker container that
itself runs inside a sealed cella micro-VM. The runner bakes the whole
workspace into that VM and boots it; a systemd oneshot runs `titanium run
--env docker` against this task. A docker escape from here lands in the cella
guest, never on the host.

The smoke runs with the oracle agent, which applies the checked-in
`solution/solve.sh`. This file specifies the report that solution must produce
and the offline verifier pins.

The probe reads the container boundary from the inside and writes
`/app/report.json` with exactly these keys:

- `in_container`: the workload is containerized — `/.dockerenv` exists, or
  `/proc/1/cgroup` names docker/containerd (expected: true)
- `pid1_comm`: `/proc/1/comm`, stripped — the container entrypoint, not the
  guest's `systemd` (expected: not `systemd`)
- `host_root_reachable`: the host filesystem is bind-mounted in at `/host` or
  `/hostfs` (expected: false — the boundary held)
- `uid`: the numeric user id
- `net_interfaces`: sorted `/sys/class/net` entries
- `egress_tcp_denied`: a TCP connect to `1.1.1.1:443` with a short timeout did
  NOT succeed (expected: true — `allow_internet = false`, no route out)
- `kernel_release`: `uname -r`, stripped (informational)

Rules: short timeouts on the network probe, permission errors are data, and
the report is valid JSON even when a probe fails.
