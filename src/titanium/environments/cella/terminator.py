"""The agent line as cella's terminated pair (the terminator).

The tinyproxy router this module replaces was a stopgap. cella's own
network appliance is the terminator (docs/integration/TLS-TERMINATOR.md,
docs/NETWORK-MODEL.md "The terminator"): one appliance per trial that
splits every TCP connection in two. The member's peer *is* the
terminator (its patience configured, not negotiated -- the member may
freeze mid-handshake for as long as judgment takes), and the world leg
is the terminator's own connection, made at wire speed. TLS is
terminated on a leaf minted at runtime from the pair CA; plain TCP is
spliced.

Two properties this buys titanium, both from cella's contract:

- **Names cross the membrane.** The terminator is the resolver: it
  answers every member DNS query with its own wire address, reads the
  real name from the SNI (TLS) or the Host header (plain HTTP), and
  resolves it upstream at connect time. Because that upstream lookup
  crosses the appliance's world nic, the membrane witnesses name<->ip
  and stamps the resolved name onto the world-leg crossing
  (proto/cella.proto, "the ratchet acts on names"). titanium's engine
  then judges by *name* (:mod:`titanium.environments.cella.policy`), so
  a grant survives the ip rotation a CDN gives an exact-ip grant.
- **The middle is consented, once, at build.** The member trusts the
  pair CA because titanium bakes that CA into its trust store at image
  build -- a recorded act, never injected at run time. One pair, one
  CA, one blast radius (TLS-TERMINATOR.md, "the middle is consented").

Addressing follows the pair-0 convention: the appliance is the
member's gateway at ``10.77.0.1``; the member is ``10.77.0.2``. The
appliance boots the terminator golden (``cella build rootfs
terminator``) and reads ``/etc/cella-terminator.conf``; the member is
any task image with the pair CA baked, ``resolv.conf`` pointing at the
appliance, and its ephemeral ports pinned to the reply window.
"""

from __future__ import annotations

from pathlib import Path

from titanium.environments.cella.boot_layer import BootEntry, GuestFile

# The wire's pair-0 convention: the appliance is the member's gateway.
APPLIANCE_WIRE_ADDRESS = "10.77.0.1"
MEMBER_WIRE_ADDRESS = "10.77.0.2"
_WIRE_PREFIX = 24

# The ports the appliance terminates on: TLS by SNI, plain HTTP by
# Host, on any of them. 443 carries the agent's inference and every
# https fetch; 80 carries apt's plain-HTTP mirrors.
LISTEN_PORTS = (443, 80)

# The real resolver the appliance asks upstream. The names live at the
# appliance; this is where it turns them into addresses.
UPSTREAM_DNS = "9.9.9.9"

# The consistent reply port window (docs/integration/MEMBRANE-MEMORY.md,
# "The consistent reply port"): the member pins its ephemeral range to a
# narrow, agreed window, and the appliance grants that window as exact
# destinations -- so the appliance's replies toward the member never
# freeze on an unnameable ephemeral port. Eight ports is the concurrency
# budget (the 4-tuple still demuxes each flow).
REPLY_PORT_LOW = 50000
REPLY_PORT_HIGH = 50007

# The terminator golden and the pair CA it exports beside itself
# (scripts/init/cella.sh builds both, one per host).
TERMINATOR_GOLDEN = "terminator"

# Guest paths: where the appliance reads its config, and where the
# member carries the pair CA and points its resolver.
TERMINATOR_CONF_PATH = "/etc/cella-terminator.conf"
MEMBER_CA_PATH = "/etc/cella/pair-ca.pem"
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def pair_ca_path(home: Path) -> Path:
    """Where the terminator golden's build exported the pair CA."""
    return home / ".cella" / "rootfs" / TERMINATOR_GOLDEN / "ca.pem"


def terminator_golden_rootfs(home: Path) -> Path:
    """The terminator golden's rootfs ext4, the appliance's template."""
    return home / ".cella" / "rootfs" / TERMINATOR_GOLDEN / "rootfs.ext4"


