"""The agent line: a router guest between the task and the world.

A real agent lives inside the sealed task guest and always needs a
line to its inference API -- the assumption every titanium rung makes
(``filtered_egress``). On the cella rung the line is cella's own E3
pattern, a forwarding topology: the task guest gets a **wire** to a
router guest; the router holds the world nic and forwards exactly the
agent's API traffic. Every hop is judged -- "a forwarding topology
costs a judged crossing at every membrane it traverses, deliberately"
(cella docs/NETWORK-MODEL.md) -- so the line itself is on the record
at three membranes, and an airgapped task keeps the strongest reading
of ``allow_internet = false``: with no world nic on the task guest,
task egress is impossible by topology, not merely refused.

The router is titanium's appliance: a converted guest whose one baked
job is tinyproxy with the agent's allowlist. Name-level enforcement
("api.anthropic.com, that's it") lives in the proxy today; cella's
roadmap (DNS as a parkable operation, the appliance pair) formalizes
it later. The router's own ``cella.policy`` stays coarse on purpose --
DNS and 443 to the world -- because API endpoints rotate IPs, and the
names are the proxy's to hold.

Addressing: cella's world plane is kernel-autoconfigured by cella; a
wire "imposes no address convention", so this module imposes one:
router ``10.77.0.1/24``, task ``10.77.0.2/24``, configured by
systemd-networkd entries in each guest's boot layer.
"""

from __future__ import annotations

from titanium.environments.cella.boot_layer import (
    BootEntry,
    GuestFile,
    GuestSymlink,
)

# The wire's address convention (ours: cella imposes none).
ROUTER_WIRE_ADDRESS = "10.77.0.1"
TASK_WIRE_ADDRESS = "10.77.0.2"
_WIRE_PREFIX = 24

# The proxy the agent's HTTP(S)_PROXY points at.
PROXY_PORT = 8888

# The resolver the router uses. One exact address, granted exactly.
ROUTER_RESOLVER = "1.1.1.1"

# The router's build file: the same provisioning path as every task
# image (no init in the base; the debian-systemd policy runs), plus
# tinyproxy. Its config and allowlist arrive as boot-layer entries, so
# one router image serves every trial.
ROUTER_DOCKERFILE = """\
# Titanium's cella agent-line router: tinyproxy between the wire and
# the world, forwarding only the agent's allowlisted API domains.
FROM docker.io/library/debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
    tinyproxy iproute2 \\
    && rm -rf /var/lib/apt/lists/*
"""


def router_policy_text() -> str:
    """The router's world-membrane grants, coarse on purpose.

    Names are the proxy's job: API endpoints rotate IPs, so the
    membrane grants DNS to the pinned resolver and 443 to the world,
    and tinyproxy's allowlist decides which names ride it. Every
    crossing is still parked, judged, and on the record.
    """
    return (
        "# The agent line's membranes -- one policy, both planes. Every\n"
        "# hot path carries a keep_open window with skip_freeze on the\n"
        "# outgoing leg, so the gateway waits live instead of freezing:\n"
        "# what keeps a real API peer's TLS inside its patience.\n"
        "#\n"
        "# The wire side: the task peer reaching the proxy, and the\n"
        "# proxy replying. An incoming crossing is named by its source\n"
        "# (the task's address, an ephemeral port): wildcard port, both\n"
        "# ways.\n"
        "release outgoing arp (keep_open=24h) (skip_freeze=true)\n"
        "release incoming arp (keep_open=24h)\n"
        f"release outgoing {TASK_WIRE_ADDRESS}:*/tcp (keep_open=5m) (skip_freeze=true)\n"
        f"release incoming {TASK_WIRE_ADDRESS}:*/tcp (keep_open=5m)\n"
        "# The world side: DNS to the pinned resolver, and 443 out.\n"
        "# Coarse on purpose -- the proxy holds the names, the membrane\n"
        "# holds the record and remembers each concrete IP it resolves.\n"
        f"release outgoing {ROUTER_RESOLVER}:53/udp (keep_open=90s) (skip_freeze=true)\n"
        f"release incoming {ROUTER_RESOLVER}:53/udp (keep_open=90s)\n"
        "release outgoing *:443/tcp (keep_open=5m) (skip_freeze=true)\n"
        "release incoming *:443/tcp (keep_open=5m)\n"
    )


