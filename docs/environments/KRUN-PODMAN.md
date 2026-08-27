# krun-Podman environment: protections, deltas, and the relaxation probe record

The selector is `--env krun-podman`. The class is
`titanium.environments.krun.podman.KrunPodmanEnvironment`. The script
`scripts/init/krun-podman.sh` provisions the runtime: it installs
`crun-krun` with dnf, applies the version floor from `runtime.env`,
writes a trust-on-first-use digest pin, registers the runtime in the
root-gated `containers.conf.d`, and checks `/dev/kvm`.

This flavor extends `GVisorPodmanEnvironment` and onboards **every**
gvisor-podman wiring: the staging channel, the network machinery, the
host-side verification gates, the fail-closed teardown, the rootless
ownership rules, and the runner separation. Thus
[GVISOR-PODMAN.md](GVISOR-PODMAN.md) applies here in full, and through it
[GVISOR.md](GVISOR.md) and [PODMAN.md](PODMAN.md). This document records
only what the runtime swap changes (§2). It ends with the probe record
(§5): measured facts about which inherited relaxations have a live cause
under krun, and the decision record built on those facts.

This is a running document. §4 and §5 update when new validation lands.

## 1. Baseline: a different boundary under the same driving

krun is crun built with the libkrun handler. Each container runs in a KVM
microVM with a real Linux guest kernel from `libkrunfw`. The isolation
boundary is hardware virtualization. The runsc boundary is a reimplemented
syscall surface. The trade is explicit:

* **Guest syscalls terminate in a full guest kernel**, not in Sentry's Go
  reimplementation. Syscall compatibility is broader. There is no Sentry
  syscall filter to hit.
* **The host-facing attack surface is the KVM ioctl interface plus
  libkrun's virtio device code.** That code runs in an unprivileged
  userspace VMM inside the rootless user namespace. An escape out of the
  guest lands in that process: an unprivileged user, and the throwaway
  `titanium` runner when the host is provisioned. It does not land as host
  root.
* **`/dev/kvm` enters the trust chain.** It is the one host device this
  flavor needs. The host kernel's KVM subsystem stands behind it.

Everything else in the posture is inherited unchanged: no engine socket
anywhere, rootless by default, runner separation for the podman family,
and host-side-only evidence. A guest `uname` that reports the libkrunfw
kernel is a useful smoke signal. It is never a gate. Verification reads
`{{.OCIRuntime}}` from the host, exactly as the runsc flavor does. It
accepts the `krun` name and a `.../krun` path.

## 2. What the runtime swap changes

### 2.1 Supply chain: dnf, a version floor, and a trust-on-first-use pin

The runsc flavors install a release-pinned, checksum-verified binary from
upstream. krun has no standalone upstream binary. The distro package
`crun-krun` is the only distribution channel. Three consequences follow:

* rpm signature verification stands in for the download checksum step.
* `runtime.env` carries a **floor** (`CRUN_KRUN_MIN_VERSION`), not an
  exact pin. Init can only demand a minimum, and it warns loudly on a
  version below the floor.
* The SHA3-512 digest pin (`/usr/local/share/titanium/krun.sha3-512`, knob
  `TITANIUM_KRUN_DIGEST_PIN`) is **always** trust-on-first-use: the init
  script did not download the binary it blesses. Init therefore demands a
  second, independent witness before it writes the pin: `rpm -Vf` must
  prove the on-disk binary still matches the signed package database, and
  a binary rpm does not own is refused outright. The pin records the
  package NVR and the verification time in a comment line. The pin is
  separate from the runsc pin. Each runtime is blessed, rotated, and
  verified on its own, and each failure text names its own init script.

A `dnf upgrade crun-krun` changes the binary and trips the pin at the
next preflight. That is by design. An upgrade must be blessed
deliberately: delete the pin, then re-run init. It is never absorbed
silently.

### 2.2 Host requirement: /dev/kvm, for the operator and the runner

