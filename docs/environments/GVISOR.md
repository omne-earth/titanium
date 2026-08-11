# gVisor environment: protections, relaxations, and hardening avenues

Selector `--env gvisor`, class
`titanium.environments.gvisor.environment.GVisorEnvironment`, provisioned by
`scripts/init/runsc.sh` (installs a checksum-verified `runsc`, registers it
with the Docker daemon). This document records what vanilla runsc-under-Docker
protects, exactly where Titanium relaxed it to make trials run, and concrete ways
each relaxation could be closed. Siblings: [PODMAN.md](PODMAN.md),
[GVISOR-PODMAN.md](GVISOR-PODMAN.md).

## 1. Baseline: what the vanilla setup provides

runsc registered with default flags gives every container an application
kernel (Sentry) between the workload and the host: guest syscalls terminate in
a memory-safe Go reimplementation of the Linux surface, and Sentry itself
speaks a narrow, seccomp-restricted set of host syscalls. Three defaults
matter most to this environment's design:

**The root filesystem is sandbox-private.** `--file-access=exclusive` with
`--overlay2=root:self` means writes to the container rootfs live in an overlay
the host cannot observe, and host-side writes into the image are not observed
by a running sandbox. `docker cp` is unusable in both directions — which is a
*protection*: there is no host↔sandbox file channel unless one is explicitly
constructed.

**The network stack is the sandbox's own.** Netstack does not inherit the
container netns's netfilter rules, so Docker's embedded DNS at `127.0.0.11` —
reached via DNAT to loopback — is unreachable (google/gvisor#7469). For an
allowlist task this is incidentally ideal: the sandbox has no working DNS at
all, and only the trusted proxy (addressed by literal IP) resolves names.

**In-sandbox evidence is worthless, and the design honors that.** gVisor's own
documentation warns that `dmesg`/`uname` are attacker-replicable. Every gate
in this environment is host-side: `exec` is blocked until `docker inspect`
confirms, from the trusted host, that the daemon ran `main` under the
requested runtime (`_ensure_verified` / `_verify_main_runtime`), verification
runs exactly once with explicit states, and a failed environment can never
become ready. Teardown is fail-closed against the exact Compose project label
and refuses to report clean while resources remain.

Protections Titanium *added* on top of vanilla: `security_opt:
no-new-privileges:true` on `main` (vanilla Titanium Docker sets no security_opt at
all); bind-mount sources confined to the trial directory
(`_validate_mounts`) because a Compose override cannot remove a volume, so
refusing to start is the only enforcement point; docker-compose tasks rejected
outright (`docker_compose=False`) because a task's own compose file could
re-add `privileged`, `cap_add`, `devices`, or extra volumes that an override
cannot strip; connectivity for `allow_internet = true` is *proven* by an
in-sandbox probe after runtime verification, never assumed, with failure
tearing the environment down rather than downgrading the network mode; and
each passed verification gate records the engine-reported runtime identity
into the trial's `runtime-verification.json` for post-hoc audit.

## 2. What was relaxed, where, and why

### 2.1 Two staging bind mounts puncture the sandbox-private filesystem

Where: `write_compose_override` (`runtime.py`) mounts
`<trial>/.gvisor-stage/in` read-only at `/.titanium-stage/in` and
`<trial>/.gvisor-stage/out` writable at `/.titanium-stage/out`;
`GVisorUnixOps` (`transfer.py`) moves every upload and download through them.

What vanilla does: nothing crosses the rootfs boundary. Bind mounts default to
`--file-access-mounts=shared`, so mounting one is precisely the act of opening
a coherent, bidirectional host↔sandbox channel.

Why it was relaxed: Titanium's contract requires file transfer with a *running*
environment (task setup, artifact export, verifier inputs), and `docker
compose cp` is unusable against the private rootfs in either direction. There
is deliberately no `docker cp` fast path even for paths present in the image,
because it would return stale image content instead of what the sandbox wrote.

Blast radius: the sandbox — including a fully compromised agent — can write
arbitrary content into one host directory. The relaxation is tightly fenced:
the directory is per-trial (no cross-trial sharing possible by path
construction), `in` is read-only, both are deleted on stop, and the host side
treats everything staged as hostile — exports are placed with
component-by-component `O_NOFOLLOW` directory-descriptor walks, `O_EXCL` leaf
creation, special files rejected, and symlinks copied as links, so a planted
symlink cannot redirect a write outside the tree (`safe_place_file`,
`safe_copy_tree`). What remains is the channel itself: host-side code that
later *consumes* staged bytes (verifiers, viewers) is parsing
attacker-controlled data, and the writable mount is a disk-space vector
bounded only by the host filesystem.

Note also that Titanium's standard log/artifact binds (agent dir, verifier dir,
artifacts) exist here exactly as in the Docker environment, made
world-writable by the `chmod 777` in `DockerEnvironment.start()` — the gVisor
environment constrains their *sources* to the trial directory but does not
remove them.

