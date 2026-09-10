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

def fs_total_kb():
    s = os.statvfs("/")
    return s.f_frsize * s.f_blocks // 1024

r = {
    "egress_tcp_denied": egress_tcp_denied(),
    "net_interfaces": probe(lambda: sorted(os.listdir("/sys/class/net")), []),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "uid": os.getuid(),
    "writable_workdir": writable("/app"),
    "writable_tmp": writable("/tmp"),
    "cpu_hypervisor": probe(lambda: any(
        line.startswith("flags") and " hypervisor" in line
        for line in open("/proc/cpuinfo"))),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "mem_total_kb": probe(lambda: int(next(
        l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]), 0),
    "fs_total_kb": probe(fs_total_kb, 0),
    "root_device_is_vda": probe(
        lambda: "root=/dev/vda" in open("/proc/cmdline").read()),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
