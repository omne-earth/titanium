#!/bin/bash
set -uo pipefail

python3 - <<'PY'
import json, os, socket

def probe(fn, default=False):
    try:
        return fn()
    except Exception:
        return default

def writable(d):
    p = os.path.join(d, ".probe")
    try:
        open(p, "w").write("x")
        os.remove(p)
        return True
    except OSError:
        return False

def egress_tcp_denied():
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            return False
    except OSError:
        return True

r = {
    "egress_tcp_denied": egress_tcp_denied(),
    "dns": probe(lambda: bool(socket.getaddrinfo("example.com", 443))),
    "uid": os.getuid(),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "writable_workdir": writable("/app"),
    "writable_tmp": writable("/tmp"),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "cpu_hypervisor": probe(lambda: any(
        line.startswith("flags") and " hypervisor" in line
        for line in open("/proc/cpuinfo"))),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "mem_total_kb": probe(lambda: int(next(
        l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]), 0),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
