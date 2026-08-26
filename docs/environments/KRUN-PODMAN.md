# krun-Podman environment: protections, deltas, and the relaxation probe record

Selector `--env krun-podman`, class
`titanium.environments.krun.podman.KrunPodmanEnvironment`, provisioned by
`scripts/init/krun-podman.sh` (dnf-installed `crun-krun`, version floor in
`runtime.env`, trust-on-first-use digest pin, root-gated `containers.conf.d`
registration, `/dev/kvm` checks). This flavor extends
`GVisorPodmanEnvironment` and onboards **every** gvisor-podman wiring: the
staging channel, the network machinery, the host-side verification gates,
the fail-closed teardown, the rootless ownership rules, and the runner
separation. So [GVISOR-PODMAN.md](GVISOR-PODMAN.md) — and through it
[GVISOR.md](GVISOR.md) and [PODMAN.md](PODMAN.md) — applies here in full.
This document records only what the runtime swap changes (§2), and it ends
with a running probe record (§5) that will decide, on evidence, which
inherited relaxations this flavor keeps.

This is a running document. §4 and §5 update as validation lands; §5's
pending rows convert to decision records when `make _probe-krun-podman`
has run.

## 1. Baseline: a different boundary under the same driving

krun is crun built with the libkrun handler. Each container runs in a KVM
microVM with a real Linux guest kernel (from `libkrunfw`). The isolation
boundary is hardware virtualization, where runsc's is a reimplemented
syscall surface. The trade is explicit:

* **Guest syscalls terminate in a full guest kernel**, not in Sentry's Go
  reimplementation. Syscall compatibility is broader; there is no Sentry
  syscall filter to hit.
* **The host-facing attack surface is the KVM ioctl interface plus
  libkrun's virtio device implementations**, running in an unprivileged
  userspace VMM inside the rootless user namespace. An escape out of the
  guest lands in that process — an unprivileged user (and, when provisioned,
  the throwaway `titanium` runner), not host root.
* **`/dev/kvm` enters the trust chain.** It is the one host device this
  flavor needs; the host kernel's KVM subsystem stands behind it.

Everything else in the posture is inherited unchanged: no engine socket
anywhere, rootless by default, runner separation for the podman family,
and host-side-only evidence — a guest `uname` reporting the libkrunfw
kernel is a useful smoke signal and is never a gate. Verification reads
`{{.OCIRuntime}}` from the host, exactly as the runsc flavor does, and
accepts the `krun` name or a `.../krun` path.

## 2. What the runtime swap changes

### 2.1 Supply chain: dnf, a version floor, and a trust-on-first-use pin

The runsc flavors install a release-pinned, checksum-verified binary from
upstream. krun has no standalone upstream binary: the distro package
(`crun-krun`) is the only distribution channel. Three consequences:

* rpm signature verification stands in for the download checksum step.
* `runtime.env` carries a **floor** (`CRUN_KRUN_MIN_VERSION`), not an exact
  pin — init can demand a minimum, and warns loudly on a version below it.
* The SHA3-512 digest pin (`/usr/local/share/titanium/krun.sha3-512`, knob
  `TITANIUM_KRUN_DIGEST_PIN`) is **always** trust-on-first-use: this script
  did not download the binary it blesses. The pin is separate from the
  runsc pin — each runtime is blessed, rotated, and verified on its own,
  and each failure text names its own init script.

A `dnf upgrade crun-krun` changes the binary and trips the pin at the next
preflight. That is by design: an upgrade must be blessed deliberately
(delete the pin, re-run init), never absorbed silently.

### 2.2 Host requirement: /dev/kvm, for the operator and the runner

Without `/dev/kvm` the runtime cannot start, so init fails before it
installs anything. When a host narrows the device below world-rw, init
grants the device group to the invoking user (new login required) and to
the `titanium` runner. The runner grant lives in two places on purpose:
`titanium.sh` covers fresh hosts, where the runner does not exist yet when
`krun-podman.sh` runs; the branch in `krun-podman.sh` covers already
provisioned hosts that add krun on a later `make init`, where the
`.titanium` sentinel skips `titanium.sh`. Runner grants need no login:
trials start through `systemd-run`, which reads group membership fresh.

### 2.3 No cgroup wrapper

The runsc flavor must register its runtime through the `-ignore-cgroups`
wrapper because rootless runsc cannot drive the systemd cgroup path
(GVISOR-PODMAN.md §2.6). crun drives rootless cgroups through the user
manager natively, so krun registers bare, and there is no runtime-side
cgroup opt-out to reason about. Enforcement rides the same engine path in
both flavors, gated by the same post-start read-back (PODMAN.md §2.3).

### 2.4 The SELinux process label stays on

Podman labels every container process on an enforcing host. runsc rejects
a labeled spec, so the runsc flavor must send `label=disable` and its
`main` runs unconfined (GVISOR-PODMAN.md §2.2). crun supports SELinux, so
this flavor keeps the label: `main` runs as `container_t` with its MCS
pair. This is the one axis where krun-podman is tighter than
gvisor-podman, and it is runtime-forced in both directions — neither
flavor chose its posture. The staging-mount `z` relabel and its blast
radius are unchanged from the parent.

### 2.5 The engine guard refuses instead of redirecting

`engine=docker` on the runsc flavor redirects to `--env gvisor`. Here it
fails with a krun-specific message: no docker flavor of the krun sandbox
exists, and a redirect to gVisor would point at a different sandbox
technology.