def terminator_conf_text() -> str:
    """The appliance's ``/etc/cella-terminator.conf``: constant across
    trials (the wire address is the pair-0 gateway, the resolver and the
    listen ports are fixed), so one baked flavor serves every trial."""
    listen = ",".join(str(p) for p in LISTEN_PORTS)
    return (
        f"wire_ip={APPLIANCE_WIRE_ADDRESS}\n"
        f"upstream_dns={UPSTREAM_DNS}\n"
        f"listen={listen}\n"
    )


def terminator_conf_entry() -> GuestFile:
    """The conf as a boot-layer entry, injected into a copy of the
    terminator golden to make titanium's appliance flavor."""
    return GuestFile(
        path=TERMINATOR_CONF_PATH,
        contents=terminator_conf_text().encode(),
        mode=0o644,
        uid=0,
        gid=0,
    )


def member_trust_entries(ca_pem: bytes) -> list[BootEntry]:
    """What every task image bakes to become a member: the pair CA in
    its trust store, and ``resolv.conf`` pointing at the appliance's
    interceptor. The CA is *placed* here; the prelude folds it into the
    system bundle so apt, git, curl, and the agent's SDK all honor it."""
    return [
        GuestFile(
            path=MEMBER_CA_PATH,
            contents=ca_pem,
            mode=0o444,
            uid=0,
            gid=0,
        ),
        GuestFile(
            path="/etc/resolv.conf",
            # The appliance freezes once on the first reply to each of
            # the member's reply ports (the park is the freeze, before
            # the standing memory is planted), and a deep thaw re-warms
            # for seconds. The resolver's patience must outlast that, or
            # the lookup times out before the frozen reply is thawed and
            # delivered -- the member never reaches the world at all.
            contents=(
                f"nameserver {APPLIANCE_WIRE_ADDRESS}\n"
                "options timeout:30 attempts:3 single-request\n"
            ).encode(),
            mode=0o644,
            uid=0,
            gid=0,
        ),
    ]


def wire_up_commands(interface: str, address: str) -> str:
    """Shell lines that address a wire nic with ip(8), idempotently.
    A wire has no kernel autoconfiguration (cella supplies ``ip=`` for
    world nics only), and provisioning guarantees iproute2."""
    return (
        f"ip link set {interface} up || true\n"
        f"ip addr replace {address}/{_WIRE_PREFIX} dev {interface} || true\n"
    )


def member_prelude(interface: str) -> str:
    """The member's boot prelude: address the wire, fold the pair CA
    into the system trust bundle, point Python's TLS at that bundle, and
    pin the ephemeral port range to the reply window the appliance
    grants. Each step tolerates a re-run and a minimal image.

    Folding the CA into the system bundle covers the clients that read
    it -- curl, git, apt, and openssl. It does NOT cover a Python client
    (the agent's inference SDK, PyPI's pip): those verify against
    certifi's own bundle, not the system store, so the minted leaf reads
    as a self-signed chain and the inference call fails on TLS. So the
    prelude also exports SSL_CERT_FILE and REQUESTS_CA_BUNDLE at the
    system bundle: runuser preserves this environment into the agent
    (see the exec job), which is where the inference client runs."""
    return (
        wire_up_commands(interface, MEMBER_WIRE_ADDRESS)
        + f"cat {MEMBER_CA_PATH} >> {SYSTEM_CA_BUNDLE} 2>/dev/null || true\n"
        + f"export SSL_CERT_FILE={SYSTEM_CA_BUNDLE}\n"
        + f"export REQUESTS_CA_BUNDLE={SYSTEM_CA_BUNDLE}\n"
        + f"echo '{REPLY_PORT_LOW} {REPLY_PORT_HIGH}' "
        "> /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || true\n"
    )


