"""The per-task ``cella.policy`` file: grants read, or collected.

One file, two directions of travel, one grammar:

- **Enforce** (the default): the engine loads the file and releases
  exactly the crossings a grant names; everything else is refused with
  the why on the record.
- **Dry run**: the engine releases every crossing and *writes* the file
  -- each distinct crossing observed becomes one grant line. The
  collected file is then reviewed and checked in beside the task's
  build file, like a lockfile, and the next run enforces it.

The grammar is one grant per line, mirroring how cella itself names a
crossing (``cella_libs::ledger::Dest``): an IPv4 crossing refines to
ip, port, and protocol; every other frame is named by its ethertype.

    # comment
    allow outgoing 140.82.112.3:443/tcp
    allow incoming *:2222/tcp
    allow outgoing arp

``*`` matches any ip or any port. The protocol is ``tcp``, ``udp``, or
a bare IP protocol number. An L2 grant is the ethertype word cella
prints: ``arp``, ``ipv6``, or ``0xNNNN``. MACs are deliberately not in
the grammar: a policy that named a MAC would break on every rebuild of
the machine, and the ethertype is the decision that matters.

Parsing is strict: an unreadable line is an error naming its number,
never a skipped rule -- a policy file that silently lost a grant would
refuse crossings its author allowed, and one that silently lost a
refusal boundary would be worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from titanium.environments.cella.wire import (
    DIRECTION_INCOMING,
    Operation,
)

_DIRECTIONS = ("outgoing", "incoming")
_PROTO_NAMES = {6: "tcp", 17: "udp"}
_PROTO_NUMBERS = {"tcp": 6, "udp": 17}
_ETHERTYPE_NAMES = {0x0806: "arp", 0x86DD: "ipv6"}
_ETHERTYPE_NUMBERS = {"arp": 0x0806, "ipv6": 0x86DD}

HEADER = (
    "# cella.policy — the crossings this task is granted.\n"
    "# One grant per line: allow <direction> <ip>:<port>/<proto>\n"
    "#                  or allow <direction> <ethertype>\n"
    "# '*' matches any ip or any port. Everything not granted is refused.\n"
)


class PolicyError(ValueError):
    """A cella.policy file that cannot be read as written."""


@dataclass(frozen=True, order=True)
class Grant:
    """One allowed crossing shape.

    Exactly one of the two shapes cella names a frame by:
    ``ethertype`` is 0 for an IPv4 grant (ip/port/proto speak), and for
    an L2 grant it is the ethertype with ip/port/proto silent.
    """

    direction: str
    ethertype: int = 0
    ip: str = "*"
    port: int = 0  # 0 is the wildcard, matching the wire's default
    proto: int = 6

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
            # An L2 crossing: only an L2 grant with the same ethertype
            # speaks for it.
            return self.ethertype != 0 and destination.ethertype == self.ethertype
        if self.ethertype != 0:
            return False
        if self.ip != "*" and self.ip != ".".join(str(b) for b in destination.ip):
            return False
        if self.port != 0 and self.port != destination.port:
            return False
        return self.proto == destination.proto

    def line(self) -> str:
        if self.ethertype != 0:
            name = _ETHERTYPE_NAMES.get(self.ethertype, f"0x{self.ethertype:04x}")
            return f"allow {self.direction} {name}"
        port = "*" if self.port == 0 else str(self.port)
        proto = _PROTO_NAMES.get(self.proto, str(self.proto))
        return f"allow {self.direction} {self.ip}:{port}/{proto}"


def grant_for(operation: Operation) -> Grant | None:
    """The exact grant that would release *operation*, for dry-run
    collection. ``None`` for an operation with no destination: there is
    nothing to name, and a grant naming nothing would grant anything.
    """
    destination = operation.destination
    if destination is None:
        return None
    direction = "incoming" if operation.direction == DIRECTION_INCOMING else "outgoing"
    if not destination.ip:
        if destination.ethertype == 0:
            return None
        return Grant(direction=direction, ethertype=destination.ethertype)
    return Grant(
        direction=direction,
        ip=".".join(str(b) for b in destination.ip),
        port=destination.port,
        proto=destination.proto,
    )


def _parse_grant(line: str, lineno: int) -> Grant:
    parts = line.split()
    if len(parts) != 3 or parts[0] != "allow":
        raise PolicyError(
            f"cella.policy line {lineno}: want 'allow <direction> <spec>', "
            f"got {line!r}."
        )
    direction = parts[1]
    if direction not in _DIRECTIONS:
        raise PolicyError(
            f"cella.policy line {lineno}: direction must be one of "
            f"{_DIRECTIONS}, got {direction!r}."
        )
    spec = parts[2]

    if ":" not in spec:
        # An L2 grant, by ethertype word or number.
        if spec in _ETHERTYPE_NUMBERS:
            return Grant(direction=direction, ethertype=_ETHERTYPE_NUMBERS[spec])
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
        return Grant(direction=direction, ethertype=ethertype)

    address, _, proto_word = spec.rpartition("/")
    if not address:
        raise PolicyError(
            f"cella.policy line {lineno}: want <ip>:<port>/<proto>, got {spec!r}."
        )
    ip, _, port_word = address.rpartition(":")
    if not ip:
        raise PolicyError(
            f"cella.policy line {lineno}: want <ip>:<port>/<proto>, got {spec!r}."
        )
    if ip != "*":
        octets = ip.split(".")
        if len(octets) != 4 or not all(
            o.isdigit() and 0 <= int(o) <= 255 for o in octets
        ):
            raise PolicyError(
                f"cella.policy line {lineno}: {ip!r} is not an IPv4 address or '*'."
            )
        # One canonical spelling, so two lines cannot name one grant.
        ip = ".".join(str(int(o)) for o in octets)
    if port_word == "*":
        port = 0
    else:
        try:
            port = int(port_word)
        except ValueError as exc:
            raise PolicyError(
                f"cella.policy line {lineno}: port {port_word!r} is not a "
                f"number or '*'."
            ) from exc
        if not 0 < port <= 65535:
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
    return Grant(direction=direction, ip=ip, port=port, proto=proto)


@dataclass(frozen=True)
class Policy:
    """A parsed cella.policy: the grants, in file order."""

    grants: tuple[Grant, ...]

    def grants_crossing(self, operation: Operation) -> bool:
        return any(grant.matches(operation) for grant in self.grants)

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
    """Dry-run collection: every distinct crossing becomes one grant.

    The file is rewritten on every new grant rather than at shutdown,
    so a run that dies mid-way still leaves everything it observed --
    the collected policy is evidence, and evidence is written as it
    happens.
    """

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
