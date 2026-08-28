"""Podman-specific runsc probes and discovery for the gVisor environment.

Everything in :mod:`titanium.environments.gvisor.runtime` that is genuinely
engine-neutral (removal commands, ``.State`` / ``.NetworkSettings.Networks``
inspect templates, ID parsing for container listings) is reused as-is with
``cli="podman"``. This module holds only the places where Podman's schema or
conventions actually differ, each verified against a real Podman rather than
assumed:

* **Runtime reporting.** Podman's ``{{.HostConfig.Runtime}}`` is a
  Docker-compat placeholder that always reads ``"oci"``; the runtime Podman
  actually used lives in the top-level ``{{.OCIRuntime}}`` field, as the
  configured *name* when the runtime was selected by name and as the resolved
  *path* when it was selected by path. (Using the Docker template here would
  fail closed -- ``"oci" != "runsc"`` -- but it would fail on every start, so
  a correct template is required, not just a safe one.)

* **Runtime registration.** Podman has no daemon and therefore no
  ``info --format '{{json .Runtimes}}'`` registry to consult; it resolves a
  runtime name at container-create time from ``containers.conf`` and a
  compiled-in table of default paths. The only authoritative check is to make
  Podman itself perform that resolution, which
  :func:`assert_runtime_resolvable` does with an image-free
  ``podman create --rootfs`` probe.

* **Label namespaces.** podman-compose stamps both the Docker-compatible
  ``com.docker.compose.project`` label and its own
  ``io.podman.compose.project``, with the split varying across versions, so
  fail-closed discovery queries both and unions the results.

* **Network references.** ``podman network ls --quiet`` prints network *names*
  (Podman 4.x), not hex IDs, so Docker's hex-only ID parser would silently
  discard every result and teardown would read "nothing to remove" while
  networks remain. Podman's ``network rm`` accepts names and IDs alike, so
  discovery parses either.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from titanium.environments.gvisor.runtime import (
    _inspect,
    _run_cli,
    parse_container_ids,
)

# Both label namespaces podman-compose stamps on containers and networks,
# queried in this order. See _PROJECT_LABELS in
# titanium.environments.podman.podman, which handles the same version drift.
# ``titanium.trial`` is Titanium's own stamp (PodmanEnvironment._compose_base passes
# it through --podman-run-args), so container discovery keeps working even if
# a future podman-compose drops or renames its namespaces. Networks are
# created by podman-compose without run-args, so network discovery still
# rides the compose labels alone; a filter on a label no container carries
# just contributes nothing to the union.
PODMAN_PROJECT_LABELS: tuple[str, ...] = (
    "com.docker.compose.project",
    "io.podman.compose.project",
    "titanium.trial",
)
PODMAN_SERVICE_LABELS: tuple[str, ...] = (
    "com.docker.compose.service",
    "io.podman.compose.service",
)


def runtime_name_matches(actual: str | None, expected: str) -> bool:
    """Whether Podman's ``.OCIRuntime`` value names the *expected* runtime.

    ``.OCIRuntime`` holds the configured name (``runsc``) when the runtime was
    selected by name and the resolved binary path (``/usr/local/bin/runsc``)
    when selected by path, so both spellings must count as a match. The
    basename comparison is exact -- ``runsc-custom`` never matches ``runsc``.
    """
    if not actual:
        return False
    if actual == expected:
        return True
    return PurePosixPath(actual).name == expected and "/" in actual


async def container_oci_runtime(container_id: str, cli: str) -> str | None:
    """The runtime Podman actually used for *container_id* (host-side).

    Authoritative for the same reason as the Docker variant: it never trusts
    anything produced inside the sandbox.
    """
    return await _inspect(container_id, "{{.OCIRuntime}}", cli)


def parse_network_refs(output: str | None) -> list[str]:
    """Extract network references from ``podman network ls --quiet`` output.

    Accepts names as well as hex IDs: Podman 4.x prints names. Anything with
    whitespace is discarded rather than passed to ``network rm``.
    """
    if not output:
        return []
    return [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not any(ch.isspace() for ch in line.strip())
    ]


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication for label-union queries."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


async def project_container_ids_podman(project: str, cli: str) -> list[str]:
    """Container IDs (any state) labeled for the exact Compose *project*.

    Unions both label namespaces. Raises rather than returning an empty list
    when any query could not be answered, for the same reason as the Docker
    variant: an empty list must mean "confirmed no resources remain", never
    "could not check".
    """
    found: list[str] = []
    for label in PODMAN_PROJECT_LABELS:
        returncode, out, err = await _run_cli(
            cli,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={label}={project}",
        )
        if returncode != 0:
            raise RuntimeError(
                "The gVisor environment could not list containers for Compose "
                f"project {project!r} ('{cli} ps --all --quiet --filter "
                f"label={label}={project}' exited {returncode}): "
                f"{err.strip() or out.strip() or 'no output'}"
            )
        found.extend(parse_container_ids(out))
    return _dedupe(found)


async def project_network_refs_podman(project: str, cli: str) -> list[str]:
    """Network names or IDs labeled for the exact Compose *project*.

    Raises on query failure for the same fail-closed reason as
    :func:`project_container_ids_podman`.
    """
    found: list[str] = []
    for label in PODMAN_PROJECT_LABELS:
        returncode, out, err = await _run_cli(
            cli,
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"label={label}={project}",
        )
        if returncode != 0:
            raise RuntimeError(
                "The gVisor environment could not list networks for Compose "
                f"project {project!r} ('{cli} network ls --quiet --filter "
                f"label={label}={project}' exited {returncode}): "
                f"{err.strip() or out.strip() or 'no output'}"
            )
        found.extend(parse_network_refs(out))
    return _dedupe(found)


async def service_container_ids(
    project: str,
    service: str,
    cli: str,
    *,
    include_stopped: bool = False,
) -> list[str]:
    """Container IDs for *service* in *project*, resolved by label.

    Exists because ``podman-compose ps`` cannot answer this question: it
    ignores the service argument and always lists the whole project with
    ``--all``, so routing verification through it would return the trusted
    proxy -- or a stopped container -- as readily as the running ``main``.
    Direct label-filtered ``podman ps`` restores both the service scoping and
    the running-only default that verification relies on.
    """
    found: list[str] = []
    for project_label, service_label in zip(
        PODMAN_PROJECT_LABELS, PODMAN_SERVICE_LABELS
    ):
        args = ["ps"]
        if include_stopped:
            args.append("--all")
        args += [
            "--quiet",
            "--filter",
            f"label={project_label}={project}",
            "--filter",
            f"label={service_label}={service}",
        ]
        returncode, out, err = await _run_cli(cli, *args)
        if returncode != 0:
            raise RuntimeError(
                f"The gVisor environment could not resolve service {service!r} "
                f"for Compose project {project!r} ('{cli} "
                f"{' '.join(args)}' exited {returncode}): "
                f"{err.strip() or out.strip() or 'no output'}"
            )
        found.extend(parse_container_ids(out))
    return _dedupe(found)


def assert_runtime_resolvable(
    runtime: str, cli: str = "podman", timeout_sec: int = 30
) -> None:
    """Fail closed unless Podman itself can resolve *runtime*.

    Podman resolves a runtime name at create time from ``containers.conf``'s
    ``[engine.runtimes]`` table and a compiled-in table of default binary
    paths (which includes ``/usr/local/bin/runsc``). Probing ``PATH`` or
    parsing config files here would re-implement that resolution and drift
    from it, so the probe instead makes Podman perform it: an image-free
    ``create --rootfs`` against an empty directory, which validates the
    runtime and pulls nothing, followed by removal of the created container.
    An unresolvable runtime fails the create with Podman's own diagnostic.
    """
    probe_name = f"titanium-gvisor-runtime-probe-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="titanium-gvisor-rootfs-") as rootfs:
        try:
            result = subprocess.run(
                [
                    cli,
                    "create",
                    "--name",
                    probe_name,
                    "--network",
                    "none",
                    "--runtime",
                    runtime,
                    "--rootfs",
                    rootfs,
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except Exception as exc:
            raise RuntimeError(
                f"The gVisor environment could not ask {cli} to resolve the "
                f"{runtime!r} runtime ('{cli} create --runtime {runtime} "
                f"--rootfs ...' could not run: {exc}). Refusing to continue "
                "rather than discovering at compose-up time that the sandbox "
                "runtime is unavailable."
            ) from exc

        # Best-effort removal by the fixed probe name: it covers both a clean
        # create and a create that failed after registering the name. The
        # probe's outcome is judged on the create alone.
        subprocess.run(
            [cli, "rm", "--force", probe_name],
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise RuntimeError(
            f"The gVisor environment requires the {runtime!r} runtime to be "
            f"resolvable by {cli}, but a create probe failed: {detail}. "
            "Install runsc at a path Podman searches by default (e.g. "
            "/usr/local/bin/runsc -- scripts/init/runsc-podman.sh does this) "
            "or register it under [engine.runtimes] in containers.conf, or "
            "select a different environment with --env podman."
        )


# Where scripts/init/runsc-podman.sh records `<sha3-512>  <path>` for the
# binary it installed (or, trust-on-first-use, the one it found). SHA3
# deliberately: the download itself is verified against upstream's SHA-512
# release checksum, so pinning in a different hash family means a break in
# either family defeats at most one of the two checks. Overridable so
# deployments and tests can relocate the pin.
RUNSC_DIGEST_PIN = "/usr/local/share/titanium/runsc.sha3-512"


def assert_runtime_digest(
    pin_file: str | Path | None = None,
    *,
    init_script: str = "scripts/init/runsc-podman.sh",
) -> None:
    """Fail closed when the pinned runtime binary changed since install.

    The resolvability probe proves *a* binary answers to the runtime name;
    this proves it is still bit-for-bit the one the install script recorded,
    turning "a file named runsc exists" into "the runsc we installed exists".
    A missing pin file is not an error -- hosts provisioned before pinning, or
    with a distro-managed runsc, simply have no pin to enforce -- but a pin
    that names a now-missing or now-different binary is: silent downgrade to
    unpinned is exactly the tampering this check exists to catch.

    ``init_script`` names the provisioning script in the failure text, so
    each runtime's message points at its own install and rotation procedure
    (the krun flavor passes scripts/init/krun-podman.sh).
    """
    path = Path(
        pin_file
        if pin_file is not None
        else os.environ.get("TITANIUM_RUNSC_DIGEST_PIN", RUNSC_DIGEST_PIN)
    )
    try:
        pin = path.read_text()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"Runtime digest pin {path} is unreadable: {exc}")

    for line in pin.splitlines():
        line = line.strip()
        # '#' lines are comments; the krun pin records its rpm witness
        # (package NVR, verification time) in one.
        if not line or line.startswith("#"):
            continue
        expected, _, binary = line.partition("  ")
        if not expected or not binary:
            continue
        digest = hashlib.sha3_512()
        try:
            with open(binary, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise RuntimeError(
                f"Runtime digest pin {path} names {binary}, which cannot be "
                f"read ({exc}). Refusing to run the sandbox under an "
                "unverifiable runtime binary; reinstall with "
                f"{init_script} or remove the pin deliberately."
            )
        if digest.hexdigest() != expected:
            raise RuntimeError(
                f"{binary} does not match the digest recorded at install "
                f"time in {path}. The runtime binary changed outside "
                f"{init_script}; refusing to run untrusted "
                "code under it."
            )
