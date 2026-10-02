#!/bin/bash
# The inner probe, cella flavor. It runs inside a cella VM that itself runs
# inside a cella VM (the cella-runner). It reads the VM boundary from the
# inside and reports what it reached; a boundary probe, not a break.
set -uo pipefail

python3 - <<'PY'
import json, os, socket


def probe(fn, default=False):
    try:
        return fn()
    except Exception:
        return default


def egress_tcp_denied():
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            return False
    except OSError:
        return True


r = {
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "cpu_hypervisor": probe(lambda: any(
        line.startswith("flags") and " hypervisor" in line
        for line in open("/proc/cpuinfo"))),
    # The inner guest hosts no guests: no /dev/kvm one level further down.
    "kvm_device": os.path.exists("/dev/kvm"),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "egress_tcp_denied": egress_tcp_denied(),
    "uid": os.getuid(),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
