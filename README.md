# Titanium

Titanium runs coding agents against benchmark tasks inside real sandboxes. It records the full trajectory of every run.

For each task, Titanium builds the environment, installs the agent, runs it at the isolation level you select, verifies the result, and writes everything to `jobs/<name>/<trial>/`. The difference from other runners is the sandbox: you select the isolation level, from a plain container to a gVisor kernel with the whole run privilege-separated from your account.

## Install

Titanium is not on a package index. Install it from source:

```bash
git clone git@github.com:omne-earth/titanium.git
cd titanium
uv sync                    # builds the venv and the `titanium` binary
uv run titanium --help     # or: uv tool install .   to put it on your PATH
```

## Environments

Every environment installs agents, honors per-task network allowlists, and runs air-gapped (`allow_internet = false`) tasks. They differ in *how strongly the workload is isolated from the host*. Select by threat model, not preference.

- **`docker`** — the Docker daemon with runc; the compatibility baseline.
- **`podman`** — rootless Podman, no socket ([docs/environments/PODMAN.md](docs/environments/PODMAN.md)).
- **`gvisor`** — gVisor (runsc) on the Docker daemon ([docs/environments/GVISOR.md](docs/environments/GVISOR.md)).
- **`gvisor-podman`** — gVisor on rootless Podman; the default ([docs/environments/GVISOR-PODMAN.md](docs/environments/GVISOR-PODMAN.md)).
- **`krun-podman`** — KVM microVMs (krun) on rootless Podman ([docs/environments/KRUN-PODMAN.md](docs/environments/KRUN-PODMAN.md)).
- **`modal`** — the same task off-host on [Modal](https://modal.com), for cloud fan-out or GPUs.

### Birds-Eye View

| | `docker` | `podman` | `gvisor` | `gvisor-podman` | `krun-podman` |
|---|---|---|---|---|---|
| **Isolation** | namespaces + seccomp | namespaces + seccomp | gVisor (Sentry) kernel | gVisor (Sentry) kernel | KVM microVM (libkrun) |
| **Engine** | Docker daemon | rootless Podman, no socket | Docker daemon | rootless Podman, no socket | rootless Podman, no socket |
| **Runtime** | runc | crun | runsc | runsc | krun |
| **Kernel isolation** | none — workload syscalls hit the host kernel, seccomp-filtered | none — workload syscalls hit the host kernel, seccomp-filtered | Sentry, a userspace application kernel, absorbs the workload's syscalls | Sentry, a userspace application kernel, absorbs the workload's syscalls | a dedicated guest kernel (libkrunfw) inside a KVM VM |
| **A container escape lands as** | root | unprivileged user | host-side runsc processes, behind Sentry | unprivileged user, behind Sentry | unprivileged user, outside the VM |
| **Root daemon in the trust chain** | yes | no | yes | no | no |
| **Runner separation** (run as a throwaway user) | — | the `titanium` user, via `make titanium-run` | — | the `titanium` user, via `make titanium-run` | the `titanium` user, via `make titanium-run` |
| **Overhead** | near-native | near-native; rootless image builds cost more — pre-load images | syscall interposition tax — syscall- and I/O-heavy workloads pay most | gvisor's tax, plus a user-mode network hop (pasta) at the host edge; pre-load images | VM boot per container, virtiofs I/O, and each guest kernel's memory footprint; pre-load images |
| **Pre-load images** | — | `images-vendor` / `images-restore` | — | `images-vendor` / `images-restore` | `images-vendor` / `images-restore` |

### Network

Network policy is enforced by a **per-trial egress proxy**, not by trust: an allowlist task puts the sandbox on an `internal` network whose only route out is a Squid proxy built fresh for that trial (Alpine-based, per-trial auth token, the task's domain allowlist compiled in). The sandbox reaches it by literal IP and never needs DNS; the proxy is health-gated before the agent starts, runs outside the sandbox runtime, and is verified there by the same host-side checks that verify the sandbox. Air-gapped tasks get `network_mode: none` outright — the proxy exists only when an allowlist grants egress.

> **Note:** the proxy exists for one situation: an air-gapped task (`allow_internet = false`) run by an agent that needs the network to install itself and call its model. The agent's allowlist becomes the proxy's domain list — the task sees no internet, the agent reaches only its own endpoints. Tasks with `allow_internet = true` get direct egress and no proxy. The trial directory records which happened: proxied trials contain a `compose-egress-proxy.json`, unrestricted trials do not.

### gvisor-podman — the default

The most battle-tested option: a gVisor kernel over rootless Podman with no engine socket. On a provisioned host the entire run executes as the dedicated `titanium` user, so even a full sandbox escape never reaches your keys or source.

### krun-podman

The validated alternative with a different boundary: each container runs in a KVM microVM with a real guest kernel, a confined SELinux domain, a tightened seccomp profile on the VMM, and no host command channel into the running guest (the runtime has no exec; commands ride a measured file protocol, so the flavor is batch-only). The trade is explicit — stronger against kernel-syscall escapes, in exchange for the host's KVM subsystem in the trust chain. Choose by threat model; the probe record is [docs/environments/KRUN-PODMAN.md](docs/environments/KRUN-PODMAN.md).

### docker and gvisor

`docker` is the compatibility path. It gives stock Docker semantics to tasks that assume them. It makes no isolation claims beyond Docker's own. `gvisor` is runsc registered as a Docker runtime. That is the pairing gVisor upstream ships and tests hardest. Both environments are clients of `dockerd`. `dockerd` is one persistent daemon, owned by root, one instance per host. The unprivileged `titanium` runner user cannot own a root daemon. So these two environments do not use the per-run account. They talk to the host's shared daemon. Its socket is root-equivalent. Its image cache and networks persist across runs. Choose these environments for compatibility. The clean-slate and least-privilege guarantees belong to the podman family.

### modal

Runs the same task off-host on [Modal](https://modal.com) — a third-party cloud provider, not affiliated with Titanium — for cloud fan-out or GPUs.

## Provision the host

On a fresh Fedora host:

```bash
make bootstrap    # installs make and podman, then chains into `make init`
```

If `make` itself is missing, run `bash scripts/init/bootstrap.sh` instead. Both are idempotent. Re-run them safely after a partial failure. They provision, in order: rootless Podman; a digest-pinned `runsc` registered with both engines; a digest-pinned `krun` (KVM microVM runtime, dnf-installed, rpm-witnessed) for the `krun-podman` environment; the dedicated `titanium` runner user (a nologin account with its own subuid range and container storage); and Docker, needed only for the `docker`/`gvisor` environments.

Runtime versions come from `runtime.env`, checked in at the repo root. It pins the gVisor release and floors podman-compose and krun, so two hosts provisioned from the same commit get the same runtime.

Then prove the whole chain end to end:

```bash
cp .secrets.template .secrets   # fill in OPENROUTER_MODEL and OPENROUTER_API_KEY
make smoke-env                  # runs the podman, gvisor, gvisor-podman, and krun-podman smokes
```

`make BACKEND=claude smoke-env` uses the `claude-code` agent instead of `mini-swe-agent`. It authenticates with `ANTHROPIC_API_KEY` from your environment or `.secrets`.

## Run

How you invoke decides who runs.

### Through make — as the `titanium` runner

On a provisioned host, the podman-family make targets (`make titanium-run`, `make smoke-*`, `make bench-*` with `podman`, `gvisor-podman`, or `krun-podman`) run the whole invocation — titanium, builds, containers — as the throwaway `titanium` user. A sandbox escape lands in an account that owns nothing but trial state, not your keys or source. Use this path for agents or tasks you do not trust.

```bash
make titanium-run TITANIUM_ENV=krun-podman TITANIUM_TASK=path/to/task
```

- The docker-family targets never wrap: the runner must never join the root-equivalent `docker` group.
- Opt out for one invocation: `make titanium-run RUNNER=`.
- Wrap a hand-built command by prefixing the shim: `bash scripts/titanium-run.sh .venv/bin/titanium run -p path/to/task --env gvisor-podman`.
- Inspect runner-owned state through make — the runner's containers and images live in *its* storage, so your own `podman ps` shows nothing. `make podman-<verb> [ARGS=…]` proxies any podman subcommand into the runner's context:

  ```bash
  make podman-ps ARGS=--all          # runner's containers
  make podman-images                 # runner's images
  make podman-logs ARGS=<container>  # a runner container's logs
  make podman-inspect ARGS=<id>      # any other verb forwards the same way
  ```

  The proxy forwards the verb; whether it succeeds is the runtime's call — `podman exec` reaches crun and runsc containers, never krun ones (the handler has no exec; see [docs/environments/KRUN-PODMAN.md](docs/environments/KRUN-PODMAN.md) §5).

### With the CLI — as your user

For iterating on trusted tasks. State the environment explicitly: the CLI default is `docker`, not the make default.

```bash
# one task
titanium run -p path/to/task --agent claude-code --env gvisor-podman

# a dataset, sampled
titanium run -p path/to/dataset --env gvisor-podman --n-tasks 10 --sample-seed 0
```

Trials land in `jobs/<timestamp-or-name>/<trial-id>/` (`.run/jobs/…` for make targets). See `titanium run --help`, plus `titanium job`, `titanium view`, and `titanium critique`.

## Reset and maintenance

To return a host to a clean slate — before a from-scratch validation, after changing `runtime.env`, or when a host is leaving titanium duty:

```bash
make reset      # undo `make init` transitively; verify the clean slate
make collect    # archive artifacts only; shelve a campaign without deprovisioning
```

`reset` first runs `make collect`, which archives instead of deleting: every *untracked* repo-local dot-folder that holds artifacts moves into one timestamped folder, names preserved — a previous campaign's results are always recoverable from there.

```bash
.archive/2026-08-27__00-08-33/
├── .run/            # jobs, trial output, reports, staged smoke tasks
├── .tasks/          # the cloned datasets
└── .pytest_cache/   # any other untracked artifact dot-folder
```

Not collected: `.git/` and `.archive/` (structural), `.venv/` (rebuilt byte-equivalent by `make sync`), `.secrets`, and any dot-folder holding tracked files (`.github/` is source, not artifact). It then deprovisions the host (runner user, both sandbox runtimes' registrations and digest pins, the runsc binaries), cleans the checkout back to fresh-clone equivalence, and asserts the result. It keeps `.secrets`, `.archive`, your tracked edits, distro packages, and the operator's docker-group grant. `make collect` also works on its own, to shelve a finished campaign. `make clean` drops repo-local caches only.

The recommended validation cycle for a runtime change is: `make reset` → `make bootstrap` → `make smoke-env`.

### Image supply: vendor and restore

Two targets, one archive.

**Vendor** collects every image reference a task set can reach — Dockerfile FROMs (qualified exactly as build preparation qualifies them), image-only tasks' prebuilts, and the egress-proxy base — into one `podman save` archive:

```bash
make images-vendor                          # examples/smoke -> .run/images.tar
make images-vendor IMAGES_TASKS=path/to/tasks IMAGES_ARCHIVE=path/to/images.tar
```

**Restore** loads that archive into the runner's image storage:

```bash
make images-restore                         # from .run/images.tar
make images-restore IMAGES_ARCHIVE=.archive/<timestamp>/.run/images.tar   # from a collected campaign
```

Two uses:

- **Air-gapped hosts**: vendor on a connected host, restore where nothing can be pulled.
- **Cache warming after a reset**: `reset` deletes the runner user with its home, and the runner's entire image store lives there — every podman-family run after a reset starts from a cold cache (the Docker daemon's root-owned cache survives, which is why the docker-driven environments restart faster). `make images-restore` right after `make init` puts the podman family back on warm starts.

One interaction to know: `collect` sweeps `.run/` into `.archive/<timestamp>/`, so an archive vendored to the default path survives a reset but moves — restore it from the archive path above, or vendor to a path outside `.run` to begin with.

If the host also runs libvirt guests, the Docker daemon breaks their network forwarding. Diagnose with `make doctor-libvirt`; repair with `make doctor-libvirt ARGS=--fix`. The fix is atomic: running guests stay attached across the network restart.

## Task sizing

A task declares its resources in `task.toml` (`[environment] cpus`, `memory_mb`); trial-level overrides fold into the same values. Titanium writes them as compose `deploy.resources.limits` on `main`, and the engine turns them into cgroup limits. Declared limits are never taken on faith: the podman-family environments read `cpu.max` and `memory.max` back from the kernel after start, fail `LIMIT`/`GUARANTEE` tasks whose limits did not materialize, and log the gap for `AUTO` tasks.

| | Enforcement | What the workload sees | Enforced | Notes |
|---|---|---|---|---|
| `docker` | The root daemon owns the cgroup tree. | All host cores. The cgroup throttles usage. | Always. | — |
| `podman` | Rootless cgroups v2. `make init` provisions the drop-in that delegates the cpu and memory controllers. | All host cores. The cgroup throttles usage. | Conditional. On cgroups v1, or without delegation, the engine drops the limits and reports no error. | The post-start read-back detects the dropped limits. See [PODMAN.md](docs/environments/PODMAN.md) §2.3. |
| `gvisor` | Engine-side cgroups, the same as `docker`. | All host cores. The cgroup throttles usage. | Always. | The Sentry takes no part in sizing. |
| `gvisor-podman` | Engine cgroups only. The `-ignore-cgroups` wrapper registers rootless runsc. runsc creates no cgroups itself. | All host cores. The cgroup throttles usage. | Conditional, the same as `podman`. | The post-start read-back detects the dropped limits. See [GVISOR-PODMAN.md](docs/environments/GVISOR-PODMAN.md) §2.6. |
| `krun-podman` | Engine cgroups, plus guest sizing. The compose override emits `krun.cpus` and `krun.ram_mib` from the declared values. | Exactly the declared cores and RAM. The guest agrees with the cgroup. | Conditional for the cgroup, the same as `podman`. The guest size always applies. | The microVM is sized, not only throttled. See below, and [KRUN-PODMAN.md](docs/environments/KRUN-PODMAN.md) §2.8. |

Why krun needs the extra step: vCPU count and RAM are properties of the guest, visible to everything inside it, and a cgroup quota alone gets this wrong — left to itself, the handler gives the guest the host's cores and sizes RAM from the OCI memory limit as a side effect, so a task declaring one CPU gets a guest that *reports* sixteen while the cgroup lets it *use* one, and every thread pool sized by core count oversubscribes. The annotations make the guest agree with the declaration. One shared envelope remains: guest RAM, VMM overhead, and the virtiofs DAX window all count against the same cgroup `memory.max`.

## Development and testing

### Developer workflow

The workflow is make targets, end to end:

```bash
make unit-all             # unit suites (podman, krun, factory, gvisor)
make _probe-krun-podman   # evidence probes for the krun probe record
make smoke-podman         # one environment, three trials
make smoke-env            # all four environments, one tmux session
make run-attach           # watch a running smoke session
make reset                # deprovision; verify the clean slate
```

For a runtime change, run the full cycle: `make reset` → `make init` → `make smoke-env` → inspect the job results under `.run/jobs/`. Unit tests gate every smoke target, and the podman-family smokes run as the `titanium` runner automatically.

### Oracle runs

The `oracle` agent replays a task's own `solution/` instead of calling a model. An oracle run is deterministic, costs nothing, and finishes in minutes — it validates the environment, the transfers, the verifier, and the artifact collection without an LLM in the loop:

```bash
make titanium-run TITANIUM_AGENT=oracle TITANIUM_ENV=krun-podman \
  TITANIUM_TASK=examples/smoke/verify-krun-podman-env-www
```

A reward of 1.0 proves the wiring; a failure implicates the environment or the task, never the model.

#### Titanium Oracles

Each environment ships a verification task under `examples/smoke/`:

- `verify-podman-env`
- `verify-gvisor-env`
- `verify-gvisor-podman-env`
- `verify-krun-podman-env-www`
- `verify-krun-podman-env-airgapped`

These tasks exist for one reason: an environment must prove its own claims. Each solution probes the boundary from inside — egress, DNS, engine sockets, write limits, and the runtime's identity signals — and writes a report the verifier re-checks independently. An oracle run of one of these tasks is therefore the cheapest full proof an environment has: deterministic, model-free, and scored against what the sandbox actually did, not what the docs say it does. In-guest evidence corroborates; the host-side runtime gate stays authoritative. When an environment changes, run its oracle first. These oracles are by no means complete nor exhaustive but provide a bootstrap example to begin with.


## Trust chain

Isolation is only as good as its trust chain, so Titanium verifies rather than assumes:

- **The runtime is proven, not presumed.** The host confirms the sandbox runtime (`{{.OCIRuntime}}` / the daemon registry) before any command may run, records the verified identity in the trial's `runtime-verification.json`, and never accepts in-sandbox evidence — a guest can fake `uname` and `dmesg`, so nothing produced inside the sandbox gates anything.
- **The runtime binaries are pinned.** `runsc` is version-pinned in `runtime.env` and digest-pinned at install; `krun` is version-floored, rpm-witnessed at first use, and digest-pinned the same way. Both register in root-owned `containers.conf.d`. Preflight fails closed if either binary changed.
- **Limits are read back.** Declared cpu and memory limits are re-read from the kernel after start. Tasks that require limits fail loudly when the kernel does not enforce them.
- **Images are pinned to source.** References are fully qualified and built from local Dockerfiles, never pulled from mutable third-party tags; `short-name-mode=enforcing` stays untouched.
- **Network policy is topology.** Egress rides the per-trial proxy on an `internal` network, or does not exist at all — never a firewall rule the workload could race.
- **Escapes land in a throwaway.** On a provisioned host the whole run executes as the nologin `titanium` user, and the krun VMM additionally runs under a tightened seccomp profile and a confined SELinux domain.
- **Teardown is fail-closed.** Cleanup discovers resources by exact project label and refuses to report clean while any remain.
- **The proof re-runs.** `make smoke-env` and the per-environment oracles re-verify the whole chain on demand; the per-environment documents record every relaxation, its blast radius, and the measurements behind it.

**The boundary** is containment of untrusted agent code plus privilege separation. For the gvisor family it is a *shared kernel* behind a gVisor application kernel — not a VM boundary. For `krun-podman` it is exactly a hypervisor boundary: a KVM microVM per container, at the price of the host's KVM subsystem in the trust chain. Neither is formally verified. The per-environment protections, the relaxations made to run trials, the blast radius of each, and the avenues still open are documented in [`docs/environments/`](docs/environments/).

## License

Apache-2.0. Titanium is derived from [Pier](https://github.com/datacurve-ai/pier), which is derived from [Harbor](https://github.com/harbor-framework/harbor); see [`NOTICE`](NOTICE).

---

<sub>Made by Omne with Claude</sub>
