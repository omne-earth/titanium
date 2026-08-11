# Podman environment: protections, relaxations, and hardening avenues

Selector `--env podman`, class `pier.environments.podman.podman.PodmanEnvironment`,
provisioned and audited by `scripts/doctor/podman.sh` (report-only by default, `--bootstrap` to provision;
formerly `--fix`). This document records what the vanilla setup
protects, exactly where Pier relaxed it to make trials run, and concrete ways
each relaxation could be closed. Its siblings are
[GVISOR.md](GVISOR.md) and [GVISOR-PODMAN.md](GVISOR-PODMAN.md); the
gvisor-podman document assumes this one and describes only its deltas.

## 1. Baseline: what the vanilla setup provides

"Vanilla" here means stock Podman driven the way this environment drives it,
before any Pier-specific configuration. That baseline is meaningfully stronger
than the Docker environment's in three ways, and Pier preserves all three.

**No control socket anywhere in the path.** `podman-compose` emits `podman`
CLI calls against libpod in-process, so there is no Docker API socket and no
`podman.socket` in the trust chain. A container that somehow reached the host
filesystem would find no engine endpoint to escalate through, and
`_compose_env()` (`podman.py`) strips `DOCKER_HOST` from every compose
invocation so nothing downstream quietly falls back to one. The doctor script
warns when socket units or socket files exist at all, even though nothing here
uses them.

**Rootless by default.** Podman's native mode maps container root to the
invoking user through a user namespace; a full container escape lands as an
unprivileged user, not host root. Nothing in Pier requires rootful Podman.

**Runner separation (default when provisioned).** "Unprivileged user" is
only as comforting as what that user owns — without separation it is the
operator, with their SSH/GPG keys, API secrets, and source tree. Once
`scripts/init/titanium.sh` has provisioned the `titanium` user, every
podman-family run (`--env podman`, `--env gvisor-podman`) executes wholly as
that user by default — opt out with `RUNNER=`; unprovisioned hosts run as
the invoking user unchanged. The docker-daemon environments are deliberately
outside the wrapper: they need socket access, and the runner must never
join the docker group — that group is root-equivalent, and granting it
would nullify the separation. The
wrapper (`scripts/titanium-run.sh`) is one systemd scope per run: no
per-command `sudo -u`, no API socket, so an escape lands as an account
owning nothing but trial state. Provisioning covers: nologin user,
subuid range, linger, cgroup delegation, traversal-plus-read ACLs on the
repo, read-write plus default ACLs on `.run/` so operator and runner both
keep access to trial output — and a root-owned, runner-unwritable
`~titanium/.config/containers`, which structurally closes the user-level
runtime-redirect residual of GVISOR-PODMAN.md §2.3. The environment variables
a trial legitimately needs (model API credentials, `PIER_*` knobs) are the
only ones that cross into the scope. Because trial containers and images
live in the runner's storage — invisible to the operator's own podman —
`make podman-<subcommand> [ARGS=…]` proxies any podman command into the
runner's context for inspection (`make podman-ps ARGS=--all`).

**Daemonless lifecycle.** There is no long-lived privileged daemon whose
compromise affects every container on the host; each `podman` invocation is
its own process tree under the invoking user.

On top of the engine baseline, the environment keeps Pier's standard network
posture: `allow_internet = false` tasks run with `network_mode: none`;
allowlist tasks put `main` on an `internal: true` network with a Squid proxy
(runc/crun, per-trial basic-auth token, domain allowlist) as the only route
out; the shared pod is disabled (`--in-pod=false`) precisely so `main`'s
no-network mode cannot be undone by pod-level namespace sharing.

## 2. What was relaxed, where, and why

### 2.1 Short-name resolution: retired — Pier qualifies its image references