def line_grants_text() -> str:
    """The task guest's wire grants: the peer's proxy port, both ways,
    and ARP for the wire plane. Appended to the task's own policy."""
    return (
        "\n# The agent line (appended by titanium): the wire peer's\n"
        "# proxy, and the wire plane's ARP. Live on the outgoing leg so\n"
        "# the task guest does not freeze reaching its own line.\n"
        "release outgoing arp (keep_open=24h) (skip_freeze=true)\n"
        "release incoming arp (keep_open=24h)\n"
        f"release outgoing {ROUTER_WIRE_ADDRESS}:{PROXY_PORT}/tcp "
        "(keep_open=5m) (skip_freeze=true)\n"
        f"release incoming {ROUTER_WIRE_ADDRESS}:{PROXY_PORT}/tcp (keep_open=5m)\n"
    )


def wire_up_commands(interface: str, address: str) -> str:
    """Shell lines that address a wire nic with ip(8), idempotently.
    Deterministic and dependency-light on purpose: the wire has no
    kernel autoconfiguration (cella supplies ``ip=`` for world nics
    only), and provisioning guarantees iproute2.
    """
    return (
        f"ip link set {interface} up || true\n"
        f"ip addr replace {address}/{_WIRE_PREFIX} dev {interface} || true\n"
    )


def router_entries(allowed_domains: list[str]) -> list[BootEntry]:
    """Everything the router guest needs beyond its image: the wire
    address (eth1: the world nic is eth0 and kernel-configured), the
    resolver, tinyproxy's config and unit, and the domain allowlist.

    tinyproxy's filter file holds one pattern per line
    (``FilterType fnmatch``) and ``FilterDefaultDeny Yes`` refuses
    every unmatched host. An exact domain matches itself; a
    leading-dot suffix (harbor's other allowlist form) matches the
    bare domain and every subdomain.
    """
    tinyproxy_conf = (
        f"Port {PROXY_PORT}\n"
        f"Listen {ROUTER_WIRE_ADDRESS}\n"
        f"Allow {TASK_WIRE_ADDRESS}\n"
        "Timeout 600\n"
        "MaxClients 32\n"
        'Filter "/etc/tinyproxy/filter"\n'
        "FilterDefaultDeny Yes\n"
        "FilterType fnmatch\n"
        "ConnectPort 443\n"
    )
    filter_lines = []
    for domain in allowed_domains:
        bare = domain.lstrip(".")
        if domain.startswith("."):
            filter_lines.append(f"*.{bare}")
            filter_lines.append(bare)
        else:
            filter_lines.append(bare)
    unit = (
        "[Unit]\n"
        "Description=Titanium agent-line proxy\n"
        "After=network.target\n\n"
        "[Service]\n"
        # The wire nic is eth1 (the world nic is eth0, kernel-
        # configured by cella); a wire has no autoconfiguration, so
        # the unit addresses it before the proxy listens on it.
        "ExecStartPre=/usr/sbin/ip link set eth1 up\n"
        f"ExecStartPre=/usr/sbin/ip addr replace "
        f"{ROUTER_WIRE_ADDRESS}/{_WIRE_PREFIX} dev eth1\n"
        "ExecStart=/usr/bin/tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf\n"
        "Restart=always\n"
        "RestartSec=1\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return [
        GuestFile(
            path="/etc/resolv.conf",
            contents=f"nameserver {ROUTER_RESOLVER}\n".encode(),
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestFile(
            path="/etc/tinyproxy/tinyproxy.conf",
            contents=tinyproxy_conf.encode(),
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestFile(
            path="/etc/tinyproxy/filter",
            contents=("\n".join(filter_lines) + "\n").encode(),
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestFile(
            path="/etc/systemd/system/titanium-line-proxy.service",
            contents=unit.encode(),
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestSymlink(
            path=(
                "/etc/systemd/system/multi-user.target.wants/"
                "titanium-line-proxy.service"
            ),
            target="../titanium-line-proxy.service",
            uid=0,
            gid=0,
        ),
    ]


def proxy_env() -> dict[str, str]:
    """The variables that point an in-guest agent at its line."""
    address = f"http://{ROUTER_WIRE_ADDRESS}:{PROXY_PORT}"
    return {
        "HTTP_PROXY": address,
        "HTTPS_PROXY": address,
        "http_proxy": address,
        "https_proxy": address,
        "NO_PROXY": "localhost,127.0.0.1",
    }
