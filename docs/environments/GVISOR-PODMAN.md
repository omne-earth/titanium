# gVisor-Podman environment: protections, relaxations, and hardening avenues

Selector `--env gvisor-podman`, class
`pier.environments.gvisor.podman.GVisorPodmanEnvironment`, provisioned by
`scripts/init/runsc-podman.sh` (checksum-verified `runsc` at
`/usr/local/bin`, a path Podman resolves by default — no daemon-style
registration exists or is needed). This environment composes the other two by
MRO — gVisor's sandboxing over Podman's driving — so **everything in
[GVISOR.md](GVISOR.md) and [PODMAN.md](PODMAN.md) applies here**: the staging
channel, the DNS repair, the proxy-outside-the-sandbox decision, the
short-name and SELinux relaxations, the rootless resource-limit gap. This
document covers only what is different at the seam.

How the composition stays additive in code: engine-specific host-side
interactions (runtime assert, inspect templates, runtime matching, project
discovery, removal) are overridable hooks on the gVisor base with Docker
defaults unchanged, and a known engine outside a class's supported set
redirects to the `--env` selector that drives it rather than raising.
`GVisorEnvironment._project_name` is a property computed from `session_id` —
identical value, made a property so it composes with `PodmanEnvironment`'s
read-only property of the same name.

## 1. Baseline: what the combined vanilla setup provides

The composition is strictly additive on the isolation axis. Podman
contributes the engine posture (no API socket anywhere, no daemon, rootless by
default — see PODMAN.md §1); gVisor contributes the workload boundary (Sentry
between the agent and the kernel, sandbox-private rootfs, host-side-only
evidence — see GVISOR.md §1). Rootless is the notable upgrade over
`--env gvisor`: Podman establishes the user namespace and invokes `runsc`
with UID 0 already mapped inside it — gVisor's supported rootless path — so
even a hypothetical full Sentry escape lands as an unprivileged host user
rather than root. Two smaller inherited wins: with no daemon there is no
`/etc/docker/daemon.json` runtime registry to tamper with, and Podman's
per-network aardvark-dns listens on a routable gateway address rather than
DNAT-to-loopback, so the embedded-DNS blindness that motivates GVISOR.md §2.2
is less absolute here (the same verified repair logic still governs, and only
rewrites when the resolver is actually unusable).

All of gVisor's host-side gates carry over verbatim: exec is blocked until
inspection confirms the runtime, the proxy must be *off* the sandbox runtime,
teardown is fail-closed against the exact project label, and staging-based
transfers replace `podman cp` (which cannot see the private rootfs any more
than `docker cp` can).

## 2. What was relaxed, changed, or newly trusted at the seam

### 2.1 Exec runs without a pseudo-TTY (`-T` injected)

Where: inherited from `PodmanEnvironment._run_docker_compose_command`
(PODMAN.md §2.5), which inserts `-T` into every programmatic `exec`.

Why it is load-bearing here rather than cosmetic: `podman-compose exec`
passes `--tty` unless told otherwise, Podman forwards terminal allocation to
the OCI runtime, and runsc's `exec` implements no such flag — every exec
fails with `flag provided but not defined: -tty`. crun merely tolerates the
pty, so for the plain Podman environment the same injection is a fidelity
fix; for this one it is a correctness requirement. Interactive `attach`
deliberately keeps its TTY.

### 2.2 SELinux at the seam: staging mounts relabeled, process label disabled

Where (mounts): `GVisorPodmanEnvironment._prepare_gvisor` passes
`selinux_relabel` (default `z`, from `PIER_PODMAN_SELINUX_RELABEL`) into
`write_compose_override`, which stamps `bind: {selinux: …}` on both staging
binds.

Why: Podman does not relabel binds (PODMAN.md §2.2), and under `--env gvisor`
no label question arises at all because Docker assigns runsc containers no
SELinux label. Under Podman the container *is* labeled, so on an enforcing
host the sandbox would be denied its own staging directories.