Without `/dev/kvm` the runtime cannot start, so init fails before it
installs anything. When a host narrows the device below world-rw, init
grants the device group to the invoking user (a new login is required)
and to the `titanium` runner. The runner grant lives in two places on
purpose. `titanium.sh` covers fresh hosts: the runner does not exist yet
when `krun-podman.sh` runs. The branch in `krun-podman.sh` covers already
provisioned hosts that add krun on a later `make init`: there the
`.titanium` sentinel skips `titanium.sh`. Runner grants need no login.
Trials start through `systemd-run`, which reads group membership fresh.

### 2.3 No cgroup wrapper

The runsc flavor must register its runtime through the `-ignore-cgroups`
wrapper, because rootless runsc cannot drive the systemd cgroup path
(GVISOR-PODMAN.md §2.6). crun drives rootless cgroups through the user
manager natively. Thus krun registers bare, and there is no runtime-side
cgroup opt-out to reason about. Enforcement rides the same engine path in
both flavors, gated by the same post-start read-back (PODMAN.md §2.3).

### 2.4 The SELinux process label stays on

Podman labels every container process on an enforcing host. runsc rejects
a labeled spec, so the runsc flavor must send `label=disable`, and its
`main` runs unconfined (GVISOR-PODMAN.md §2.2). crun supports SELinux, so
this flavor keeps the label. `main` runs as `container_kvm_t`, the
confined domain podman assigns to KVM runtimes, with its MCS pair (probe
row 5). This is the one axis where krun-podman is tighter than
gvisor-podman. The posture is runtime-forced in both directions: neither
flavor chose it. The staging-mount `z` relabel and its blast radius are
unchanged from the parent.

### 2.5 The engine guard refuses instead of redirecting

`engine=docker` on the runsc flavor redirects to `--env gvisor`. Here it
fails with a krun-specific message. No docker flavor of the krun sandbox
exists, and a redirect to gVisor would point at a different sandbox
technology.

### 2.6 Exec `-T`: demoted from correctness to fidelity

The `-T` injection on programmatic execs is inherited. Under runsc it is
a correctness requirement: runsc's exec has no `-tty` flag (see
GVISOR-PODMAN.md §2.1). Under krun it is the same fidelity fix it is for
plain podman. crun tolerates the pty, and `-T` keeps transcript bytes
pipe-clean.

### 2.7 A tightened seccomp profile on the VMM

The VMM process is the host-facing attack surface (§1). The probe record
shows podman's default seccomp filter already applies to it, and this
flavor tightens it: the compose override applies
`src/titanium/environments/krun/seccomp.json`, the default profile minus
unconditional allowances a VMM never needs after crun's setup — `ptrace`,
`process_vm_readv`/`writev`, `keyctl`, `memfd_secret`, `mount`,
`umount`/`umount2`, `pivot_root`, `unshare`, `setns`. The battery
validates boot, virtiofs, cp, TSI egress, and AF_VSOCK under it, and the
live syscall capture (steady state: `epoll_wait`, `ioctl`, `read`,
`write`) stays allowed with a wide margin. The runsc flavors are
untouched: the seam defaults to no extra `security_opt`. What no profile
can shrink: the guest attacks KVM through the virtualization interface,
not through the VMM's syscalls — that surface is the price of the
boundary type.

### 2.8 Guest sizing is explicit

Without direction, the handler sizes the guest from the host: vCPUs from
the process's affinity mask (capped at 16) and RAM from the OCI memory
limit, falling back to 1024 MiB. Both are implicit dependencies. This
flavor sizes the guest straight through the handler's highest-precedence
surface instead: the compose override emits `krun.cpus` and
`krun.ram_mib` annotations from the task's resolved `cpus` and
`memory_mb`. Thus the guest sees exactly the cores the task declares —
without `krun.cpus`, thread pools sized by core count oversubscribe
against the cgroup quota — and the guest RAM no longer rides the OCI
limit as a side effect. Both annotations are battery-proven (the
annotation probe measures `nproc` and `MemTotal`), and both verify
examples assert the sizing from inside. Tasks that declare no resources
emit no annotations and fall to the handler defaults. One shared
envelope remains: the guest RAM, the VMM overhead, and the virtiofs DAX
window all live inside the same cgroup `memory.max`.

