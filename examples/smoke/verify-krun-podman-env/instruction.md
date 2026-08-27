You are inside a KVM microVM (the krun runtime) driven by rootless Podman. Verify the environment from the inside and write your findings to `/app/report.json`.

Probe and record exactly these keys:

- `egress_http`: can you fetch <https://example.com> over HTTPS? (true/false)
- `dns`: does name resolution work for example.com? (true/false — distinguish this from TCP failure)
- `uid`: your numeric user id (integer)
- `engine_sockets`: list of container-engine control sockets present in the guest — check at least `/var/run/docker.sock` and `/run/podman/podman.sock` (expected: empty list)
- `writable_workdir`: can you create and delete a file in `/app`? (true/false)
- `writable_tmp`: same for `/tmp` (true/false)
- `net_interfaces`: the sorted entries of `/sys/class/net` (informational — a krun guest has no NIC; expect `lo` plus a `dummy0` placeholder and no `eth0`)
- `cpu_hypervisor`: does `/proc/cpuinfo` list the `hypervisor` flag? (true/false)
- `vsock_dev`: does `/dev/vsock` exist? (true/false)
- `sysrq_writable`: does writing "h" to `/proc/sysrq-trigger` succeed? Actually attempt it — flush/close the file so any error surfaces — and record the outcome (informational: this is a real guest kernel, so the write may succeed, and it is contained by the VM either way)
- `pid1_comm`: contents of `/proc/1/comm`, stripped (informational)
- `kernel_release`: output of `uname -r`, stripped (informational)

Edge cases matter: use timeouts on network probes, treat permission errors as data rather than failures, and make sure the report is valid JSON even when a probe fails.
