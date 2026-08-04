"""gVisor (runsc) support for :class:`~pier.environments.docker.docker.DockerEnvironment`.

Opt-in via ``--ek gvisor=true``. When enabled, only the untrusted ``main``
service runs under the configured gVisor runtime; every other service --
notably the trusted egress proxy -- keeps Docker's default runtime, and
Docker's own default runtime is never changed.

Two gVisor properties drive everything here:

* **The root filesystem is sandbox-private.** ``runsc`` defaults to
  ``--file-access=exclusive`` with ``--overlay2=root:self``, so writes to the
  container's root filesystem live in an overlay the host cannot see, and
  external writes into it are not observed by the sandbox. ``docker compose
  cp`` is therefore unusable in *both* directions against a running gVisor
  container: copying in is stale/invisible, and copying out reports that the
  file does not exist. Transfers instead go through the scoped staging bind
  mounts declared below -- bind mounts default to ``--file-access-mounts=shared``
  and stay coherent both ways.

* **Docker's embedded DNS at 127.0.0.11 is unreachable.** The sandbox has its
  own network stack, so it cannot reach a resolver bound to the host network
  namespace's loopback (google/gvisor#7469, closed-as-designed in #115). The
  first slice therefore requires ``allow_internet = false``, and resolves the
  egress proxy to a literal IPv4 address after startup rather than relying on
  Compose service-name resolution.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

# The runtime name registered with the Docker daemon (``sudo runsc install``
# writes this into /etc/docker/daemon.json).
GVISOR_DEFAULT_RUNTIME = "runsc"

GVISOR_COMPOSE_NAME = "docker-compose-gvisor.json"

# Per-trial staging root on the host, under the trial directory so that
# concurrent trials can never share a path.
GVISOR_STAGE_DIR_NAME = ".gvisor-stage"

# Staging mount points inside the container.
GVISOR_STAGE_ROOT = PurePosixPath("/.pier-stage")
GVISOR_STAGE_IN = GVISOR_STAGE_ROOT / "in"
GVISOR_STAGE_OUT = GVISOR_STAGE_ROOT / "out"

# ``label=disable`` is deliberately absent. A production-shaped runsc container
# carrying both staging bind mounts starts and works on an SELinux-enforcing
# host without it: Docker sees runsc advertise ``selinux: false`` in its OCI
# runtime features and assigns no process label at all, so there is no label to
# disable.
GVISOR_SECURITY_OPT: list[str] = ["no-new-privileges:true"]

# `docker compose ps -q` prints one container ID per line. Pier merges compose
# stderr into stdout, so lines are filtered rather than taken wholesale.
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


def gvisor_stage_dirs(trial_dir: Path | str) -> tuple[Path, Path]:
    """Return the host ``(stage_in, stage_out)`` directories for a trial."""
    root = Path(trial_dir) / GVISOR_STAGE_DIR_NAME
    return root / "in", root / "out"


def write_gvisor_compose_file(
    path: Path,
    *,
    runtime: str,
    stage_in: Path,
    stage_out: Path,
) -> Path:
    """Write the Compose override that puts ``main`` under *runtime*.

    Appended last so its scalars win: Compose resolves scalar keys as
    last-writer-wins, so an override placed before a task's own compose file
    could have its ``runtime`` flipped back to ``runc``.
    """
    compose = {
        "services": {
            "main": {
                "runtime": runtime,
                "security_opt": list(GVISOR_SECURITY_OPT),
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(Path(stage_in).resolve()),
                        "target": str(GVISOR_STAGE_IN),
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(Path(stage_out).resolve()),
                        "target": str(GVISOR_STAGE_OUT),
                    },
                ],
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path


def docker_runtimes(timeout_sec: int = 10) -> set[str] | None:
    """Return the runtime names the Docker daemon knows, or None on error."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return set(data)


def assert_runtime_registered(runtime: str) -> None:
    """Fail closed unless *runtime* is registered with the Docker daemon.

    Checking the daemon is authoritative; probing ``PATH`` for a ``runsc``
    binary is neither necessary nor sufficient, because the daemon resolves the
    runtime's path from its own configuration.
    """
    runtimes = docker_runtimes()
    if runtimes is None:
        raise RuntimeError(
            "gVisor mode could not query the Docker daemon for its registered "
            "runtimes ('docker info --format \"{{json .Runtimes}}\"' failed), so "
            f"it cannot confirm that {runtime!r} is available. Refusing to "
            "continue rather than silently falling back to runc."
        )
    if runtime not in runtimes:
        available = ", ".join(sorted(runtimes)) or "none"
        raise RuntimeError(
            f"gVisor mode requires the {runtime!r} runtime to be registered with "
            f"the Docker daemon, but 'docker info' lists: {available}. Register "
            "it (e.g. 'sudo runsc install') and restart Docker, or drop "
            "--ek gvisor=true."
        )


def parse_container_ids(output: str | None) -> list[str]:
    """Extract container IDs from ``docker compose ps -q`` output."""
    if not output:
        return []
    return [
        line.strip()
        for line in output.splitlines()
        if _CONTAINER_ID_RE.match(line.strip())
    ]


async def _docker_inspect(container_id: str, template: str) -> str | None:
    """Run ``docker inspect --format <template>`` and return stdout, or None."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "--format",
            template,
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
    except Exception:
        return None
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip()


async def container_runtime(container_id: str) -> str | None:
    """Return the OCI runtime Docker actually used for *container_id*.

    Host-side and therefore authoritative. In-container evidence (``uname -r``
    reporting a gVisor kernel, ``dmesg``) is a useful smoke test but gVisor's
    own documentation warns it "is easily replicated by an attacker so
    applications should never use dmesg to verify the runtime in a security
    sensitive context".
    """
    return await _docker_inspect(container_id, "{{.HostConfig.Runtime}}")


async def container_networks(container_id: str) -> dict[str, dict]:
    """Return ``{network_name: settings}`` for *container_id*."""
    raw = await _docker_inspect(container_id, "{{json .NetworkSettings.Networks}}")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def shared_network_ipv4(
    peer_networks: dict[str, dict],
    own_networks: dict[str, dict],
) -> str | None:
    """Return *peer*'s IPv4 on a network it shares with *own*.

    Selecting the shared network avoids hard-coding Pier's internal network
    name and answers the question that actually matters: the address at which
    the sandboxed service can reach its peer. Names are sorted so the choice is
    deterministic when more than one network is shared.
    """
    for name in sorted(set(peer_networks) & set(own_networks)):
        address = (peer_networks.get(name) or {}).get("IPAddress")
        if address:
            return address
    return None