## 3. Inherited and functional limitations

Everything in GVISOR-PODMAN.md §3 and PODMAN.md §3 applies, and GVISOR.md
§3 applies where it concerns the shared machinery, not Sentry.
krun-specific limits:

* KVM is required: bare metal or nested virtualization. No `/dev/kvm`
  means no trials. Init and preflight both fail closed.
* Networking rides TSI (transparent socket impersonation). The guest has
  no NIC at all, only `lo` plus a `dummy0` placeholder. Every socket a
  guest process opens is impersonated host-side, and it is invisible to
  the guest's own netstat (probe record, §5). Three measured
  consequences: outbound TCP and external DNS work; container-name DNS
  does not work (TSI resolves host-side and bypasses aardvark); inbound
  TCP does not work at all. A guest listener is connection-refused from
  peers and from the guest's own loopback. Tasks whose workload serves a
  port do not work under this flavor. This limit is currently
  unexercised: zero of the 206 onboarded tasks (deep-swe 117,
  terminal-bench-2 89) declare compose `ports:`, a Dockerfile `EXPOSE`,
  or a healthcheck. Raw sockets and `ping` are also outside TSI.
* Engine verbs that reach *into* a running guest do not work. Exec is one
  (§5 row 0). Healthchecks are the other: the engine runs the probe
  command inside the container, so a healthcheck on a krun service
  silently never executes. Only the egress proxy uses one today, and it
  runs under crun.
* Signals stop at the VMM. `podman stop` sends SIGTERM to the VMM
  process, and the signal never reaches the workload. Every stop is hard,
  and this flavor says so up front: the compose override declares
  `stop_signal: SIGKILL` on `main`, so teardown skips the dead grace
  period instead of waiting ten seconds for a SIGTERM the guest cannot
  see (observed live, 2026-08-26). Teardown already force-removes, so
  nothing in the lifecycle depends on a shutdown window.
* Rootfs I/O crosses virtiofs, and each container carries a VM's memory
  footprint. Syscall-heavy workloads trade gVisor's syscall tax for
  virtualization and virtiofs overhead.
* The installed `libkrunfw` fixes the guest kernel. Workloads that need
  specific kernel modules or versions are out of scope.

## 4. Validation posture (running)

2026-08-26, Fedora 44 host (podman 5.8.4, crun-krun 1.28, libkrun 1.19.0,
libkrunfw 5.5.0, SELinux Enforcing): the full init chain is proven. That
covers the dnf install, the digest pin, the drop-in registration, the
image-free `podman create --runtime krun` resolution probe, an idempotent
re-run, and the Python preflight (podman + resolution + digest pin). Unit
suites are green (`make unit-all`, 366 tests total at the time). On the
same day and host, the full validation cycle `make reset` → `make init`
ran with krun in both directions: deprovision removes the drop-in and the
pin, the clean-slate assertion checks them, and re-init restores them.
`make _probe-krun-podman` ran identically before and after the cycle;
the results are in §5. The battery was then extended twice on the same
host: the vsock probes (guest stack, handler surface) and the bind-mount
coherence probe that qualifies the mailbox channel. Not yet proven: no
live trial has run under krun. The mailbox exec override is implemented and
unit-tested; the first live trial is the smoke goal.

## 5. Probe record: which inherited relaxations this flavor keeps

Every gVisor-lineage relaxation exists because of a runsc property. For
each one the question is: does the property exist under krun? Where it
does not, the relaxation's *cause* is gone, and this flavor faces a
decision: onboard the gvisor-podman workaround (one wiring for the
sandbox lineage) or the plain-podman native path. Both already exist and
are tested; nothing here proposes new machinery. Where the property is
engine-forced, krun changes nothing and the row is closed.

