# Titanium

Run coding agents against benchmark tasks inside real sandboxes, and keep the full trajectory of every run for analysis.

Titanium builds each task's environment, installs the agent, runs it under the isolation level you choose, verifies the result, and records everything under `jobs/<name>/<trial>/`. The point of difference is the sandbox: you pick how much isolation a run gets, from a plain container up to a gVisor kernel with the whole run privilege-separated from your account.

## Install

Titanium is not published to a package index; install it from source.

```bash
git clone git@github.com:omne-earth/titanium.git
cd titanium
uv sync                    # builds the venv and the `titanium` binary
uv run titanium --help     # or: uv tool install .   to put it on your PATH
```

## Provision the host

On a fresh Fedora host:

```bash
bash scripts/init/bootstrap.sh
```

Idempotent, and safe to re-run after a partial failure. It provisions, in order: rootless Podman; a digest-pinned `runsc` registered with both engines; the dedicated `titanium` runner user (`scripts/init/titanium.sh` — a nologin account with its own subuid range and container storage, used for privilege separation below); and Docker, only needed for the `docker`/`gvisor` environments.

Then prove the whole chain end to end:

```bash
cp .secrets.template .secrets   # fill in OPENROUTER_MODEL and OPENROUTER_API_KEY
make smoke-env                  # runs podman, gvisor, and gvisor-podman smokes
```

`make BACKEND=claude smoke-env` uses the `claude-code` agent instead of `mini-swe-agent`; it authenticates with `ANTHROPIC_API_KEY` from your environment (or `.secrets`) rather than the OpenRouter pair.

## Run

There are two ways to run trials, differing in **who the trial executes as**.

**As your user** — fine for iterating on trusted tasks:

```bash
# one task
titanium run -p path/to/task --agent claude-code --env gvisor-podman

# a dataset, sampled
titanium run -p path/to/dataset --n-tasks 10 --sample-seed 0
```

**As the `titanium` runner user** — for agents or tasks you don't trust. The entire run (titanium, builds, containers) executes as the throwaway runner, so even a sandbox escape lands in an account that owns nothing but trial state — not your keys or source. Step by step:

1. Provision the runner (once): `make init` above already did it; on its own it's `bash scripts/init/titanium.sh`.
2. Run through the shim — prefix your exact command with `scripts/titanium-run.sh`:

   ```bash
   bash scripts/titanium-run.sh .venv/bin/titanium run -p path/to/task --env gvisor-podman
   ```

   Every `make` target (`make titanium-run`, `make smoke-*`, `make bench-*`) applies the shim automatically on a provisioned host — you never type it when driving runs through make. To opt out for one invocation, pass an empty runner: `make titanium-run RUNNER=`.
3. Inspect runner-owned state through make: the runner's containers and images live in *its* storage, so your own `podman ps` shows nothing. Use `make podman-ps ARGS=--all`, `make podman-images`, or `make podman-logs ARGS=<container>`.

Separation applies to the podman-family environments (`podman`, `gvisor-podman`) only; the docker-daemon environments are never wrapped, because joining the runner to the root-equivalent `docker` group would nullify the separation.

Trials land in `jobs/<timestamp-or-name>/<trial-id>/` (`.run/jobs/…` for make targets). See `titanium run --help`, plus `titanium job`, `titanium view`, and `titanium critique` for the rest.

## Environments

Every environment installs agents, honors per-task network allowlists, and runs air-gapped (`allow_internet = false`) tasks. They differ in *how strongly the workload is isolated from the host* — pick by threat model, not preference.

| | `docker` | `podman` | `gvisor` | `gvisor-podman` |
|---|---|---|---|---|
| **Isolation** | namespaces + seccomp | namespaces + seccomp | gVisor (Sentry) kernel | gVisor (Sentry) kernel |
| **Engine** | Docker daemon | rootless Podman, no socket | Docker daemon | rootless Podman, no socket |
| **Runtime** | runc | crun | runsc | runsc |
| **A container escape lands as** | root | unprivileged user | host-side runsc processes, behind Sentry | unprivileged user, behind Sentry |
| **Root daemon in the trust chain** | yes | no | yes | no |
| **Runner separation** (run as a throwaway user) | — | ✓ | — | ✓ |
| **Air-gapped image supply** (vendor / restore) | — | ✓ | — | ✓ |

`gvisor-podman` is the default and the strongest: a gVisor kernel over rootless Podman with no engine socket, and — once provisioned — the entire run executes as a dedicated `titanium` user that owns nothing but trial state, so even a full sandbox escape never reaches your keys or source. `docker`/`gvisor` remain the compatibility path and gVisor's most polished host. A fifth environment, `modal`, runs the same task off-host on [Modal](https://modal.com) — a third-party cloud provider, not affiliated with Titanium — when you want to fan trials out across cloud workers or reach GPUs.

Isolation is only as good as its trust chain, so Titanium verifies rather than assumes: the sandbox runtime is confirmed from the host before any code runs, `runsc` is pinned by digest, declared resource limits are read back from the kernel, and image references are fully qualified and built from source rather than pulled from mutable third-party tags.

**The boundary** is containment of untrusted agent code and privilege separation on a *shared kernel* — a gVisor application kernel plus rootless, throwaway-user execution — not a VM or hypervisor isolation boundary, and not a formally verified one. The per-environment protections, the relaxations made to run trials, the blast radius of each, and the avenues still open are documented in [`docs/environments/`](docs/environments/).

## License

Apache-2.0. Titanium is derived from [Pier](https://github.com/datacurve-ai/pier), which is derived from [Harbor](https://github.com/harbor-framework/harbor); see [`NOTICE`](NOTICE).
