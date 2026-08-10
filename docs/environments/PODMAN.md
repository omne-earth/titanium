# Podman environment: protections, relaxations, and hardening avenues

Selector `--env podman`, class `pier.environments.podman.podman.PodmanEnvironment`,
provisioned and audited by `scripts/doctor/podman.sh` (report-only by default,
`--fix` to apply changes). This document records what the vanilla setup
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

### 2.1 Short-name resolution: `enforcing` → `permissive` with docker.io search

Where: `scripts/doctor/podman.sh --fix` writes
`unqualified-search-registries = ["docker.io"]` and
`short-name-mode = "permissive"` into the user-level
`~/.config/containers/registries.conf` (which, when present, replaces the
`/etc` file wholesale).

What vanilla does: Podman's compiled-in default is `short-name-mode =
"enforcing"`, which refuses to resolve an unqualified image name (`FROM
ubuntu:24.04`) without an interactive registry choice. That is a supply-chain
protection: a short name is ambiguous across registries, and enforcing mode
makes the ambiguity a human decision instead of a silent pick.

Why it was relaxed: task Dockerfiles are written in Docker's world, where
short names always mean Docker Hub, and builds run non-interactively — the
enforcing prompt is a hard failure inside `podman-compose build`. Permissive
mode with a single search registry restores Docker's behavior.

Blast radius: every unqualified image reference on the host — not just Pier's
— now resolves to docker.io without confirmation. The pick is pinned to one
registry, so the classic cross-registry squatting attack is off the table, but
a typo-squatted or hijacked docker.io name is pulled without the pause
enforcing mode would have imposed. The `--fix` write also replaces the entire
effective registries.conf for that user, discarding any distribution-shipped
mirror or blocking configuration.

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
enforcement choice worth knowing about.

Blast radius: on affected hosts, tasks in `AUTO` resource mode run with **no
CPU or memory ceiling at all**. A runaway or adversarial workload is bounded
only by the host. This is the single largest practical relaxation of this
environment on older hosts.

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

### 2.5 Exec allocates a pseudo-TTY

Where: inherited behavior — `podman-compose exec` passes `--tty` unless given
`-T` (`compose_exec_args` in podman-compose), and
`DockerEnvironment.exec()` does not pass `-T`.

What this changes: every programmatic exec runs on a pty. crun tolerates it,
so nothing fails, but pty line discipline rewrites `\n` to `\r\n` in captured
output and merges the streams' interleaving differently than a pipe would.
This is a *fidelity* relaxation: transcripts and any output-sensitive parsing
see terminal-mangled bytes. (The gvisor-podman environment already injects
`-T` because runsc rejects the flag outright; see GVISOR-PODMAN.md §2.1.)

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

**Short names (2.1).** Several options, combinable. (a) Fully qualify images
at build preparation: Pier already rewrites the task Dockerfile for agent
installs (`write_agent_dockerfile`), so rewriting `FROM ubuntu:24.04` to
`FROM docker.io/library/ubuntu:24.04` there removes the need for permissive
mode without touching task sources — the cleanest fix, and it lets the doctor
stop writing registries.conf entirely. (b) Keep `enforcing` and ship a
per-name alias table in a `registries.conf.d` drop-in instead of a wholesale
user file, which preserves the distribution's configuration and confines the
relaxation to exactly the names tasks use. (c) For curated datasets, pin
digests (`FROM ubuntu@sha256:…`) at dataset-ingestion time, which removes the
resolution question altogether.

**SELinux sharing (2.2).** (a) Assign the trial container and its
separate-mode verifier the *same* private MCS category pair
(`--security-opt label=level:s0:cX,cY`, one pair generated per trial), then
relabel with `Z`; the two cooperating containers share the data while every
other container on the host is excluded — this restores the inter-container
boundary at the cost of plumbing a per-trial level through both container
launches. (b) Relabel externally once at trial-directory creation
(`chcon -R`) with a per-trial level and set
`PIER_PODMAN_SELINUX_RELABEL=none`, keeping label management in trusted host
code. (c) Generate a scoped policy with udica if categories prove too coarse.

**Resource limits (2.3).** (a) Document and preflight-require cgroups v2 with
`Delegate=cpu memory` on the user's systemd slice — the doctor script is the
natural home for the check and the `--fix`. (b) Make the unknown-state
fallback fail closed (`False, False`) behind a flag for deployments that
prefer refusal over unbounded runs. (c) For hosts stuck on v1, wrap trials in
a systemd transient scope (`systemd-run --user --scope -p MemoryMax=…`) as an
engine-external ceiling; coarser than per-container cgroups but real.

**Artifact ownership (2.4).** (a) Run a `podman unshare chown -R 0:0` recovery
pass over artifact directories in `prepare_logs_for_host()` — host-side,
cheap, and closes the gap for non-root agents without touching in-container
state. (b) Alternatively `podman cp` artifacts out instead of relying on the
bind view, since `cp` writes as the invoking user.

**Exec fidelity (2.5).** Inject `-T` into programmatic execs exactly as
`GVisorPodmanEnvironment._run_docker_compose_command` does — the change is
three lines, interactive `attach` keeps its TTY, and transcript bytes become
pipe-clean. Worth doing here independently of gVisor.
