"""Resolver selection and connectivity probing for unrestricted-internet mode.

Everything here is a pure function over strings and host files so that resolver
selection, ``resolv.conf`` rendering and probe construction can be tested without
Docker, without gVisor and without a network.

Scope: **this module is used only when ``allow_internet`` is true.** The
no-network path (``network_mode: none``) and the allowlist path (trusted Squid
proxy on an ``internal`` network, addressed by literal IPv4) are untouched. In
allowlist mode the *proxy* resolves hostnames, so the sandbox never needs DNS and
its ``resolv.conf`` is never rewritten.

Why this exists: a gVisor sandbox has its own network stack and does not inherit
the container network namespace's netfilter rules, so Docker's embedded resolver
at ``127.0.0.11`` -- reached through DNAT to loopback -- is unreachable
(google/gvisor#7469, open). Without a usable resolver an ``allow_internet = true``
task cannot resolve a single hostname. Sources disagree on the workaround: gVisor's
own Compose tutorial recommends setting ``dns:`` on the service, while
docker/compose#8441 reports that key never reaches ``/etc/resolv.conf`` on a
user-defined network. Rather than bet on either, the trusted control plane
inspects the sandbox's actual resolver after startup, repairs it only when it is
unusable, and then proves DNS *and* outbound TCP work before the environment is
handed to an agent.
"""

from __future__ import annotations

import ipaddress
import shlex
from collections.abc import Iterable, Sequence
from pathlib import Path

# Read in order; the first file yielding a usable resolver wins. The systemd
# stub file is included because on systemd-resolved hosts (Fedora, Ubuntu)
# /etc/resolv.conf holds only the 127.0.0.53 stub while the real upstreams live
# here.
HOST_RESOLV_SOURCES: tuple[Path, ...] = (
    Path("/etc/resolv.conf"),
    Path("/run/systemd/resolve/resolv.conf"),
)

SANDBOX_RESOLV_PATH = "/etc/resolv.conf"

# Marker echoed by the probe so success is proven by the command's own output
# rather than inferred from an exit code alone.
PROBE_OK_MARKER = "TITANIUM_GVISOR_NET_OK"

DEFAULT_PROBE_HOST = "example.com"
DEFAULT_PROBE_PORT = 443
DEFAULT_PROBE_TIMEOUT_SEC = 15


def is_usable_resolver(address: str) -> bool:
    """Whether *address* is reachable as a nameserver from inside a gVisor sandbox.

    Rejects every loopback address (which covers Docker's embedded resolver at
    ``127.0.0.11`` and the systemd-resolved stub at ``127.0.0.53``), the IPv6
    loopback ``::1``, and the unspecified addresses ``0.0.0.0`` / ``::``. A
    loopback nameserver names a listener in *some* network namespace's loopback;
    the sandbox's own loopback has nothing listening on it, so such an entry can
    never resolve.
    """
    try:
        parsed = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return not (parsed.is_loopback or parsed.is_unspecified)


def parse_resolv_conf(text: str) -> tuple[list[str], list[str]]:
    """Split ``resolv.conf`` *text* into ``(nameservers, other_lines)``.

    ``other_lines`` keeps ``search`` / ``options`` / ``domain`` directives (and
    comments) verbatim so a rewrite preserves the task image's own search path
    instead of silently dropping it.
    """
    nameservers: list[str] = []
    other: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        fields = stripped.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            nameservers.append(fields[1])
        elif stripped:
            other.append(stripped)
    return nameservers, other


def needs_normalization(text: str) -> bool:
    """Whether the sandbox's current resolver configuration must be repaired.

    True when there is no ``nameserver`` at all, or when every declared
    nameserver is unusable from inside the sandbox. A file that already lists at
    least one usable resolver is left completely alone.
    """
    nameservers, _ = parse_resolv_conf(text)
    return not any(is_usable_resolver(address) for address in nameservers)


def parse_explicit_dns(value: object) -> list[str]:
    """Parse the ``dns`` environment kwarg into a list of addresses.

    Accepts ``"1.1.1.1,9.9.9.9"``, ``["1.1.1.1", "9.9.9.9"]`` or a single
    address. Every entry must be usable; an explicitly configured loopback
    address is a caller error and is rejected rather than quietly dropped.
    """
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [part.strip() for part in value.split(",")]
    elif isinstance(value, Iterable):
        candidates = [str(part).strip() for part in value]
    else:
        candidates = [str(value).strip()]

    resolvers = [candidate for candidate in candidates if candidate]
    rejected = [
        candidate for candidate in resolvers if not is_usable_resolver(candidate)
    ]
    if rejected:
        raise ValueError(
            "The gVisor environment rejects the configured DNS server(s) "
            f"{', '.join(repr(item) for item in rejected)}: a loopback, "
            "unspecified or malformed address cannot be reached from inside a "
            "gVisor sandbox. Pass a routable nameserver, e.g. "
            "--ek dns=1.1.1.1."
        )
    return resolvers


