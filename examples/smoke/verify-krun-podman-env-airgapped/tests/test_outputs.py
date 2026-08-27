import json
import os
import socket
from pathlib import Path

REPORT = Path("/app/report.json")


def report():
    assert REPORT.exists(), "agent did not write /app/report.json"
    return json.loads(REPORT.read_text())


def test_report_claims():
    r = report()
    assert r["egress_tcp_denied"] is True
    assert r["egress_http"] is False
    assert int(r["uid"]) == 0
    assert r["engine_sockets"] == []
    assert r["writable_workdir"] is True
    assert r["writable_tmp"] is True
    assert r["cpu_hypervisor"] is True
    assert r["vsock_dev"] is True
    assert "eth0" not in r["net_interfaces"]
    # dns is deliberately recorded, not asserted: whether TSI's host-side
    # resolution answers on an internal network is a measured fact, and
    # either outcome is data for the probe record, not a boundary breach —
    # the boundary is the egress denial above.


# The verifier runs inside the same guest, so re-verify the
# security-relevant claims independently of the agent's report.

def test_egress_is_denied():
    # Literal IP, so no DNS in the path: the internal network must have no
    # route out. A denied route may hang rather than refuse, so timeout.
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            reached = True
    except OSError:
        reached = False
    assert reached is False, "TCP to an external IP succeeded on an air-gapped task"


def test_engine_sockets_absent():
    for s in ("/var/run/docker.sock", "/run/podman/podman.sock"):
        assert not os.path.exists(s), f"engine socket leaked into guest: {s}"


# The same in-guest microVM signals as the -www variant. In-guest
# evidence, not the gate: the host-side runtime check is authoritative.

def test_no_nic_in_the_guest():
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert "lo" in interfaces
    assert "eth0" not in interfaces


def test_cpu_reports_a_hypervisor():
    flags = [line for line in open("/proc/cpuinfo") if line.startswith("flags")]
    assert flags and all(" hypervisor" in line for line in flags)


def test_uname_is_not_gvisor():
    assert "gvisor" not in os.uname().release.lower()
