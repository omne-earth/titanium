# In-guest verification of the terminated pair

Read this first: **no model runs this task.** The smoke runs with the
oracle agent, which applies the checked-in `solution/solve.sh`. This
file specifies the report that solution must produce and the offline
verifier pins.

`allow_internet = true`, so titanium stands the terminated pair: this
machine is a **member** with no world nic of its own, wired to a
terminator appliance that holds the world. The member reaches the
world only by **name** — the appliance is the resolver (it answers
every name with its own address), reads the SNI, terminates TLS on a
leaf minted from the pair CA (which titanium folded into this image's
trust bundle), and connects the world leg itself. titanium's engine
judges that world leg by the resolved name against
`environment/cella.policy`, which grants `example.com` and nothing
else.

The probe writes `/app/report.json` with exactly these keys:

- `granted_https_ok`: three sequential HTTPS GETs to the granted name
  `example.com` all succeeded (expected: true — the minted leaf
  verified against the pair CA; the first is retried for up to 240 s
  across the boot and the first-crossing freezes)
- `calls`: one entry per call, `{measured_at, secs}` — the guest wall
  clock it started at and the guest-perceived seconds it took, cold
  then two warm (verified: the standing memory makes the second and
  third run live, so neither is slower than the cold first). Both are
  cryogenic; paired with the audit book's `host_ns` they show the
  frozen time the guest slept through
- `denied_https_blocked`: an HTTPS GET to the ungranted name
  `example.org` did NOT succeed (expected: true — its world leg is
  refused on the appliance, on the record)
- `resolver_is_appliance`: `example.com` resolves to the appliance
  `10.77.0.1` (expected: true — the resolver is the interceptor)
- `pid1_comm`: `/proc/1/comm`, stripped (expected: `systemd`)
- `uid`: numeric user id (expected: 0)
- `nproc`: CPU count (expected: 1)
- `mem_total_kb`: `MemTotal` in kB (the task declares 1024 MB)
- `kernel_release`: `uname -r`, stripped (informational)

Rules: the probe must speak TLS (a bare TCP connect carries no name for
the appliance to route); bounded retries with short per-attempt
timeouts on the granted probe, a short timeout on the denied probe,
permission errors are data, and the report is valid JSON even when a
probe fails.
