"""The per-task ``cella.policy`` file: the engine's grant list.

This is cella's own recommended grammar (docs/integration/
MEMBRANE-MEMORY.md), adopted verbatim so the file's words are the
wire's words and the chronicle's words, with no translation table:

    <release|refuse> <incoming|outgoing> <destination> (key=value)*

    # comment
    release outgoing 140.82.112.3:443/tcp (keep_open=5m) (skip_freeze=true)
    release incoming 140.82.112.3:443/tcp (keep_open=5m)
    release outgoing arp (keep_open=24h) (skip_freeze=true)
    refuse  outgoing 169.254.169.254:80/tcp (keep_open=24h) (reason="metadata")

A destination is exact -- ``ip:port/proto`` (``tcp``/``udp``/a bare IP
protocol number), a **host** name (``deb.debian.org:80/tcp``, or a
leading-dot suffix ``.debian.org`` for every subdomain), or an
ethertype word (``arp``, ``ipv6``, ``0xNNNN``). A host grant matches
the name the terminator resolved and stamped on the crossing
(proto/cella.proto, "the ratchet acts on names"), so it survives the
ip rotation a CDN gives an exact-ip grant; an ip-only crossing never
matches a host grant. ``*`` matches any ip or any port in a *verdict*;
MACs are never named (they change on every rebuild). Everything not
granted is refused -- default-refuse is the ground state.

The keys carry cella's membrane memory (N.F.7):

- ``keep_open=<window>`` -- ``90s`` / ``5m`` / ``24h`` / bare seconds.
  Its presence plants a standing memory for the destination, so the
  machine stops freezing on the crossing and instead waits *live* --
  what keeps a TLS handshake's flights inside the real peer's
  patience. Absent, the crossing is judged the cryogenic way: the park
  is the freeze.
- ``skip_freeze=true|false`` -- outgoing only (an incoming park never
  freezes), and needs ``keep_open``. ``true`` is the live wait above.
- ``reason="..."`` -- refuse only; lands verbatim in the chronicle's
  ``Lapsed``.

Parsing is strict: an unreadable line is an error naming its number,
never a skipped rule -- a policy that silently lost a grant would
refuse crossings its author allowed, and one that silently lost a
refusal boundary would be worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from titanium.environments.cella.wire import (
    DIRECTION_INCOMING,
    Destination,
    MembraneMemory,
    Operation,
)

_VERBS = ("release", "refuse")
_DIRECTIONS = ("outgoing", "incoming")
_PROTO_NAMES = {6: "tcp", 17: "udp"}
_PROTO_NUMBERS = {"tcp": 6, "udp": 17}
_ETHERTYPE_NAMES = {0x0806: "arp", 0x86DD: "ipv6"}
_ETHERTYPE_NUMBERS = {"arp": 0x0806, "ipv6": 0x86DD}
_WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600}

HEADER = (
    "# cella.policy — the crossings this task is granted.\n"
    "# <release|refuse> <incoming|outgoing> <destination> (key=value)*\n"
    '# keys: keep_open=<90s|5m|24h> skip_freeze=true reason="..."\n'
    "# '*' matches any ip or any port. Everything not granted is refused.\n"
)


class PolicyError(ValueError):
    """A cella.policy file that cannot be read as written."""


def _format_window(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _parse_window(value: str, lineno: int) -> int:
    raw = value
    unit = 1
    if value and value[-1] in _WINDOW_UNITS:
        unit = _WINDOW_UNITS[value[-1]]
        value = value[:-1]
    try:
        seconds = int(value) * unit
    except ValueError as exc:
        raise PolicyError(
            f"cella.policy line {lineno}: keep_open {raw!r} is not a window "
            f"(seconds, or a number with s/m/h)."
        ) from exc
    if seconds <= 0:
        raise PolicyError(
            f"cella.policy line {lineno}: keep_open must be positive, got {raw!r}."
        )
    return seconds


@dataclass(frozen=True, order=True)
class Grant:
    """One grant line.

    ``verb`` is release or refuse; the destination is one of the three
    shapes a frame is named by: an ``ethertype`` (non-zero, for an L2
    frame), a ``host`` (a domain, matched against the name the
    appliance resolved and stamped on the crossing -- proto/cella.proto,
    "the ratchet acts on names"), or ``ip``/port/proto. ``keep_open`` >
    0 plants a membrane memory for a matched crossing's *exact*
    destination; ``skip_freeze`` makes that memory a live wait;
    ``reason`` is the refusal's recorded why.

    A host destination is exact (``deb.debian.org``) or a leading-dot
    suffix (``.debian.org`` matches the bare domain and every
    subdomain -- harbor's other allowlist form). It reaches only what
    the terminator resolved: an ip-only crossing (no name on it) never
    matches a host grant, so a name grant fails closed.
    """

    verb: str = "release"
    direction: str = "outgoing"
    ethertype: int = 0
    host: str = ""
    ip: str = "*"
    port: int = 0  # 0 is the wildcard, matching the wire's default
    proto: int = 6
    keep_open: int = 0
    skip_freeze: bool = False
    reason: str = ""

    def _host_matches(self, name: str) -> bool:
        if not name:
            return False  # an unnamed crossing never matches a name grant
        if self.host.startswith("."):
            bare = self.host[1:]
            return name == bare or name.endswith("." + bare)
        return name == self.host

    def matches(self, operation: Operation) -> bool:
        direction = (
            "incoming" if operation.direction == DIRECTION_INCOMING else "outgoing"
        )
        if direction != self.direction:
            return False
        destination = operation.destination
        if destination is None:
            return False
        if not destination.ip:
            return self.ethertype != 0 and destination.ethertype == self.ethertype
        if self.ethertype != 0:
            return False
        if self.host:
            if not self._host_matches(destination.host):
                return False
        elif self.ip != "*" and self.ip != ".".join(str(b) for b in destination.ip):
            return False
        if self.port != 0 and self.port != destination.port:
            return False
        return self.proto == destination.proto

    def memory_for(self, operation: Operation) -> MembraneMemory | None:
        """The standing memory this grant plants for *operation*, or
        ``None`` when the grant plants none. Built from the park's own
        exact Destination -- a wildcard grant still remembers each
        concrete crossing it matched, and cella's rule that a memory
        names an exact destination holds.

        An **incoming** grant never plants a memory: an incoming park
        never freezes (skip_freeze is outgoing only), so its memory
        would be meaningless -- and worse, cella keys a memory by its
        destination alone, so an incoming memory (skip_freeze=False)
        would collide with and suppress the outgoing grant's
        skip_freeze=True memory for the same destination, defeating the
        live wait the outgoing leg asked for."""
        if (
            self.keep_open <= 0
            or self.direction == "incoming"
            or operation.destination is None
        ):
            return None
        return MembraneMemory(
            destination=operation.destination,
            skip_freeze=self.skip_freeze,
            keep_open=self.keep_open,
        )

    def standing_memory(self) -> MembraneMemory | None:
        """The memory to pre-plant at stream open, before any crossing --
        the reference engine (cella-engine motor) does this so the first
        crossing to a granted destination never freezes. Only a grant
        with a *concrete* outgoing destination and a live window
        qualifies: ARP (an ethertype), or an exact ip:port/proto. A host
        or wildcard-ip grant names no concrete destination until the
        appliance resolves it, so its first crossing plants reactively in
        :meth:`memory_for` and freezes once, unavoidably."""
        if (
            self.verb != "release"
            or self.direction != "outgoing"
            or self.keep_open <= 0
            or not self.skip_freeze
            or self.host
        ):
            return None
        if self.ethertype != 0:
            destination = Destination(ethertype=self.ethertype)
        elif self.ip != "*":
            destination = Destination(
                ip=bytes(int(o) for o in self.ip.split(".")),
                port=self.port,
                proto=self.proto,
                ethertype=0x0800,
            )
        else:
            return None  # a wildcard ip has no concrete destination
        return MembraneMemory(
            destination=destination,
            skip_freeze=True,
            keep_open=self.keep_open,
        )

    def line(self) -> str:
        if self.ethertype != 0:
            dest = _ETHERTYPE_NAMES.get(self.ethertype, f"0x{self.ethertype:04x}")
        else:
            port = "*" if self.port == 0 else str(self.port)
            proto = _PROTO_NAMES.get(self.proto, str(self.proto))
            dest = f"{self.host or self.ip}:{port}/{proto}"
        parts = [self.verb, self.direction, dest]
        if self.keep_open > 0:
            parts.append(f"(keep_open={_format_window(self.keep_open)})")
        if self.skip_freeze:
            parts.append("(skip_freeze=true)")
        if self.reason:
            parts.append(f'(reason="{self.reason}")')
        return " ".join(parts)


def grant_for(operation: Operation) -> Grant | None:
    """The release grant that would name *operation*, for dry-run
    collection. ``None`` for a destinationless or unnamed operation."""
    destination = operation.destination
    if destination is None:
        return None
    direction = "incoming" if operation.direction == DIRECTION_INCOMING else "outgoing"
    if not destination.ip:
        if destination.ethertype == 0:
            return None
        return Grant(
            verb="release", direction=direction, ethertype=destination.ethertype
        )
    # Prefer the resolved name when the terminator stamped one: a name
    # grant survives the ip rotation an exact-ip grant would not.
    if destination.host:
        return Grant(
            verb="release",
            direction=direction,
            host=destination.host.lower(),
            port=destination.port,
            proto=destination.proto,
        )
    return Grant(
        verb="release",
        direction=direction,
        ip=".".join(str(b) for b in destination.ip),
        port=destination.port,
        proto=destination.proto,
    )


_KEY_RE = re.compile(r"\(([a-z_]+)=(.*?)\)")

# A domain destination: dotted labels of letters, digits, and hyphens,
# with an optional leading dot for a subdomain suffix (``.debian.org``).
# At least one dot, so a bare word is never mistaken for a host.
_HOST_RE = re.compile(
    r"^\.?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)


def _looks_like_ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _parse_destination(spec: str, lineno: int) -> dict:
    if ":" not in spec:
        if spec in _ETHERTYPE_NUMBERS:
            return {"ethertype": _ETHERTYPE_NUMBERS[spec]}
        try:
            ethertype = int(spec, 16) if spec.startswith("0x") else int(spec)
        except ValueError as exc:
            raise PolicyError(
                f"cella.policy line {lineno}: unknown ethertype {spec!r}."
            ) from exc
        if not 0 < ethertype <= 0xFFFF:
            raise PolicyError(
                f"cella.policy line {lineno}: ethertype {spec!r} is out of range."
            )
        return {"ethertype": ethertype}

    address, _, proto_word = spec.rpartition("/")
    ip, _, port_word = address.rpartition(":")
    if not address or not ip:
        raise PolicyError(
            f"cella.policy line {lineno}: want <ip|host>:<port>/<proto>, got {spec!r}."
        )
    host = ""
    if ip == "*":
        pass
    elif _looks_like_ipv4(ip):
        octets = ip.split(".")
        if not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
            raise PolicyError(
                f"cella.policy line {lineno}: {ip!r} is not an IPv4 address."
            )
        ip = ".".join(str(int(o)) for o in octets)
    elif _HOST_RE.match(ip):
        # A domain name: matched against the name the terminator
        # resolved and stamped on the crossing, not against the ip.
        host, ip = ip.lower(), "*"
    else:
        raise PolicyError(
            f"cella.policy line {lineno}: {ip!r} is not an IPv4 address, a "
            f"host name, or '*'."
        )
    if port_word == "*":
        port = 0
    else:
        try:
            port = int(port_word)
        except ValueError as exc:
            raise PolicyError(
                f"cella.policy line {lineno}: port {port_word!r} is not a number or '*'."
            ) from exc
        if not 0 <= port <= 65535:
            raise PolicyError(
                f"cella.policy line {lineno}: port {port} is out of range."
            )
    if proto_word in _PROTO_NUMBERS:
        proto = _PROTO_NUMBERS[proto_word]
    else:
        try:
            proto = int(proto_word)
        except ValueError as exc:
            raise PolicyError(
                f"cella.policy line {lineno}: protocol {proto_word!r} is not "
                f"tcp, udp, or a number."
            ) from exc
        if not 0 <= proto <= 255:
            raise PolicyError(
                f"cella.policy line {lineno}: protocol {proto} is out of range."
            )
    return {"host": host, "ip": ip, "port": port, "proto": proto}


def _parse_grant(line: str, lineno: int) -> Grant:
    keys = dict(_KEY_RE.findall(line))
    head = _KEY_RE.sub("", line).split()
    if len(head) != 3 or head[0] not in _VERBS or head[1] not in _DIRECTIONS:
        raise PolicyError(
            f"cella.policy line {lineno}: want "
            f"'<release|refuse> <incoming|outgoing> <destination> (key=value)*', "
            f"got {line!r}."
        )
    verb, direction, spec = head
    dest = _parse_destination(spec, lineno)

    keep_open = 0
    if "keep_open" in keys:
        keep_open = _parse_window(keys.pop("keep_open"), lineno)
    skip_freeze = False
    if "skip_freeze" in keys:
        raw = keys.pop("skip_freeze").lower()
        if raw not in ("true", "false"):
            raise PolicyError(
                f"cella.policy line {lineno}: skip_freeze must be true or false."
            )
        skip_freeze = raw == "true"
    reason = keys.pop("reason", "").strip('"')
    if keys:
        raise PolicyError(f"cella.policy line {lineno}: unknown key(s) {sorted(keys)}.")

    # cella's discipline: skip_freeze needs a window and is outgoing
    # only; reason is refuse only.
    if skip_freeze and keep_open <= 0:
        raise PolicyError(
            f"cella.policy line {lineno}: skip_freeze needs a keep_open window."
        )
    if skip_freeze and direction != "outgoing":
        raise PolicyError(
            f"cella.policy line {lineno}: skip_freeze is outgoing only "
            f"(an incoming park never freezes)."
        )
    if reason and verb != "refuse":
        raise PolicyError(f"cella.policy line {lineno}: reason is refuse only.")
    return Grant(
        verb=verb,
        direction=direction,
        keep_open=keep_open,
        skip_freeze=skip_freeze,
        reason=reason,
        **dest,
    )


@dataclass(frozen=True)
class Match:
    """The engine's read of one park against the policy: the verdict
    (``release`` bool, ``reason`` for a refuse) and the standing memory
    to plant, if any."""

    release: bool
    reason: str = ""
    memory: MembraneMemory | None = None


@dataclass(frozen=True)
class Policy:
    """A parsed cella.policy: the grants, in file order."""

    grants: tuple[Grant, ...] = field(default_factory=tuple)

    def evaluate(self, operation: Operation) -> Match:
        """Judge one park: the first matching grant decides, and its
        window (if any) plants a memory. No match is default-refuse."""
        for grant in self.grants:
            if grant.matches(operation):
                return Match(
                    release=grant.verb == "release",
                    reason=grant.reason,
                    memory=grant.memory_for(operation),
                )
        return Match(release=False)

    @classmethod
    def parse(cls, text: str) -> Policy:
        grants = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            grants.append(_parse_grant(line, lineno))
        return cls(grants=tuple(grants))

    @classmethod
    def load(cls, path: Path) -> Policy:
        try:
            text = path.read_text()
        except OSError as exc:
            raise PolicyError(f"cannot read {path}: {exc}") from exc
        return cls.parse(text)

    def render(self) -> str:
        lines = [HEADER]
        lines += sorted(grant.line() for grant in set(self.grants))
        return "\n".join(lines) + "\n"


class PolicyRecorder:
    """Dry-run collection: every distinct crossing becomes one release
    grant. Rewritten on every new grant, so a run that dies mid-way
    still leaves what it observed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._grants: set[Grant] = set()

    @property
    def grants(self) -> frozenset[Grant]:
        return frozenset(self._grants)

    def record(self, operation: Operation) -> None:
        grant = grant_for(operation)
        if grant is None or grant in self._grants:
            return
        self._grants.add(grant)
        self._path.write_text(Policy(grants=tuple(self._grants)).render())