`make _probe-krun-podman` (internal target,
`scripts/doctor/probe-krun-podman.sh`) collected the evidence on this
host, 2026-08-26 (crun-krun 1.28, libkrun 1.19.0, libkrunfw 5.5.0,
podman 5.8.4, SELinux Enforcing). It ran twice, identically, before and
after a full `make reset` → `make init` cycle. The "Reconciled" column
states whether the row is settled: the facts are measured, the decision
is recorded, and nothing about the row waits on unfinished work. The
chosen row-0 path is the mailbox override (closing record, below). The
SSH channel stays documented as its upgrade.

| # | Relaxation | Forced by | Result (measured) | Reconciled |
|---|------------|-----------|-------------------|------------|
| 0 | Precondition, not a relaxation: the whole wiring rides compose exec (agent commands, in-sandbox probes, transfer pipeline) | — | **The handler does not implement exec.** But the trial uses exec as a *serial chain*: install, setup, agent config plus one long launch, pre-artifacts, verifier. It is never concurrent and never mid-run interactive (audited: `trial.py`, `installed/base.py`, per-agent run paths). The mailbox channel that can carry that chain is measured: bind-mount writes are visible host→guest and guest→host within ~1s while the guest runs. Resolved: the mailbox `exec()` override is implemented in the subclass (closing record) | **Yes — live-proven.** `make smoke-krun-podman` (2026-08-26): three trials, zero errors, clean teardown; the mailbox carried agent install, the full agent run, transfers, and the verifier |
| 1 | GVISOR §2.1 — staging binds puncture the rootfs | runsc rootfs is sandbox-private; `cp` unusable | Cause absent: `podman cp` is coherent in both directions against a *running* guest. Uploads land root-owned inside. Root-written and uid-1000-written exports arrive invoker-owned. Directories export intact | Yes — wiring inherited, live-proven with row 0 |
| 2 | GVISOR §2.4 — transfers execute as root inside the sandbox | Same cause as row 1 | Cause absent: host-side `cp` needs no guest cooperation | Yes — with row 1 |
| 3 | GVISOR §2.2 — resolv.conf repair, host resolvers in `dns:` | netstack cannot reach DNAT-to-loopback engine DNS | Cause replaced, not absent: TSI resolves host-side and bypasses aardvark names (a peer name fails, an external name resolves). Thus the lineage wiring — proxy by literal IP, sandbox never needing DNS — is required under krun for a new reason. Decision (operator, 2026-08-26): keep the lineage wiring | Yes — decision recorded, wiring kept, nothing pending |
| 4 | GVISOR §2.3 — egress proxy off the sandbox runtime | The proxy needs engine DNS, which runsc breaks | Enabler dead: inbound TCP to a krun guest fails in every shape. Connect is refused (httpd), data is never delivered (nc), and the guest's own loopback is refused. A krun guest cannot host a TCP service. The proxy stays crun, and port-serving workloads are out of scope (§3) | Yes — proxy stays crun; the limit is documented in §3 and no onboarded task hits it |
| 5 | GVISOR-PODMAN §2.2 — `label=disable` on `main` | runsc rejects a labeled spec | Confirmed removed: `main` runs confined as `container_kvm_t` with an MCS pair under Enforcing | Yes — shipped and confirmed |
| 6 | GVISOR-PODMAN §2.2 — staging `z` relabel | Staging existing + podman not relabeling binds | Follows row 1: returns with staging, as inherited. The relabel is measured working, and the first live run found and fixed a latent lineage bug here: staged uploads carried their source's SELinux label (`copytree` copies xattrs), which the runsc flavors' `label=disable` masked and krun's kept label exposed. The staging ops now re-align staged trees to the staging mount's context | Yes — with row 1 |
| 7 | GVISOR-PODMAN §2.4 — exports chowned to `0:0` | Staging ops under the rootless mapping | Follows row 1: returns with staging, as inherited | Yes — with row 1 |
| 8 | GVISOR-PODMAN §2.6 — `-ignore-cgroups` wrapper | Rootless runsc cannot drive systemd cgroups | Confirmed absent: declared `cpu.max`/`memory.max` read back enforced, with no wrapper | Yes — shipped and confirmed |
| 9 | PODMAN §2.2 — task-mount `z` relabel | Podman engine property | Present — engine, not runtime. Keep | Yes — engine-forced keep |
| 10 | PODMAN §2.3 — rootless limits gap | Host/engine property | Present. Keep (row 8's read-back is the backstop) | Yes — engine-forced keep |
| 11 | GVISOR-PODMAN §2.3 — path-based runtime trust | Podman has no daemon registry | Present. Keep. The rpm witness hardens the TOFU pin: init refuses to bless a binary that fails `rpm -Vf` or that rpm does not own, and the pin records the package NVR in a comment line | Yes — the rpm witness gates the pin at init and the pin records the package NVR |

**Row 0's chosen path: the mailbox exec override.** Every channel the
mailbox needs is measured, and every seam it needs already exists.
`main`'s command is titanium-owned: `build.yaml` and `prebuilt.yaml` set
`sleep infinity`, so the task image's CMD is already displaced. This
flavor's compose override is appended last, where the `command` key wins.
It swaps the keepalive for a runner loop that watches a mailbox directory
on the staging mounts the flavor already inherits.
`KrunPodmanEnvironment` overrides `exec()`. The override writes a command
file (script, env, cwd, user, timeout) and returns the runner's exit,
stdout, and stderr files as the `ExecResult`. Everything above the seam —
trial, agents, oracle, verifier, the serial exec chain — runs unmodified.
Every other environment is untouched. Timeouts run guest-side (`timeout`
in the runner) with engine `stop` as the backstop. The honest costs:
cancellation is cooperative, each command pays ~1s of poll latency
(measured), and interactive `attach` stays unsupported. Trust is
unchanged: the guest cooperates or lies, exactly as with exec. Nothing
evidentiary rides the channel, and every verification gate stays
host-side. Status: live-proven. `make smoke-krun-podman` on 2026-08-26
ran three trials with zero errors and a clean teardown: the mailbox
carried the agent install, the full agent run, the staging transfers,
and the verifier. `verify-krun-podman-env` (now `-www`) scored 1.0 asserting the
microVM signatures from inside; `fix-git-offline` scored 1.0 through the
egress proxy; `build-pmars` scored 0.0 on a model instruction-following
miss with a failure signature identical to its gvisor-podman runs — the
environment is not implicated.

Why this is enough is worth recording. Exec is Harbor's portability
contract, inherited through two forks (titanium ← pier ← Harbor). It is
not a titanium requirement. The audit shows trials never use more than
serial command execution, and a file protocol carries that.

**The SSH-over-vsock upgrade (kept, not needed).** The guest's vsock
stack is measured viable: `/dev/vsock` exists and AF_VSOCK binds. The
handler parses `/.krun_vm.json` (`ram_mib` and `cpus` measured; the
higher-precedence `krun.*` annotations were read from crun source,
krun.c). But crun-krun 1.28 exposes no vsock mapping surface, so this
path waits on an upstream change; the operator owns crun. The shape,
should attach, streamed output, or hard kill ever matter: the handler
maps a guest vsock port to a host unix socket (`krun_add_vsock_port2`);
the guest runs `socat VSOCK-LISTEN:<port>,fork EXEC:"sshd -i"` — inetd
mode, because the guest's TCP loopback is dead (row 4), so no bridge to
a TCP sshd can work; the host connects with `ProxyCommand` through the
trial's unix socket, with a per-trial keypair and a pinned host key. No
onboarded task needs what this adds (§3's audit). That is why the
mailbox goes first.