### 2.6 Exec `-T`: demoted from correctness to fidelity

The `-T` injection on programmatic execs is inherited. Under runsc it is a
correctness requirement (runsc's exec has no `-tty` flag; see
GVISOR-PODMAN.md §2.1). Under krun it is the same fidelity fix it is for
plain podman: crun tolerates the pty, and `-T` keeps transcript bytes
pipe-clean.

## 3. Inherited and functional limitations

Everything in GVISOR-PODMAN.md §3, PODMAN.md §3, and — where it concerns
the shared machinery, not Sentry — GVISOR.md §3 applies. krun-specific:

* KVM is required: bare metal or nested virtualization. No `/dev/kvm`, no
  trials — init and preflight both fail closed.
* Networking rides TSI (transparent socket impersonation): guest sockets
  are impersonated as host-side sockets in the container netns. TSI is
  socket-level; raw sockets and `ping` semantics inside the guest differ
  from a plain container.
* Rootfs I/O crosses virtiofs, and each container carries a VM's memory
  footprint; syscall-heavy workloads trade gVisor's syscall tax for
  virtualization and virtiofs overhead.
* The guest kernel is fixed by the installed `libkrunfw`; workloads that
  need specific kernel modules or versions are out of scope.

## 4. Validation posture (running)

2026-08-26, Fedora 44 host (podman 5.8.4, crun-krun 1.28, libkrun 1.19.0,
libkrunfw 5.5.0, SELinux Enforcing): full init chain proven —
dnf install, digest pin written, drop-in registration, image-free
`podman create --runtime krun` resolution probe, idempotent re-run, and
the Python preflight (podman + resolution + digest pin) green. Unit
suites green (`make unit-all`, 366 tests total at the time). Not yet
proven: no live trial has run under krun, and §5's probes have not run.
This section updates as each lands.

## 5. Probe record: which inherited relaxations this flavor keeps

Every gVisor-lineage relaxation exists because of a runsc property. For
each one the question is: does the property exist under krun? Where it
does not, the relaxation's *cause* is gone, and this flavor faces a
decision — onboard the gvisor-podman workaround anyway (one wiring for
the sandbox lineage) or the plain-podman native path (both already exist
and are tested; nothing here proposes new machinery). Where the property
is engine-forced, krun changes nothing and the row is closed.

`make _probe-krun-podman` (internal target,
`scripts/doctor/probe-krun-podman.sh`) collects the evidence on a live
host. When it has run, each pending row converts to a decision record:
the probe result, the decision (keep or switch), and the reason.

| # | Relaxation | Forced by | Expected under krun | Probe | Result |
|---|------------|-----------|---------------------|-------|--------|
| 0 | Not a relaxation — a precondition: the whole wiring rides compose exec (transfers, in-sandbox probes, agent commands) | — | Unknown: the libkrun handler may not implement exec | `podman exec` against a running krun container | pending |
| 1 | GVISOR §2.1 — staging binds puncture the rootfs | runsc rootfs is sandbox-private; `cp` unusable | Cause absent: rootfs is virtiofs, host-coherent | `podman cp` both directions against a running krun container | pending |
| 2 | GVISOR §2.4 — transfers execute as root inside the sandbox | Same cause as row 1 | Same as row 1 | Same probe as row 1 | follows row 1 |
| 3 | GVISOR §2.2 — resolv.conf repair, host resolvers in `dns:` | netstack cannot reach DNAT-to-loopback engine DNS | Cause absent: TSI sockets live in the container netns | Peer-name and external resolution from inside krun | pending |
| 4 | GVISOR §2.3 — egress proxy off the sandbox runtime | The proxy needs engine DNS, which runsc breaks | Cause absent (same as row 3) | DNS probe + a krun listener reachable from a crun peer | pending |
| 5 | GVISOR-PODMAN §2.2 — `label=disable` on `main` | runsc rejects a labeled spec | Cause absent: crun supports SELinux (already removed in code) | Process label of running `main` under Enforcing | pending |
| 6 | GVISOR-PODMAN §2.2 — staging `z` relabel | Staging existing + podman not relabeling binds | Present while staging stays | None — moot if row 1 switches | follows row 1 |
| 7 | GVISOR-PODMAN §2.4 — exports chowned to `0:0` | Staging ops under the rootless mapping | Tied to staging | None — moot if row 1 switches | follows row 1 |
| 8 | GVISOR-PODMAN §2.6 — `-ignore-cgroups` wrapper | Rootless runsc cannot drive systemd cgroups | Cause absent: crun is native (already absent in init) | `cpu.max`/`memory.max` read back from a limited krun container | pending |
| 9 | PODMAN §2.2 — task-mount `z` relabel | Podman engine property | **Present** — engine, not runtime | none needed | closed: keep |
| 10 | PODMAN §2.3 — rootless limits gap | Host/engine property | **Present** | covered by row 8's read-back | closed: keep |
| 11 | GVISOR-PODMAN §2.3 — path-based runtime trust | Podman has no daemon registry | **Present** | none needed (rpm witness hardens the TOFU pin at init) | closed: keep |

Rows 5 and 8 validate behavior this flavor already ships; their probes
turn claims into recorded facts. Rows 1–4 are the open decisions. Row 4's
decision (proxy under krun) is a separate feature in any case — the probe
here records only whether the enabler holds.