What vanilla does: Podman's compiled-in default is `short-name-mode =
"enforcing"`, which refuses to resolve an unqualified image name (`FROM
ubuntu:24.04`) without an interactive registry choice. That is a supply-chain
protection: a short name is ambiguous across registries, and enforcing mode
makes the ambiguity a human decision instead of a silent pick.

What Pier does now: nothing is relaxed. Short names in the Dockerfile dialect
tasks are written in mean Docker Hub, so build preparation makes that meaning
explicit at the byte level (`qualify_image_reference` /
`qualify_dockerfile_froms` in `agent_setup.py`): the agent-install rewrite
qualifies every `FROM` in the embedded task Dockerfile (multi-stage
references and `scratch`/variable bases excluded), the prebuilt image name is
qualified the same way, and the egress proxy image is written qualified.
Enforcing mode never fires because no unqualified name reaches Podman on the
standard flow, and the doctor no longer writes `registries.conf` at all — it
warns when a previously written permissive configuration is still present.

Residual: a task built directly from its own Dockerfile with *no* agent
install bypasses the rewrite (Pier builds the task's context verbatim), so a
short name there still resolves — or under enforcing, fails — through host
configuration. The doctor's message names this case; the fix is qualifying
the task's own `FROM` line, not relaxing the host.

The same trust posture governs *which* image is used at all
(`_effective_docker_image`, shared by every local compose-driven
environment): when a task ships both a Dockerfile and a `docker_image`
prebuilt, the prebuilt — typically a mutable-tag build cache of that same
Dockerfile on a third-party registry account — is ignored and the Dockerfile
is built locally with qualified FROMs. `PIER_IMAGE_SOURCE=prebuilt` opts
into upstream-parity byte-exactness where that matters more than
auditability; image-only tasks are unaffected either way. The remote
Daytona/Modal environments retain their own prebuilt handling and are
outside this policy.

Airgapped hosts close the loop with `make images-vendor` /
`make images-restore` (`scripts/images/`): vendor collects every reference a
task set can reach — Dockerfile FROMs qualified identically to build
preparation, image-only tasks' prebuilts, the proxy base, and with
`--prebuilt` the full prebuilt set — into one `podman save` archive that
restore loads where nothing can be pulled.

### 2.2 SELinux relabel: shared `z`, not private `Z`

Where: `PodmanEnvironment._apply_selinux_relabel()` (`podman.py`) stamps
`bind: {selinux: "z"}` on every bind mount in the trial's mount set; the knob
is `PIER_PODMAN_SELINUX_RELABEL` (`z` default, `Z` accepted, anything else
disables).

What vanilla does: Podman does not relabel bind mounts at all. On an enforcing
host that is itself a protection — the container's SELinux domain simply
cannot touch host files that don't carry a container-accessible type — but it
also means the agent dies writing its own log directories.

Why it was relaxed: the mounted log and artifact directories must be writable
from inside. `z` (shared category) rather than `Z` (private category) is
deliberate: a separate-mode verifier container mounts the same artifact
directories, and a private category assigned to the trial container would lock
the verifier out.

Blast radius: relabeling *modifies the host directories' labels in place* —
this is not a per-mount view. Anything under the trial's mounted directories
becomes `container_file_t` with the shared level, readable and writable by
*any* container on the host, not just this trial's pair. The scope is confined
to trial directories (Pier controls what gets mounted), but for the lifetime
of those labels the SELinux boundary between unrelated containers does not
apply to that data.

### 2.3 Resource limits: unenforced under rootless cgroups v1 (and honest about it)

Where: `PodmanEnvironment.resource_capabilities()` /
`_cgroup_controllers()` (`podman.py`).

What vanilla docker does: `--cpus` / `--memory` always enforce, because the
rootful daemon owns the cgroup tree.

What happens here: rootless Podman enforces limits only on cgroups v2 with
the cpu and memory controllers delegated to the user; on v1, or without
delegation, it *warns and silently drops the limit*. Pier's mitigation is to
report `cpu_limit=False` / `memory_limit=False` in that situation so tasks
declaring `LIMIT`/`GUARANTEE` enforcement are rejected up front instead of
running unbounded. When Podman cannot even be queried, the code assumes the
common modern case (`True, True`) rather than blocking — an availability-over-
enforcement choice; deployments that prefer refusal set
`PIER_PODMAN_CGROUP_FAIL_CLOSED=1` to make that fallback report no support.

Capability reporting is backed by post-start verification
(`_verify_resource_limits`, called from `start()`): when a task declares a
cpu or memory limit, the trusted host resolves `main`'s cgroup directory
(`podman inspect {{.State.CgroupPath}}` under `/sys/fs/cgroup`) and reads
`cpu.max` / `memory.max` itself. A file reporting `max` — the signature of a
silently dropped limit — or a value more than 10% off the declaration fails
the start for `LIMIT`/`GUARANTEE` tasks and logs a warning for `AUTO` tasks.
This verifies *enforcement*, not configuration: the same trust posture as
runtime verification, and the only signal that survives Podman accepting a
flag it cannot honor.

Blast radius: on affected hosts, tasks in `AUTO` resource mode still run with
**no CPU or memory ceiling at all** — verification makes the gap loud (and
fatal where enforcement was promised), not closed. A runaway or adversarial
`AUTO` workload on such a host remains bounded only by the host.

### 2.4 Host artifact ownership: chown-to-host-user disabled

Where: `PodmanEnvironment._chown_to_host_user()` overrides the Docker parent
with a no-op.

Why: this is a correctness fix, not a weakening — under rootless Podman the
parent's `chown $(id -u)` would resolve through the user namespace into the
subuid range, producing host files the invoking user cannot write. Container
root already maps to the invoking user, so root-written files arrive correctly
owned.

Residual limitation rather than relaxation: files written by *non-root*
in-container users land on the host owned by a subuid. They are readable but
not writable/removable by the host user until recovered with
`podman unshare chown -R 0:0 <dir>`. Trials whose agents run as a non-root
`default_user` will exhibit this on their artifact directories.

### 2.5 Exec fidelity: programmatic execs run without a pseudo-TTY (resolved)

Where: `PodmanEnvironment._run_docker_compose_command` injects `-T` into
every programmatic `exec`; interactive `attach` builds its own command and
keeps its TTY.

History: `podman-compose exec` passes `--tty` unless given `-T`, and this
environment originally inherited that — crun tolerates the pty, so nothing
failed, but pty line discipline rewrote `\n` to `\r\n` in captured output and
merged stream interleaving differently than a pipe would, so transcripts saw
terminal-mangled bytes. The injection that gvisor-podman needed for
correctness (runsc rejects the flag outright; see GVISOR-PODMAN.md §2.1) is
now applied here for fidelity, and gvisor-podman inherits it.

## 3. Inherited and functional limitations (not relaxations)

Linux containers only — `capabilities.windows` is `False` and the constructor
rejects `[environment].os = "windows"` tasks. podman-compose must be ≥ 1.6.0
(for `depends_on: service_healthy`, which the proxy health gate uses).
Service-name DNS for the egress proxy needs netavark + aardvark-dns; CNI hosts
need the dnsname plugin or a migration (the doctor checks this).
`podman-compose` lacks `--project-directory`, so the subprocess cwd stands in
for it, and lacks a `cp` subcommand, so transfers resolve the container by
label and use `podman cp` (`podman_unix.py`).

## 4. Hardening avenues

**[PARTIAL] Short names (2.1).** Qualification at build preparation is implemented and
the doctor no longer writes `registries.conf` (§2.1). Remaining: (a) extend
the rewrite to the direct-Dockerfile-without-agent-install path by always
building from a prepared copy of the task context; (b) for curated datasets,
pin digests (`FROM ubuntu@sha256:…`) at dataset-ingestion time, which removes
the resolution question altogether and pairs with the local image-supply
work.

**[ ] SELinux sharing (2.2).** (a) Assign the trial container and its
separate-mode verifier the *same* private MCS category pair
(`--security-opt label=level:s0:cX,cY`, one pair generated per trial), then
relabel with `Z`; the two cooperating containers share the data while every
other container on the host is excluded — this restores the inter-container
boundary at the cost of plumbing a per-trial level through both container
launches. (b) Relabel externally once at trial-directory creation
(`chcon -R`) with a per-trial level and set
`PIER_PODMAN_SELINUX_RELABEL=none`, keeping label management in trusted host
code. (c) Generate a scoped policy with udica if categories prove too coarse.

**[PARTIAL] Resource limits (2.3).** Post-start enforcement verification, the
fail-closed fallback flag, and doctor coverage are implemented: the doctor
reports cgroups version and delegated controllers, and `--fix` writes a
`Delegate=cpu cpuset io memory pids` drop-in for `user@.service` (applies at
next login). Remaining: for hosts stuck on v1, wrap trials in a systemd
transient scope (`systemd-run --user --scope -p MemoryMax=…`) as an
engine-external ceiling for `AUTO` tasks; coarser than per-container cgroups
but real.

**[ ] Artifact ownership (2.4).** (a) Run a `podman unshare chown -R 0:0` recovery
pass over artifact directories in `prepare_logs_for_host()` — host-side,
cheap, and closes the gap for non-root agents without touching in-container
state. (b) Alternatively `podman cp` artifacts out instead of relying on the
bind view, since `cp` writes as the invoking user.

**[DONE] Exec fidelity (2.5).** Implemented — programmatic execs inject `-T` in
`PodmanEnvironment._run_docker_compose_command`, transcript bytes are
pipe-clean, and gvisor-podman inherits the injection instead of carrying its
own override.