### 2.2 Unrestricted-internet tasks get direct DNS to host-configured resolvers

Where: `network.select_resolvers` reads `/etc/resolv.conf` and
`/run/systemd/resolve/resolv.conf`; `_normalize_sandbox_dns`
(`environment.py`) rewrites the sandbox's `/etc/resolv.conf` with those
addresses when the existing resolver is unusable; the same list is emitted as
the Compose `dns:` key as declarative belt-and-braces.

What vanilla does: DNS through Docker's embedded resolver, which scopes
service names and applies the daemon's DNS configuration — except that under
gVisor it simply doesn't work (§1), so vanilla behavior for an
`allow_internet = true` task is *no name resolution at all*.

Why it was relaxed: an unrestricted-internet task that cannot resolve a single
hostname is not running its declared policy. The trusted control plane repairs
the resolver only when unusable, only in unrestricted mode (allowlist and
no-network tasks are never touched — a rewrite there would create DNS egress
the policy does not grant), and only after runtime verification. There is
deliberately no hardcoded public fallback: a host with only a loopback stub
fails closed with instructions (`--ek dns=…`).

Blast radius: the sandbox sends DNS directly (UDP/TCP 53) to the host's
upstream resolvers, bypassing any embedded-DNS-level policy, and learns those
resolver addresses — a small host-configuration disclosure. Queries from an
untrusted workload now originate against infrastructure resolvers with no
per-trial attribution or filtering. Scope: `allow_internet = true` tasks only,
which have unrestricted egress by declaration anyway; the relaxation is about
*which* resolvers see the queries and what the sandbox learns, not about new
reachability.

### 2.3 The trusted egress proxy runs outside the sandbox runtime

Where: `_verify_proxy_runtime` (`environment.py`) *requires* the proxy to be
on Docker's default runtime and fails if it lands under runsc.

Why it was relaxed: the proxy is the component that still needs Docker's
embedded DNS to resolve allowlisted hostnames; under runsc it would inherit
the same resolver blindness as the sandbox. Its address is resolved host-side
from `docker inspect` and handed to the agent as a literal IP.

Blast radius: a network-facing service (Squid, plus Ubuntu userland) processes
hostile traffic from the sandbox under runc isolation rather than gVisor. The
existing fences are real — per-trial token auth, domain allowlist, `internal`
network on the sandbox side, no volumes on the proxy service, and the runtime
check ensures a misconfiguration cannot silently swap which side is sandboxed
— but a Squid RCE would be contained by runc, not Sentry.

### 2.4 Transfers execute as root inside the sandbox

Where: `GVisorUnixOps._exec_root` (`transfer.py`): every upload/download runs
a shell pipeline (`cp -a`, `chown`, `mkdir`) as the sandbox's root user, via
the gated `exec` path.

What vanilla does: `docker cp` is a host-side operation; the container's
cooperation is not required.

Why it was relaxed: the private rootfs makes host-side copies impossible; the
copy between staging and the rootfs can only happen from inside.

Blast radius: transfer *availability and integrity* depend on the sandbox
executing honestly — a compromised sandbox can drop, alter, or fabricate
transferred content. This is a smaller concession than it looks, because
artifacts originate inside the sandbox regardless; the meaningful line held is
that transfers are never used as *evidence* (runtime verification is
inspect-only) and host-side placement treats staged output as hostile (§2.1).
Commands are built with `shlex.quote` throughout, so paths cannot smuggle
shell.

### 2.5 Thin security_opt, and no SELinux label at all

Where: `SECURITY_OPT = ["no-new-privileges:true"]` (`runtime.py`), with a
comment recording that `label=disable` is deliberately absent from this
shared baseline. The Podman flavor opts in per service
(`write_compose_override(disable_process_label=True)`) because Podman —
unlike Docker — labels the container process unconditionally, which runsc
rejects; see GVISOR-PODMAN.md §2.2.