Blast radius: the same shared-`z` consequence as PODMAN.md §2.2, now covering
the transfer channel itself — `.gvisor-stage/{in,out}` become
`container_file_t` at the shared level, accessible to any container on the
host, layered on top of the channel already being the environment's largest
relaxation (GVISOR.md §2.1). The directories remain per-trial and are removed
on stop.

Where (process label): the same override carries `security_opt:
label=disable` on `main`, via
`write_compose_override(disable_process_label=True)`.

Why: Podman labels every container process on an enforcing host without
consulting the runtime's advertised features, and runsc aborts on any spec
that carries a label (`SELinux is not supported: system_u:system_r:
container_t:…`) — leaving `main` stuck in Created while `up --detach
--wait` blocks indefinitely, which is how the gap surfaced. Docker consults
runsc's `selinux: false` feature and assigns no label (GVISOR.md §2.5), so
only this flavor needs the opt-out.

Blast radius of the opt-out: `main`'s host-side runsc processes run without
the `container_t`-plus-MCS confinement a crun container would get. This
aligns the flavor with the Docker posture — where no label exists either
and Sentry is the boundary — rather than weakening below it, and it is
scoped to `main`: the override touches no other service, so the egress
proxy remains a labeled crun container.

### 2.3 Runtime trust rests on Podman's resolution, not a daemon registry

Where: `podman_runtime.assert_runtime_resolvable` (preflight and every
`start()`), `container_oci_runtime` + `runtime_name_matches` (verification).

What changed: Docker's daemon keeps an explicit runtime registry that
`assert_runtime_registered` queries authoritatively. Podman has no daemon;
"runsc" resolves at create time from `containers.conf` and a compiled-in path
table (`/usr/local/bin/runsc` among them). The preflight therefore makes
Podman itself perform that resolution — an image-free
`create --network none --rootfs <emptydir>` probe — rather than re-implement
it, and post-start verification reads `{{.OCIRuntime}}` (Podman's
`{{.HostConfig.Runtime}}` is a compat placeholder that always reads `"oci"`).
Because Podman reports a *name* or a *resolved path* depending on how the
runtime was selected, matching accepts `runsc` and `…/runsc` — exact basename
only, never a substring.

