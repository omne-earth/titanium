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

def sysrq():
    try:
        # `with` so the flush (where gVisor raises EIO) happens inside the try;
        # a bare open().write() buffers and the error escapes at GC close
        with open("/proc/sysrq-trigger", "w") as f:
            f.write("h")
        return True
    except OSError:
        return False

r = {
    "dns": probe(lambda: bool(socket.getaddrinfo("example.com", 443))),
    "egress_http": probe(lambda: subprocess.run(
        ["curl", "-fsS", "--max-time", "20", "https://example.com"],
        capture_output=True).returncode == 0),
    "uid": os.getuid(),
    "engine_sockets": [s for s in ("/var/run/docker.sock", "/run/podman/podman.sock")
                       if os.path.exists(s)],
    "writable_workdir": writable("/app"),
    "writable_tmp": writable("/tmp"),
    "sysrq_writable": sysrq(),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "dmesg_gvisor": probe(lambda: "gvisor" in subprocess.run(
        ["dmesg"], capture_output=True, text=True).stdout.lower()),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
