"""Pier's first-party Compose set.

These files are Pier's own, consumed by every compose-driven environment —
Docker, Podman, gVisor on either engine, and the remote Modal/Daytona DinD
flavors — so they live outside the docker package and drop the historical
``docker-compose-`` prefix. The dedicated home exists for a reason beyond
naming: engine-specific concerns that must not leak onto the host (registry
qualification for Podman's short-name resolution, for one) get expressed as
overlays here instead of host-global configuration edits.

Generated per-trial overlays follow the same naming (``compose-mounts.json``,
``compose-resources.json``, ``compose-egress-proxy.json``,
``compose-gvisor.json``), written into the trial directory by the
environments that need them.
"""

import json
from pathlib import Path

from pier.models.trial.config import ServiceVolumeConfig

COMPOSE_DIR = Path(__file__).parent
COMPOSE_BASE_PATH = COMPOSE_DIR / "base.yaml"
COMPOSE_BUILD_PATH = COMPOSE_DIR / "build.yaml"
COMPOSE_PREBUILT_PATH = COMPOSE_DIR / "prebuilt.yaml"
COMPOSE_NO_NETWORK_PATH = COMPOSE_DIR / "no-network.yaml"
COMPOSE_WINDOWS_KEEPALIVE_PATH = COMPOSE_DIR / "windows-keepalive.yaml"
RESOURCES_COMPOSE_NAME = "compose-resources.json"


def write_mounts_compose_file(path: Path, mounts: list[ServiceVolumeConfig]) -> Path:
    compose = {"services": {"main": {"volumes": list(mounts)}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path


def write_resources_compose_file(
    path: Path,
    *,
    cpu_request: int | None = None,
    cpu_limit: int | None = None,
    memory_request_mb: int | None = None,
    memory_limit_mb: int | None = None,
) -> Path:
    resources: dict[str, dict[str, str]] = {}
    limits: dict[str, str] = {}
    reservations: dict[str, str] = {}

    if cpu_limit is not None:
        limits["cpus"] = str(cpu_limit)
    if memory_limit_mb is not None:
        limits["memory"] = f"{memory_limit_mb}M"
    if cpu_request is not None:
        reservations["cpus"] = str(cpu_request)
    if memory_request_mb is not None:
        reservations["memory"] = f"{memory_request_mb}M"

    if limits:
        resources["limits"] = limits
    if reservations:
        resources["reservations"] = reservations

    main = {"deploy": {"resources": resources}} if resources else {}
    compose = {"services": {"main": main}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path
