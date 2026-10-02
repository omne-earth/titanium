#!/bin/bash
# The inner probe. It runs inside a docker container that itself runs inside a
# sealed cella VM (the cella-runner runner). It reads the container boundary from
# the inside and reports what it reached. It attempts nothing destructive: a
# boundary probe, not a break. Even a boundary that gave way would land the
# probe in the cella guest, never on the host.
set -uo pipefail

python3 - <<'PY'
import json, os, socket


def probe(fn, default=False):
    try:
        return fn()
    except Exception:
        return default


def in_container():
    # Two independent signals a workload is containerized.
    if os.path.exists("/.dockerenv"):
        return True
    return probe(lambda: "docker" in open("/proc/1/cgroup").read()
                 or "containerd" in open("/proc/1/cgroup").read())


def egress_tcp_denied():
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            return False
    except OSError:
        return True


r = {
    # PID 1's name. A shared PID namespace would show the host's own init; a
    # contained workload sees its container entrypoint.
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "in_container": in_container(),
    # The classic escape tell: the host filesystem bind-mounted in. A contained
    # workload sees only its own image, so this stays false.
    "host_root_reachable": os.path.exists("/host") or os.path.exists("/hostfs"),
    "uid": os.getuid(),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "egress_tcp_denied": egress_tcp_denied(),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
