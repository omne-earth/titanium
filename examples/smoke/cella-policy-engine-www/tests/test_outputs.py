import json
import os
import socket
import time
from pathlib import Path

REPORT = Path("/app/report.json")


def report():
    assert REPORT.exists(), "agent did not write /app/report.json"
    return json.loads(REPORT.read_text())


def test_report_claims():
    r = report()
    assert r["granted_tcp_ok"] is True
    assert r["denied_tcp_blocked"] is True
    assert int(r["uid"]) == 0
    assert r["pid1_comm"] == "systemd"
    assert int(r["nproc"]) == 1


def test_a_nic_exists():
    # allow_internet=true is the judged world nic: the guest HAS a
    # network, unlike the airgapped variant.
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert len(interfaces) > 1 and "lo" in interfaces
    assert report()["net_interfaces"] == interfaces


def _connect_ok(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_the_grant_releases_and_the_refusal_holds():
    # Re-verified in-guest, independent of the agent's report, on the
    # verifier's own judged machine: the granted destination connects
    # (retried across the boot race), the ungranted one does not.
    deadline = time.monotonic() + 60
    granted = False
    while time.monotonic() < deadline and not granted:
        granted = _connect_ok("1.1.1.1", 443, timeout=10)
        if not granted:
            time.sleep(2)
    assert granted, "the granted crossing 1.1.1.1:443 was not released"
    assert not _connect_ok("8.8.8.8", 443, timeout=10), (
        "an ungranted crossing reached 8.8.8.8:443"
    )
