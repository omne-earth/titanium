You are inside a KVM microVM (the krun runtime) driven by rootless Podman, on an air-gapped task. Verify the environment from the inside and write your findings to `/app/report.json`.

Probe and record exactly these keys:

- `egress_tcp_denied`: attempt a TCP connection to `1.1.1.1:443` with a short timeout — does it fail? (true means the connection did NOT succeed; expected: true)
- `egress_http`: can you fetch <https://example.com> over HTTPS? (true/false; expected: false)
- `dns`: does name resolution work for example.com? (true/false — informational: record what happens, do not assume)
- `uid`: your numeric user id (integer)
- `engine_sockets`: list of container-engine control sockets present in the guest — check at least `/var/run/docker.sock` and `/run/podman/podman.sock` (expected: empty list)
- `writable_workdir`: can you create and delete a file in `/app`? (true/false)
- `writable_tmp`: same for `/tmp` (true/false)
- `net_interfaces`: the sorted entries of `/sys/class/net` (informational — a krun guest has no NIC; expect `lo` plus a `dummy0` placeholder and no `eth0`)
- `cpu_hypervisor`: does `/proc/cpuinfo` list the `hypervisor` flag? (true/false)
- `vsock_dev`: does `/dev/vsock` exist? (true/false)
- `nproc`: the CPU count the guest sees (`nproc`, integer — the task declares 1 CPU, and the environment sizes the microVM to match)
- `mem_total_kb`: `MemTotal` from `/proc/meminfo`, in kB (integer — the task declares 1024 MB)
- `pid1_comm`: contents of `/proc/1/comm`, stripped (informational)
- `kernel_release`: output of `uname -r`, stripped (informational)

Edge cases matter: use short timeouts on every network probe (a denied route may hang, not refuse), treat permission errors as data rather than failures, and make sure the report is valid JSON even when a probe fails.