def _round_trip(dest: str, proto: str, window: str) -> str:
    """A granted destination and its reply twin. The outgoing leg carries
    a window and skip_freeze, so the machine waits live instead of
    freezing on the crossing. The incoming twin is bare: an incoming park
    never freezes, so a window there would only plant an inert memory
    (skip_freeze is outgoing-only by cella's design) -- the verdict
    releases it, no memory needed."""
    return (
        f"release outgoing {dest}/{proto} (keep_open={window}) (skip_freeze=true)\n"
        f"release incoming {dest}/{proto}\n"
    )


def member_policy_text() -> str:
    """The member border, fixed for every task: the wire plane's ARP and
    the appliance itself. The member reaches only its appliance -- 443
    (https + inference), 80 (apt), 53 (the interceptor's DNS) -- so its
    grants are three exact destinations, all to the gateway ip. Every
    world name is judged on the *appliance's* border, never here.

    Every window is 24h, like ARP. The reason is that each of these is
    the member-to-appliance plumbing hop, not a world crossing: the
    engine pre-plants the memory at stream open (so the first crossing
    waits live, never freezes), and a 24h window keeps it from lapsing
    for the machine's whole life. The member should freeze only when it
    is genuinely blocked on an upstream decision, never on its own
    plumbing -- a shorter window re-freezes the hot path each time it
    lapses, and every re-freeze pays a full cryogenic thaw. Long is
    safe here because these hops carry no world authorization: the
    world name is resolved and judged at the appliance, so the window
    governs freeze frequency, not what the member may reach."""
    gw = APPLIANCE_WIRE_ADDRESS
    return (
        "# The member border (appended by titanium): the wire plane's\n"
        "# ARP and the appliance. The member's only peer is its\n"
        "# terminator; the world names are judged at the appliance.\n"
        "release outgoing arp (keep_open=24h) (skip_freeze=true)\n"
        "release incoming arp\n"
        + _round_trip(f"{gw}:443", "tcp", "24h")
        + _round_trip(f"{gw}:80", "tcp", "24h")
        + _round_trip(f"{gw}:53", "udp", "24h")
    )


def appliance_border_policy_text(world_hosts: list[str]) -> str:
    """The appliance border: the world leg titanium's engine judges by
    name. ARP; the upstream resolver; the member's reply window as exact
    destinations; and one grant per allowed world host, on 443 and 80 --
    matched against the name the appliance resolves and stamps on the
    crossing, so a rotated CDN ip is a non-issue. Everything else is
    refused, on the record."""
    lines = [
        (
            "# The appliance border (titanium's engine): the world leg,\n"
            "# judged by the resolved name. ARP, the upstream resolver,\n"
            "# the member's reply window, and each allowed world host.\n"
        ),
        "release outgoing arp (keep_open=24h) (skip_freeze=true)\n",
        "release incoming arp\n",
        _round_trip(f"{UPSTREAM_DNS}:53", "udp", "24h"),
        (
            "# The member's reply window (the consistent reply port): the\n"
            "# member pins its ephemeral ports to this range, so a crossing\n"
            "# to the member is named by one of these exact ports -- both\n"
            "# the member's request arriving (incoming, named by its source\n"
            "# port) and the appliance's answer going back (outgoing).\n"
        ),
    ]
    for port in range(REPLY_PORT_LOW, REPLY_PORT_HIGH + 1):
        for proto in ("tcp", "udp"):
            lines.append(
                f"release outgoing {MEMBER_WIRE_ADDRESS}:{port}/{proto} "
                "(keep_open=1h) (skip_freeze=true)\n"
            )
            lines.append(f"release incoming {MEMBER_WIRE_ADDRESS}:{port}/{proto}\n")
    if world_hosts:
        lines.append("# The allowed world hosts, by name.\n")
        for host in world_hosts:
            lines.append(_round_trip(f"{host}:443", "tcp", "5m"))
            lines.append(_round_trip(f"{host}:80", "tcp", "5m"))
    return "".join(lines)
