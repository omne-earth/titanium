# Network egress probe report

Write `/app/report.json` with exactly these keys, each from what
the machine shows:

- `granted_https_ok`: whether three sequential HTTPS GETs to the
  granted name `example.com` all succeeded
- `calls`: one entry per call, `{measured_at, secs}` — the epoch
  seconds each started at and the seconds it took, both as numbers
- `denied_https_blocked`: whether an HTTPS GET to the ungranted
  name `example.org` failed to complete
- `resolver_is_gateway`: whether the granted and the ungranted name
  both resolve to one identical address, and `resolver_address`:
  that address, as resolved
- `pid1_comm`: the contents of `/proc/1/comm`, stripped
- `uid`: the numeric user id the probe ran as
- `nproc`: the CPU count the guest observes
- `mem_total_kb`: `MemTotal` in kB, as the guest reports it
- `kernel_release`: `uname -r`, stripped

Use short timeouts on network probes. The report is valid JSON even
when a probe fails.
