# Titanium

Run coding agents against benchmark tasks inside real sandboxes, and keep the full trajectory of every run for analysis.

Titanium builds each task's environment, installs the agent, runs it under the isolation level you choose, verifies the result, and records everything under `jobs/<name>/<trial>/`. The point of difference is the sandbox: you pick how much isolation a run gets, from a plain container up to a gVisor kernel with the whole run privilege-separated from your account.

## Install

```bash
uv tool install titanium   # or: pip install titanium
```

## Run

```bash
# one task
titanium run -p path/to/task --agent claude-code --env gvisor-podman

# a dataset, sampled
titanium run -p path/to/dataset --n-tasks 10 --sample-seed 0
```

Trials land in `jobs/<timestamp-or-name>/<trial-id>/`. See `titanium run --help`, plus `titanium job`, `titanium view`, and `titanium critique` for the rest.

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

Isolation is only as good as its trust chain, so Titanium verifies rather than assumes: the sandbox runtime is confirmed from the host before any code runs, `runsc` is pinned by digest, declared resource limits are read back from the kernel, and image references are fully qualified and built from source rather than pulled from mutable third-party tags. The per-environment protections, the relaxations made to run trials, and the avenues still open are documented in [`docs/environments/`](docs/environments/).

## Setup

```bash
make init          # provision the host (engines, runsc, runner user)
make smoke-env     # end-to-end check across podman / gvisor / gvisor-podman
```

## License

Apache-2.0. Titanium is derived from [Pier](https://github.com/datacurve-ai/pier), which is derived from [Harbor](https://github.com/harbor-framework/harbor); see [`NOTICE`](NOTICE).
