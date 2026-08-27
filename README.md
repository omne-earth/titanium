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

There are two ways to run trials. They differ in **who the trial executes as**.

**As your user** — for iterating on trusted tasks:

```bash
# one task
titanium run -p path/to/task --agent claude-code --env gvisor-podman

# a dataset, sampled
titanium run -p path/to/dataset --n-tasks 10 --sample-seed 0
```

**As the `titanium` runner user** — for agents or tasks you do not trust. The entire run (titanium, builds, containers) executes as the throwaway runner. A sandbox escape lands in an account that owns nothing but trial state — not your keys or source.

1. Provision the runner once: `make bootstrap` above already did it. On its own it is `bash scripts/init/titanium.sh`.
2. Run through the shim — prefix your exact command with `scripts/titanium-run.sh`:

   ```bash
   bash scripts/titanium-run.sh .venv/bin/titanium run -p path/to/task --env gvisor-podman
   ```

   Every `make` target (`make titanium-run`, `make smoke-*`, `make bench-*`) applies the shim automatically on a provisioned host. To opt out for one invocation, pass an empty runner: `make titanium-run RUNNER=`.
3. Inspect runner-owned state through make. The runner's containers and images live in *its* storage, so your own `podman ps` shows nothing. Use `make podman-ps ARGS=--all`, `make podman-images`, or `make podman-logs ARGS=<container>`.

Separation applies to the podman-family environments (`podman`, `gvisor-podman`, `krun-podman`) only. The docker-daemon environments are never wrapped: joining the runner to the root-equivalent `docker` group would nullify the separation.

Trials land in `jobs/<timestamp-or-name>/<trial-id>/` (`.run/jobs/…` for make targets). See `titanium run --help`, plus `titanium job`, `titanium view`, and `titanium critique`.

## Reset and maintenance

To return a host to a clean slate — before a from-scratch validation, after changing `runtime.env`, or when a host is leaving titanium duty:

```bash
make reset      # undo `make init` transitively; verify the clean slate
```

`reset` first runs `make collect`, which moves all artifact dot-folders (`.run`, `.tasks`, …) into `.archive/<timestamp>/` — nothing is deleted. It then deprovisions the host (runner user, both sandbox runtimes' registrations and digest pins, the runsc binaries), cleans the checkout back to fresh-clone equivalence, and asserts the result. It keeps `.secrets`, `.archive`, your tracked edits, distro packages, and the operator's docker-group grant. `make collect` also works on its own, to shelve a finished campaign. `make clean` drops repo-local caches only.

The recommended validation cycle for a runtime change is: `make reset` → `make bootstrap` → `make smoke-env`.

If the host also runs libvirt guests, the Docker daemon breaks their network forwarding. Diagnose with `make doctor-libvirt`; repair with `make doctor-libvirt ARGS=--fix`. The fix is atomic: running guests stay attached across the network restart.

## Environments

Every environment installs agents, honors per-task network allowlists, and runs air-gapped (`allow_internet = false`) tasks. They differ in *how strongly the workload is isolated from the host*. Select by threat model, not preference.

| | `docker` | `podman` | `gvisor` | `gvisor-podman` | `krun-podman` |
|---|---|---|---|---|---|
| **Isolation** | namespaces + seccomp | namespaces + seccomp | gVisor (Sentry) kernel | gVisor (Sentry) kernel | KVM microVM (libkrun) |
| **Engine** | Docker daemon | rootless Podman, no socket | Docker daemon | rootless Podman, no socket | rootless Podman, no socket |
| **Runtime** | runc | crun | runsc | runsc | krun |
| **A container escape lands as** | root | unprivileged user | host-side runsc processes, behind Sentry | unprivileged user, behind Sentry | unprivileged user, outside the VM |
| **Root daemon in the trust chain** | yes | no | yes | no | no |
| **Runner separation** (run as a throwaway user) | — | ✓ | — | ✓ | ✓ |
| **Air-gapped image supply** (vendor / restore) | — | ✓ | — | ✓ | ✓ |

`gvisor-podman` is the default and the most battle-tested: a gVisor kernel over rootless Podman with no engine socket. On a provisioned host the entire run executes as the dedicated `titanium` user, so even a full sandbox escape never reaches your keys or source. `docker`/`gvisor` remain the compatibility path and gVisor's most polished host. `krun-podman` is the validated alternative with a different boundary: each container runs in a KVM microVM with a real guest kernel, a confined SELinux domain, a tightened seccomp profile on the VMM, and no host command channel into the running guest (the runtime has no exec; commands ride a measured file protocol, so the flavor is batch-only). The trade is explicit — stronger against kernel-syscall escapes, in exchange for the host's KVM subsystem in the trust chain. Choose by threat model; the probe record is [docs/environments/KRUN-PODMAN.md](docs/environments/KRUN-PODMAN.md). A further environment, `modal`, runs the same task off-host on [Modal](https://modal.com) — a third-party cloud provider, not affiliated with Titanium — for cloud fan-out or GPUs.

## Trust chain

Isolation is only as good as its trust chain, so Titanium verifies rather than assumes:

- The sandbox runtime is confirmed from the host before any code runs.
- `runsc` is version-pinned in `runtime.env` and digest-pinned at install; `krun` is version-floored, rpm-witnessed, and digest-pinned the same way. Preflight fails closed if either binary changed.
- Declared resource limits are read back from the kernel. Tasks that require limits fail loudly when the kernel does not enforce them.
- Image references are fully qualified and built from source, never pulled from mutable third-party tags.

**The boundary** is containment of untrusted agent code plus privilege separation. For the gvisor family it is a *shared kernel* behind a gVisor application kernel — not a VM boundary. For `krun-podman` it is exactly a hypervisor boundary: a KVM microVM per container. Neither is formally verified. The per-environment protections, the relaxations made to run trials, the blast radius of each, and the avenues still open are documented in [`docs/environments/`](docs/environments/).

## License

Apache-2.0. Titanium is derived from [Pier](https://github.com/datacurve-ai/pier), which is derived from [Harbor](https://github.com/harbor-framework/harbor); see [`NOTICE`](NOTICE).

---

<sub>Made with Claude</sub>
