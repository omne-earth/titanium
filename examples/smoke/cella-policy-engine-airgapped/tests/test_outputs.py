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
    assert int(r["uid"]) == 0
    assert r["writable_workdir"] is True
    assert r["writable_tmp"] is True
    assert r["cpu_hypervisor"] is True
    assert int(r["nproc"]) == 1
    assert 700_000 <= int(r["mem_total_kb"]) <= 1_100_000
    # dns is recorded, not asserted: with no nic there is nothing to
    # resolve through, but how the resolver fails is data.


def test_pid1_is_systemd():
    # The conversion's whole claim: the distro's own systemd is PID 1,
    # put there by the provisioning policy at build time.
    assert report()["pid1_comm"] == "systemd"
    assert Path("/proc/1/comm").read_text().strip() == "systemd"


def test_no_nic_exists():
    # allow_internet=false is a topology, not a firewall: --net none
    # means no interface at all beyond loopback.
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert "lo" in interfaces
    assert "eth0" not in interfaces
    assert report()["net_interfaces"] == interfaces


def test_egress_is_denied():
    # Re-verified in-guest, independent of the agent's report. A
    # literal IP, so no DNS in the path; a missing route may hang
    # rather than refuse, so timeout.
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            reached = True
    except OSError:
        reached = False
    assert reached is False, "TCP to an external IP succeeded on an air-gapped task"