What is newly trusted: the filesystem path `/usr/local/bin/runsc` and any
`[engine.runtimes]` entry are now the runtime "registry". Anyone who can write
that path (or the user's `containers.conf`) chooses what "runsc" means —
whereas Docker's registration at least sits behind root-owned
`/etc/docker/daemon.json` plus a daemon restart. Rootless cuts both ways here:
a *user-level* `containers.conf` can redirect the runtime for that user
without any privilege. The post-start check would still catch a runtime that
reports a different name, but a malicious binary *named* runsc reports
whatever it likes — which is true of Docker's registry too if the binary
behind the registered path is replaced; the difference is who can do the
replacing. The probe also creates (and force-removes, by fixed name) a real
container per check — writes to Podman storage that vanilla preflights don't
make.

Hardened since: the install script pins the binary's SHA3-512 at
`/usr/local/share/pier/runsc.sha3-512` (trust-on-first-use when runsc
pre-existed; an existing pin is never overwritten, so replacing the binary
and re-running init cannot silently re-bless it), and
`assert_runtime_digest` — run at CLI preflight and again at every start —
fails closed when the pinned binary changed or vanished. A missing pin file
is the only pass-through, for hosts provisioned before pinning
(`PIER_RUNSC_DIGEST_PIN` relocates it). The script also registers the
resolved path in root-owned `/etc/containers/containers.conf.d/
pier-runsc.conf`, restoring a root-gated registry analogous to Docker's, and
warns when a user-level `containers.conf` mentions runsc. Residual trust:
the user-level override is warned about, not blocked (Podman's precedence is
not Pier's to change), and an attacker with root can rewrite pin and binary
together — the pin raises "anyone who can write the path" to "root", not to
impossible.

### 2.4 Rootless export ownership: staged copies chowned to in-container root

Where: `GVisorPodmanUnixOps._host_owner()` returns `"0:0"`, overriding
`GVisorUnixOps`'s `os.getuid():os.getgid()`.

Why: a correctness inversion, not a weakening. Under rootful Docker,
in-container UIDs are host UIDs, so chowning the staged export to the host
user's numeric UID is right. Under rootless Podman that same chown resolves
through the userns into the subuid range, leaving exports the host cannot
write; in-container `0:0` is what maps back to the invoking user. Under
*rootful* Podman the same rule degrades gracefully (0 maps to 0 — the
invoker). The chown still targets only the staged copy with `-h`, never the
in-container source, preserving the symlink posture of GVISOR.md §2.1.

### 2.5 Teardown discovery widened: label unions and name references

Where: `project_container_ids_podman`, `project_network_refs_podman`,
`service_container_ids` (`podman_runtime.py`);
`_compose_container_id` override in `gvisor/podman.py`.

Why, three empirically-forced changes. First, podman-compose stamps both
`com.docker.compose.*` and `io.podman.compose.*` labels with the split varying
by version, so fail-closed discovery queries both namespaces and unions
results — a single-label query could under-count, and under-counting is
exactly what fail-closed teardown must never do. Second,
`podman network ls --quiet` prints *names*, not hex IDs; the Docker hex-only
parser would silently discard every result and teardown would report "nothing
to remove" while networks remain — a fail-open bug this environment's parser
(`parse_network_refs`) exists to prevent. Third, `podman-compose ps` ignores
its service argument and lists the whole project with `--all`, so the parent's
service resolution would hand verification an arbitrary project container —
the trusted proxy, or a stopped one — as readily as the running `main`;
label-filtered `podman ps` restores both the service scoping and the
running-only default the verification contract states. None of these relax a
boundary; they widen *queries* so the existing fail-closed guarantees keep
holding on Podman's output formats. Container discovery no longer trusts
podman-compose's stamping at all: every container also carries a
``pier.trial=<project>`` label that Pier itself passes through
``--podman-run-args`` (PodmanEnvironment._compose_base), and the discovery
union includes it. The residual trust is scoped to *networks*, which
podman-compose creates without run-args and which therefore still carry only
the compose namespaces.

### 2.6 Resource limits: the Podman gap, with a gVisor wrinkle

Inherited: `resource_capabilities` resolves to `PodmanEnvironment`'s cgroup
detection (PODMAN.md §2.3), so v1/undelegated hosts reject `LIMIT` tasks and
run `AUTO` tasks unbounded. The wrinkle this environment adds: even where
controllers *are* delegated, limits are now applied by `runsc` inside a user
namespace, and gVisor documents that cgroup configuration errors on this path
may be ignored rather than fatal. The capability report can therefore be
optimistic in ways the plain Podman environment's isn't — controllers
detected, limit accepted, enforcement silently absent. Two backstops:
runsc is registered without `--ignore-cgroups`, so where cgroup application
fails loudly it fails loudly; and the post-start enforcement verification
inherited from PODMAN.md §2.3 reads `main`'s `cpu.max`/`memory.max` from the
host after every start, so the silent-ignore path is detected — fatal for
`LIMIT`/`GUARANTEE` tasks, a logged warning for `AUTO` — rather than trusted.

### 2.7 Validation posture: rootful- and rootless-verified

The end-to-end validation in this repository's history ran against rootful
Podman 4.9 + runsc release-20260803.0 (build, `OCIRuntime=runsc` from the
host, `4.19.0-gvisor`
from inside, staging round trips, sysrq denied with EIO, clean teardown). The
rootless-specific code paths — §2.4's ownership rule, the no-op host chown,
§2.2's relabel — are unit-tested. A first live run on a rootless,
SELinux-enforcing host (Fedora, rootless Podman, Enforcing; 2026-08-10)
surfaced exactly one startup gap — the unconditional process label §2.2's
opt-out now disables — after which `main` starts, host-side inspection
reports `OCIRuntime=runsc` with an empty process label, and the sandbox
reports `4.19.0-gvisor`. A full `make smoke-gvisor-podman` run on that host
is on record from the same day: unit suite green and all three smoke trials
— offline fix-git behind the egress proxy, egress-dependent build-pmars,
and verify-gvisor-podman-env's in-sandbox boundary assertion — at reward
1.0 with zero errored trials and zero containers or networks left after
teardown. Rootless behavior under enforcement is proven, not just
designed.

## 3. Inherited and functional limitations (not relaxations)

Everything in GVISOR.md §3 (Linux only, Dockerfile/prebuilt tasks only,
syscall-compatibility and performance costs, bash `/dev/tcp` probe) and
PODMAN.md §3 (podman-compose ≥ 1.6.0, netavark + aardvark-dns for the proxy,
no `--project-directory`, Linux images only). Additionally: rootless
networking rides pasta/slirp4netns at the host edge, adding a user-mode hop to
all egress; and nested rootless operation (Podman-in-a-container) is outside
what this environment claims — gVisor's own rootless documentation describes
narrower UID-mapping and networking support there.

## 4. Hardening avenues

**Runtime resolution trust (2.3).** (a) and (b) are implemented (§2.3):
SHA3-512 pin verified fail-closed at preflight and every start, root-owned
`containers.conf.d` registration, user-level override warning at init.
Remaining: block (not just warn on) user-level `[engine.runtimes]` redirects
by having preflight parse the user config, if the added config-parsing
surface is ever judged worth it.
(c) is implemented: after each verification gate passes, the shared gVisor
base writes `runtime-verification.json` into the trial directory recording
the engine-reported runtime identity for `main` and the proxy verbatim (a
name when selected by name, a resolved path when selected by path — Pier
does not re-derive paths from names, which would re-implement the engine's
search order). Post-hoc audit sees what the engine claimed each trial ran
under; (a)'s digest pin is what would upgrade the claim to proof.

**Staging relabel (2.2).** The per-trial MCS-category avenue from PODMAN.md §4
applies directly and is *more* valuable here: assigning the trial container
and separate-mode verifier one private category pair and relabeling staging
with `Z` would remove the only cross-container-readable surface this
environment adds, at the cost of plumbing a per-trial level into the compose
override (one more key next to `selinux_relabel`). §2.2's process-label
opt-out constrains the mechanics: `main` carries no label to hang a category
pair on, so the private level must be stamped on the staging directories
directly and granted to the verifier's label — unconfined `main` still
reaches its own staging, while other labeled containers lose access.

**Cgroup honesty (2.6).** Implemented via the inherited post-start
enforcement verification (PODMAN.md §2.3): declared limits are read back
from `main`'s cgroup files host-side after every start, so the silent-ignore
path is detectable and fatal where enforcement was promised. Remaining: the
systemd transient-scope ceiling from PODMAN.md §4 as an engine-external
backstop for `AUTO` tasks on hosts that cannot enforce.

**Rootless proof (2.7).** Not code: stand up a rootless, cgroups-v2-delegated,
SELinux-enforcing host in CI and put `make smoke-gvisor-podman` in the merge
gate for this environment's files. The smoke task
(`examples/smoke/verify-gvisor-podman-env`) already asserts the boundary from
inside, and §2.7's one-off run proves it passes on such a host; what's missing
is making that run continuous rather than on-record-once.

**Label-stamping dependency (2.5).** Implemented for containers: Pier stamps
`pier.trial=<project>` through `--podman-run-args` and the discovery union
queries it, so container teardown owns a label no podman-compose version
change can take away. Remaining: networks are created by podman-compose
without run-args, so network discovery still rides the compose namespaces —
closable by creating the network Pier-side before `up` with its own label.
