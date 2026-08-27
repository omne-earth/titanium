"""Docker-specific runsc constants, Compose override, and host-side probes.

Everything here is deliberately Docker-specific except where a helper is
genuinely engine-neutral, in which case the CLI binary name is threaded through
explicitly rather than hidden behind an abstraction. The places where Podman's
schema or conventions differ (inspect fields, label namespaces, runtime
resolution) live in :mod:`titanium.environments.gvisor.podman_runtime`; a
speculative container-engine framework spanning both would be dead weight.

Two gVisor properties drive the design:

* **The root filesystem is sandbox-private.** ``runsc`` defaults to
  ``--file-access=exclusive`` with ``--overlay2=root:self``, so writes to the
  container's root filesystem live in an overlay the host cannot see, and
  external writes into it are not observed by the sandbox. ``docker compose cp``
  is therefore unusable in *both* directions against a running gVisor container.
  Transfers instead go through the scoped staging bind mounts declared below --
  bind mounts default to ``--file-access-mounts=shared`` and stay coherent both
  ways.

* **Docker's embedded DNS at 127.0.0.11 is unreachable.** The sandbox has its own
  network stack and does not inherit the container netns' netfilter rules, so a
  resolver reached through DNAT to loopback cannot be used
  (google/gvisor#7469, still open). Allowlist mode sidesteps this entirely --
  the trusted proxy resolves names, so the sandbox never needs DNS -- and the
  proxy is addressed by literal IPv4. Unrestricted-internet mode is handled by
  :mod:`titanium.environments.gvisor.network`.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

# The runtime name registered with the Docker daemon (``sudo runsc install``
# writes this into /etc/docker/daemon.json).
DEFAULT_RUNTIME = "runsc"

COMPOSE_OVERRIDE_NAME = "compose-runtime.json"

# Per-trial staging root on the host, under the trial directory so that
# concurrent trials can never share a path.
STAGE_DIR_NAME = ".titanium-stage"

# Staging mount points inside the container.
STAGE_ROOT = PurePosixPath("/.titanium-stage")
STAGE_IN = STAGE_ROOT / "in"
STAGE_OUT = STAGE_ROOT / "out"

# ``label=disable`` is deliberately absent from the shared baseline. A
# production-shaped runsc container carrying both staging bind mounts starts
# and works on an SELinux-enforcing host without it under Docker: Docker sees
# runsc advertise ``selinux: false`` in its OCI runtime features and assigns no
# process label at all, so there is no label to disable, and adding one would
# weaken confinement for no benefit. Podman performs no such feature check and
# labels the process unconditionally, which runsc rejects ("SELinux is not
# supported"), so the podman flavor opts in via
# ``write_compose_override(disable_process_label=True)``.
SECURITY_OPT: list[str] = ["no-new-privileges:true"]

# Engine name -> CLI binary. Both engines are implemented, but each is driven
# by its own environment class (`--env gvisor` for Docker, `--env gvisor-podman`
# for Podman): the two differ in compose provider, inspect schema and label
# conventions, so an engine kwarg alone cannot retarget an environment. Which
# engines a given environment class actually drives is expressed through the
# ``supported`` argument of :func:`resolve_engine_cli`.
_ENGINE_CLIS: dict[str, str] = {"docker": "docker", "podman": "podman"}

# Known engine -> the --env selector that drives it. Used to redirect a caller
# who asked a gVisor environment for an engine that a *different* gVisor
# environment implements.
_ENGINE_SELECTORS: dict[str, str] = {
    "docker": "--env gvisor",
    "podman": "--env gvisor-podman",
}

# `docker compose ps -q` prints one container ID per line. Titanium merges compose
# stderr into stdout, so lines are filtered rather than taken wholesale.
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

# The label Docker Compose stamps on every container and network it creates,
# holding the exact `--project-name` used. Filtering on it -- rather than on a
# name prefix or a guess -- is what lets fallback cleanup remove only this
# environment's own resources and never another project's.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def resolve_engine_cli(engine: str, supported: tuple[str, ...] = ("docker",)) -> str:
    """Return the CLI binary for *engine*, or fail immediately and clearly.

    Called before ``super().__init__()`` so an unsupported engine is rejected at
    construction, long before anything is built or started. There is deliberately
    no fallback: silently running a "podman" request on Docker (or vice versa)
    would hand back a sandbox driven by a different engine than the caller
    asked for.

    *supported* names the engines the calling environment class actually
    drives. A known engine outside that set is not an error in the request but
    in the selector, so the failure message redirects to the ``--env`` value
    that does drive it rather than pretending the engine does not exist.
    """
    name = str(engine).strip().lower()
    cli = _ENGINE_CLIS.get(name)
    if cli is None:
        known = ", ".join(sorted(_ENGINE_CLIS))
        raise ValueError(
            f"Unknown container engine {engine!r} for the gVisor environment. "
            f"Supported engines: {known}."
        )

    if name not in supported:
        selector = _ENGINE_SELECTORS[name]
        raise ValueError(
            f"This gVisor environment does not drive the {engine!r} container "
            f"engine; select it with {selector} instead. Refusing to continue "
            "rather than silently driving a different engine than the one "
            "asked for."
        )

    return cli


def stage_dirs(trial_dir: Path | str) -> tuple[Path, Path]:
    """Return the host ``(stage_in, stage_out)`` directories for a trial."""
    root = Path(trial_dir) / STAGE_DIR_NAME
    return root / "in", root / "out"


def write_compose_override(
    path: Path,
    *,
    runtime: str,
    stage_in: Path,
    stage_out: Path,
    dns: Iterable[str] | None = None,
    selinux_relabel: str | None = None,
    disable_process_label: bool = False,
    main_command: list[str] | None = None,
    main_user: str | None = None,
    main_stop_signal: str | None = None,
    extra_security_opt: Iterable[str] | None = None,
    main_annotations: dict[str, str] | None = None,
) -> Path:
    """Write the Compose override that puts ``main`` under *runtime*.

    Appended last so its scalars win: Compose resolves scalar keys as
    last-writer-wins, so an override placed before a task's own compose file
    could have its ``runtime`` flipped back to ``runc``.

    ``dns`` is emitted only when resolvers were explicitly resolved for
    unrestricted-internet mode. It is declarative belt-and-braces: Docker does
    not reliably surface it in ``/etc/resolv.conf`` on a user-defined network
    (docker/compose#8441), which is why
    :mod:`titanium.environments.gvisor.network` verifies and repairs the sandbox's
    resolver after startup rather than trusting this key.

    ``selinux_relabel`` stamps ``bind: {selinux: <value>}`` on both staging
    mounts. Podman -- unlike Docker -- does not relabel bind mounts, so on an
    SELinux-enforcing host the sandbox would be denied its own staging
    directories without it; podman-compose honors the long-form ``selinux``
    option and Docker Compose has no use for it, so the docker flavor passes
    None. The value mirrors ``TITANIUM_PODMAN_SELINUX_RELABEL`` semantics ('z' or
    'Z'); anything else is ignored rather than emitted.

    ``main_command``, ``main_user``, and ``main_stop_signal`` override
    ``main``'s command, user, and stop signal when set. The krun flavor uses them for its mailbox exec runner (the
    handler has no exec; see KRUN-PODMAN.md §5). Both default to None, so
    the runsc flavors emit neither key and stay byte-identical.

    ``disable_process_label`` appends ``label=disable`` to ``security_opt``.
    Podman assigns an SELinux process label to every container on an enforcing
    host without consulting the runtime's advertised features, and runsc
    aborts on any spec that carries one ("SELinux is not supported"), leaving
    the container stuck in Created. Docker skips labeling for runsc on its
    own, so the docker flavor keeps the default False.
    """
    stage_binds: list[dict[str, object]] = [
        {
            "type": "bind",
            "source": str(Path(stage_in).resolve()),
            "target": str(STAGE_IN),
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(Path(stage_out).resolve()),
            "target": str(STAGE_OUT),
        },
    ]
    if selinux_relabel in ("z", "Z"):
        for bind in stage_binds:
            bind["bind"] = {"selinux": selinux_relabel}

    security_opt = list(SECURITY_OPT)
    if disable_process_label:
        security_opt.append("label=disable")
    security_opt.extend(extra_security_opt or ())

    main: dict[str, object] = {
        "runtime": runtime,
        "security_opt": security_opt,
        "volumes": stage_binds,
    }
    if main_command is not None:
        main["command"] = list(main_command)
    if main_user is not None:
        main["user"] = main_user
    if main_stop_signal is not None:
        main["stop_signal"] = main_stop_signal
    if main_annotations:
        main["annotations"] = dict(main_annotations)
    nameservers = list(dns or [])
    if nameservers:
        main["dns"] = nameservers

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"services": {"main": main}}, indent=2))
    return path


def engine_runtimes(cli: str = "docker", timeout_sec: int = 10) -> set[str] | None:
    """Return the runtime names the engine daemon knows, or None on error."""
    try:
        result = subprocess.run(
            [cli, "info", "--format", "{{json .Runtimes}}"],
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


def assert_runtime_registered(runtime: str, cli: str = "docker") -> None:
    """Fail closed unless *runtime* is registered with the engine daemon.

    Checking the daemon is authoritative; probing ``PATH`` for a ``runsc`` binary
    is neither necessary nor sufficient, because the daemon resolves the
    runtime's path from its own configuration.
    """
    runtimes = engine_runtimes(cli)
    if runtimes is None:
        raise RuntimeError(
            f"The gVisor environment could not query the {cli} daemon for its "
            f"registered runtimes ('{cli} info --format \"{{{{json .Runtimes}}}}\"' "
            f"failed), so it cannot confirm that {runtime!r} is available. "
            "Refusing to continue rather than silently falling back to runc."
        )
    if runtime not in runtimes:
        available = ", ".join(sorted(runtimes)) or "none"
        raise RuntimeError(
            f"The gVisor environment requires the {runtime!r} runtime to be "
            f"registered with the {cli} daemon, but '{cli} info' lists: "
            f"{available}. Register it (e.g. 'sudo runsc install') and restart "
            f"{cli}, or select a different environment with --env docker."
        )


def parse_container_ids(output: str | None) -> list[str]:
    """Extract container or network IDs from ``docker ... -q`` output.

    Shared by Compose's ``ps -q`` and the plain-``docker`` fallback discovery
    below (``ps``, ``network ls``): both emit one ID per line and both may have
    compose warnings merged into the same stream.
    """
    if not output:
        return []
    return [
        line.strip()
        for line in output.splitlines()
        if _CONTAINER_ID_RE.match(line.strip())
    ]


async def _inspect(container_id: str, template: str, cli: str) -> str | None:
    """Run ``<cli> inspect --format <template>`` and return stdout, or None."""
    try:
        process = await asyncio.create_subprocess_exec(
            cli,
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


async def container_runtime(container_id: str, cli: str = "docker") -> str | None:
    """Return the OCI runtime the engine actually used for *container_id*.

    Host-side and therefore authoritative. In-container evidence (``uname -r``
    reporting a gVisor kernel, ``dmesg``) is a useful smoke test but gVisor's own
    documentation warns it "is easily replicated by an attacker so applications
    should never use dmesg to verify the runtime in a security sensitive
    context". Nothing produced inside the sandbox is ever a gate here.
    """
    return await _inspect(container_id, "{{.HostConfig.Runtime}}", cli)


async def container_networks(container_id: str, cli: str = "docker") -> dict[str, dict]:
    """Return ``{network_name: settings}`` for *container_id*."""
    raw = await _inspect(container_id, "{{json .NetworkSettings.Networks}}", cli)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def container_state(container_id: str, cli: str = "docker") -> dict | None:
    """Return ``.State`` (``Status``, ``Error``, ...) for *container_id*, or None.

    Host-side, like :func:`container_runtime`: used to explain *why* a
    Created/Exited container was rejected without trusting anything the
    container itself produced.
    """
    raw = await _inspect(container_id, "{{json .State}}", cli)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _run_cli(cli: str, *args: str) -> tuple[int, str, str]:
    """Run ``<cli> *args`` and return ``(returncode, stdout, stderr)``.

    A failure to even launch the CLI (binary missing, daemon unreachable) is
    reported as a non-zero return with empty output rather than raised, so
    every caller sees the same shape of failure regardless of whether the CLI
    ran and failed or could not be launched at all. Callers that use the
    result for cleanup discovery or removal must inspect the return code
    themselves and fail closed -- a non-zero code here must never be read as
    "found nothing" or "removed everything".
    """
    try:
        process = await asyncio.create_subprocess_exec(
            cli,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except Exception as exc:
        return 1, "", str(exc)
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def project_container_ids(project: str, cli: str = "docker") -> list[str]:
    """Container IDs (any state) labeled for the exact Compose *project*.

    Uses plain ``docker ps``, not ``docker compose ps``: Compose scopes its own
    ``ps`` to the compose files passed on the command line, which is not
    available -- or trustworthy -- during fallback cleanup after a startup
    failure. The project label is exact-matched, so a project name that is a
    prefix of another project's name can never match the wrong resources.

    Raises rather than returning an empty list when the query itself could not
    be answered (daemon unreachable, CLI missing, non-zero exit): an empty
    list must mean "confirmed no resources remain", never "could not check",
    or fail-closed cleanup would read a query failure as a clean project.
    """
    returncode, out, err = await _run_cli(
        cli,
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label={COMPOSE_PROJECT_LABEL}={project}",
    )
    if returncode != 0:
        raise RuntimeError(
            "The gVisor environment could not list containers for Compose "
            f"project {project!r} ('{cli} ps --all --quiet --filter "
            f"label={COMPOSE_PROJECT_LABEL}={project}' exited {returncode}): "
            f"{err.strip() or out.strip() or 'no output'}"
        )
    return parse_container_ids(out)


async def project_network_ids(project: str, cli: str = "docker") -> list[str]:
    """Network IDs labeled for the exact Compose *project*.

    Raises on query failure for the same reason as :func:`project_container_ids`.
    """
    returncode, out, err = await _run_cli(
        cli,
        "network",
        "ls",
        "--quiet",
        "--filter",
        f"label={COMPOSE_PROJECT_LABEL}={project}",
    )
    if returncode != 0:
        raise RuntimeError(
            "The gVisor environment could not list networks for Compose "
            f"project {project!r} ('{cli} network ls --quiet --filter "
            f"label={COMPOSE_PROJECT_LABEL}={project}' exited {returncode}): "
            f"{err.strip() or out.strip() or 'no output'}"
        )
    return parse_container_ids(out)


async def remove_containers(container_ids: Iterable[str], cli: str = "docker") -> None:
    """Force-remove exactly *container_ids*. A no-op for an empty list.

    Raises on a non-zero removal so cleanup callers never mistake "the daemon
    refused to remove these" for "these are gone".
    """
    ids = [c for c in container_ids if c]
    if not ids:
        return
    returncode, out, err = await _run_cli(cli, "rm", "--force", *ids)
    if returncode != 0:
        raise RuntimeError(
            f"The gVisor environment could not remove container(s) {ids} "
            f"('{cli} rm --force ...' exited {returncode}): "
            f"{err.strip() or out.strip() or 'no output'}"
        )


async def remove_networks(network_ids: Iterable[str], cli: str = "docker") -> None:
    """Remove exactly *network_ids*. A no-op for an empty list.

    Raises on a non-zero removal for the same reason as :func:`remove_containers`.
    """
    ids = [n for n in network_ids if n]
    if not ids:
        return
    returncode, out, err = await _run_cli(cli, "network", "rm", *ids)
    if returncode != 0:
        raise RuntimeError(
            f"The gVisor environment could not remove network(s) {ids} "
            f"('{cli} network rm ...' exited {returncode}): "
            f"{err.strip() or out.strip() or 'no output'}"
        )


def shared_network_ipv4(
    peer_networks: dict[str, dict],
    own_networks: dict[str, dict],
) -> str | None:
    """Return *peer*'s IPv4 on a network it shares with *own*.

    Selecting the shared network avoids hard-coding Titanium's internal network name
    and answers the question that actually matters: the address at which the
    sandboxed service can reach its peer. Names are sorted so the choice is
    deterministic when more than one network is shared.
    """
    for name in sorted(set(peer_networks) & set(own_networks)):
        address = (peer_networks.get(name) or {}).get("IPAddress")
        if address:
            return address
    return None
