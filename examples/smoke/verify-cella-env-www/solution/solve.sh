#!/bin/bash
set -uo pipefail

python3 - <<'PY'
import json, os, socket, time

def probe(fn, default=False):
    try:
        return fn()
    except Exception:
        return default

def connect_ok(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def granted_with_retries():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if connect_ok("1.1.1.1", 443, timeout=10):
            return True
        time.sleep(2)
    return False

r = {
    "granted_tcp_ok": granted_with_retries(),
    "denied_tcp_blocked": not connect_ok("8.8.8.8", 443, timeout=10),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "kernel_ip_config": probe(
        lambda: "ip=192.168.210.2" in open("/proc/cmdline").read()),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "uid": os.getuid(),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "mem_total_kb": probe(lambda: int(next(
        l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]), 0),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