def host_resolvers(sources: Sequence[Path] | None = None) -> list[str]:
    """Return usable nameservers from the host's own resolver configuration.

    Read-only: host files are inspected, never modified. The first source that
    yields at least one usable address wins, so a systemd-resolved host falls
    through the 127.0.0.53 stub to the real upstreams.

    ``sources`` resolves to :data:`HOST_RESOLV_SOURCES` at call time rather than
    at definition time, so the module constant stays overridable.
    """
    for source in HOST_RESOLV_SOURCES if sources is None else sources:
        try:
            text = Path(source).read_text()
        except OSError:
            continue
        nameservers, _ = parse_resolv_conf(text)
        usable = [address for address in nameservers if is_usable_resolver(address)]
        if usable:
            return usable
    return []


def select_resolvers(
    explicit: object = None,
    sources: Sequence[Path] | None = None,
) -> list[str]:
    """Choose the nameservers the sandbox will use, or fail closed.

    Order: explicitly configured servers win; otherwise the host's own usable
    resolver configuration is inherited. There is deliberately **no** hardcoded
    public fallback -- silently pointing a user's traffic at a third-party
    resolver is not a decision this code gets to make. A host with only a
    loopback stub and no ``--ek dns=`` fails closed with an actionable message.
    """
    resolvers = parse_explicit_dns(explicit)
    if resolvers:
        return resolvers

    checked_sources = HOST_RESOLV_SOURCES if sources is None else sources
    resolvers = host_resolvers(checked_sources)
    if resolvers:
        return resolvers

    checked = ", ".join(str(source) for source in checked_sources)
    raise RuntimeError(
        "The gVisor environment could not find a usable DNS resolver for an "
        "unrestricted-internet task. Docker's embedded resolver at 127.0.0.11 is "
        "unreachable from a gVisor sandbox (google/gvisor#7469), and no routable "
        f"nameserver was found in: {checked}. Configure one explicitly, e.g. "
        "--ek dns=1.1.1.1, or run the task under --env docker. Refusing to "
        "continue rather than guessing a public resolver."
    )


def render_resolv_conf(
    nameservers: Sequence[str], preserved: Sequence[str] = ()
) -> str:
    """Render a ``resolv.conf`` body from *nameservers*, keeping *preserved* lines."""
    lines = [f"nameserver {address}" for address in nameservers]
    lines.extend(preserved)
    return "\n".join(lines) + "\n"


def resolv_conf_write_command(content: str, path: str = SANDBOX_RESOLV_PATH) -> str:
    """Build the shell command that rewrites the sandbox's ``resolv.conf``.

    Truncate-in-place through a redirect. ``sed -i`` and ``mv`` are deliberately
    **not** used: Docker bind-mounts ``/etc/resolv.conf`` from its own data root
    into the container, and replacing a bind-mounted file by rename fails
    (``EBUSY``/``EXDEV``) -- the same class of cross-mount rename failure gVisor
    exhibits elsewhere. A redirect writes through the existing inode, which the
    bind mount propagates. Only ever run inside the already-verified sandbox;
    the host's own ``resolv.conf`` is never touched.
    """
    return f"printf %s {shlex.quote(content)} > {shlex.quote(path)}"


def resolv_conf_read_command(path: str = SANDBOX_RESOLV_PATH) -> str:
    """Build the shell command that reads the sandbox's ``resolv.conf``."""
    return f"cat {shlex.quote(path)}"


def probe_command(
    host: str = DEFAULT_PROBE_HOST,
    port: int = DEFAULT_PROBE_PORT,
    timeout_sec: int = DEFAULT_PROBE_TIMEOUT_SEC,
) -> str:
    """Build a command proving hostname resolution *and* outbound TCP.

    Uses bash's ``/dev/tcp`` redirection, which resolves the name and opens the
    connection in one step. That is the one probe tool this repository can
    actually guarantee: ``DockerEnvironment`` executes every command through
    ``bash -c`` (``docker_unix.py``), Titanium's own egress-proxy healthcheck uses
    ``</dev/tcp/127.0.0.1/8080``, and ``examples/tasks/hello-world-no-internet``
    asserts network isolation with ``</dev/tcp/example.com/80``. ``curl``,
    ``wget``, ``dig``, ``nslookup``, ``getent`` and ``python3`` are **not**
    assumed to exist in a task image.

    Success is proven by the marker on stdout, not by an exit code alone, so a
    shell that silently tolerates the redirection cannot produce a false pass. A
    bash built without net redirections fails the probe, which is correct: the
    environment then fails closed with the captured output rather than starting a
    task whose network policy it cannot verify.
    """
    connect = f"exec 3<>/dev/tcp/{host}/{int(port)}"
    return (
        f"timeout {int(timeout_sec)} bash -c {shlex.quote(connect)} "
        f"&& printf %s\\\\n {shlex.quote(PROBE_OK_MARKER)}"
    )
