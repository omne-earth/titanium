#!/bin/bash
set -uo pipefail

python3 - <<'PY'
import json, os, socket, subprocess

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
    "egress_http": probe(lambda: subprocess.run(
        ["curl", "-fsS", "--max-time", "10", "https://example.com"],
        capture_output=True).returncode == 0),
    "dns": probe(lambda: bool(socket.getaddrinfo("example.com", 443))),
    "uid": os.getuid(),
    "engine_sockets": [s for s in ("/var/run/docker.sock", "/run/podman/podman.sock")
                       if os.path.exists(s)],
    "writable_workdir": writable("/app"),
    "writable_tmp": writable("/tmp"),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "cpu_hypervisor": probe(lambda: any(
        line.startswith("flags") and " hypervisor" in line
        for line in open("/proc/cpuinfo"))),
    "vsock_dev": os.path.exists("/dev/vsock"),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "mem_total_kb": probe(lambda: int(next(
        l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]), 0),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
