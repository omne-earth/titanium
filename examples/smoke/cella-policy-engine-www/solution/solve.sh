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
    # The address arrives via systemd-networkd and each new flow waits
    # one park-judge-release round trip, so the first attempts may
    # race the boot. Bounded retries, short per-attempt timeouts.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if connect_ok("1.1.1.1", 443, timeout=10):
            return True
        time.sleep(2)
    return False

r = {
    "granted_tcp_ok": granted_with_retries(),
    "denied_tcp_blocked": not connect_ok("8.8.8.8", 443, timeout=10),
    "dns": probe(lambda: bool(socket.getaddrinfo("example.com", 443))),
    "uid": os.getuid(),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
