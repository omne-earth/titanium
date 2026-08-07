You are inside a podman sandbox. Verify the environment from the inside and write your findings to `/app/report.json`.

Probe and record exactly these keys:

- `egress_http`: can you fetch <https://example.com> over HTTPS? (true/false)
- `dns`: does name resolution work for example.com? (true/false — distinguish this from TCP failure)
- `uid`: your numeric user id (integer)
- `engine_sockets`: list of container-engine control sockets present in the sandbox — check at least `/var/run/docker.sock` and `/run/podman/podman.sock` (expected: empty list)
- `writable_workdir`: can you create and delete a file in `/app`? (true/false)
- `writable_tmp`: same for `/tmp` (true/false)
- `sysrq_writable`: does writing "h" to `/proc/sysrq-trigger` succeed? Actually attempt it (true/false; a permission error means the boundary holds — record false, don't crash)
- `pid1_comm`: contents of `/proc/1/comm`, stripped (informational)

Edge cases matter: use timeouts on network probes, treat permission errors as data rather than failures, and make sure the report is valid JSON even when a probe fails.