The absence of an SELinux label is upstream behavior, not Titanium's choice: runsc
advertises `selinux: false` in its OCI features, so Docker assigns no process
label to the sandbox — there is nothing to disable and nothing confining
Sentry beyond its own seccomp filters and namespaces. Titanium adds
no-new-privileges but no `cap_drop`, no pids limit, and no custom seccomp on
the sandbox process. The threat model leans on Sentry being the boundary;
these would be defense-in-depth for the *host-side* runsc processes.

## 3. Inherited and functional limitations (not relaxations)

Linux hosts only (`_validate_definition` refuses otherwise). Dockerfile and
prebuilt-image tasks only — compose tasks are rejected (§1). Workloads relying
on unimplemented syscalls, exotic `/proc`, or hardware access fail under
Sentry; the connectivity probe additionally requires the image's `bash` to
support `/dev/tcp` redirection. Syscall- and I/O-heavy workloads pay gVisor's
usual performance tax. The runtime must be registered with the daemon before
start (`assert_runtime_registered`, checked again at CLI preflight), and
save/restore, GPU, and Windows paths are out of scope.

## 4. Hardening avenues

**[ ] Staging channel (2.1).** (a) Quota the writable side: back
`.gvisor-stage/out` with a size-capped loopback filesystem or project quota at
trial setup, converting the disk-exhaustion vector into a bounded failure.
(b) Narrow its lifetime: mount `out` at start but keep the host directory
`0700` under a dedicated low-privilege user, opening it only for the duration
of a download operation — coherence is per-operation, so nothing requires the
host side to be readable between transfers. (c) Treat consumption as the
boundary: run verifiers that parse exported artifacts inside their own
sandbox (the separate-verifier mode already exists), so hostile staged bytes
never meet unsandboxed parsers. (d) Longer term, replace the shared-directory
channel with a `runsc`-mediated stream (e.g. exec with stdin/stdout payload
framing, which the exec gate already covers), eliminating the persistent
mount; cost is a rewrite of `transfer.py` and losing `cp -a` fidelity for
sparse/ownership edge cases.

**[ ] DNS (2.2).** (a) Run a per-trial forwarding resolver as a third compose
service under runc (dnsmasq/CoreDNS), point the override's `dns:` and the
resolv.conf repair at its bridge IP, and let *it* hold the host/upstream
configuration — the sandbox then sees one per-trial IP, queries become
attributable and filterable, and host resolver addresses stay hidden. The
proxy-verification pattern (§2.3) already shows how to keep such a helper off
the sandbox runtime. (b) Cheaper variant: default `--ek dns=` from Titanium
configuration (operator-chosen resolver) so trials never inherit
infrastructure resolvers implicitly. (c) For deployments that want DNS policy
even on `allow_internet = true`, reuse the Squid pattern with DNS-over-HTTPS
upstream inside the helper, giving log-and-block capability per trial.

**[PARTIAL] Proxy exposure (2.3).** (a) Shrink the proxy image: a distroless or
Alpine-static Squid (or a purpose-built ~200-line CONNECT proxy) removes most
of the Ubuntu userland from the attack surface. (b) Confine it further under
runc: `cap_drop: [ALL]`, `no-new-privileges`, read-only rootfs with tmpfs for
Squid's runtime files — all expressible in the proxy compose Titanium already
writes. (c) Revisit sandboxing the proxy itself under runsc with a *static*
resolver configuration (the proxy's upstream DNS could use the §(a) helper or
an explicit `dns:` IP), which the current code forbids only because embedded
DNS was assumed necessary.

**[ ] In-sandbox transfer trust (2.4).** (a) Add content addressing: hash staged
payloads host-side before upload and after export, recording digests in trial
artifacts so tampering is at least evident, if not preventable. (b) For
uploads specifically, pre-stage into the *image* at build time where the task
allows it, shrinking the set of live-transfer operations.

**[ ] Host-side depth (2.5).** Add `cap_drop: [ALL]` plus a pids limit to the
override's `security_opt`/service config and verify runsc still operates
(it should — Sentry needs no in-container capabilities); this costs one line
in `write_compose_override` per knob and buys defense-in-depth against Docker
misrouting a container to runc, complementing the runtime verification that
already detects exactly that.
