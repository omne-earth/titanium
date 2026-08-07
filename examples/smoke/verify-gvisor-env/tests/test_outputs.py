import json
import os
import socket
import subprocess
from pathlib import Path

REPORT = Path("/app/report.json")


def report():
    assert REPORT.exists(), "agent did not write /app/report.json"
    return json.loads(REPORT.read_text())


def test_report_claims():
    r = report()
    assert r["egress_http"] is True
    assert r["dns"] is True
    assert int(r["uid"]) == 0
    assert r["engine_sockets"] == []
    assert r["writable_workdir"] is True
    assert r["writable_tmp"] is True
    assert r["sysrq_writable"] is False
    assert r["dmesg_gvisor"] is True


# The verifier runs inside the same sandbox, so re-verify the
# security-relevant claims independently of the agent's report.

def test_engine_sockets_absent():
    for s in ("/var/run/docker.sock", "/run/podman/podman.sock"):
        assert not os.path.exists(s), f"engine socket leaked into sandbox: {s}"


def test_dns_and_egress():
    socket.getaddrinfo("example.com", 443)
    rc = subprocess.run(
        ["curl", "-fsS", "--max-time", "20", "https://example.com"],
        capture_output=True,
    ).returncode
    assert rc == 0


def test_sysrq_boundary_holds():
    # gVisor returns EIO on the write (surfaced at flush, so use `with`); the
    # sentry does not pass sysrq through to the host kernel.
    try:
        with open("/proc/sysrq-trigger", "w") as f:
            f.write("h")
        writable = True
    except OSError:
        writable = False
    assert writable is False


# Two independent in-sandbox signals that the sentry, not the host kernel,
# serves this sandbox. In-sandbox evidence, not the gate: the environment's
# host-side runtime check is authoritative.

def test_dmesg_reports_gvisor():
    out = subprocess.run(["dmesg"], capture_output=True, text=True).stdout
    assert "gvisor" in out.lower()


def test_uname_reports_gvisor():
    assert "gvisor" in os.uname().release.lower()
