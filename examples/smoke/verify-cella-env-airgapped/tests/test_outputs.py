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
    assert r["pid1_comm"] == "systemd"
    assert int(r["uid"]) == 0
    assert r["writable_workdir"] is True
    assert r["writable_tmp"] is True
    assert r["cpu_hypervisor"] is True
    assert int(r["nproc"]) == 1
    assert 700_000 <= int(r["mem_total_kb"]) <= 1_100_000
    # The declared 4096 MB capacity, minus ext4 overhead.
    assert 3_500_000 <= int(r["fs_total_kb"]) <= 4_300_000
    assert r["root_device_is_vda"] is True


def test_topology_is_a_valid_airgap():
    """The airgap has two valid shapes, and only two.

    Agentless: no nic at all (``--net none``) -- loopback alone.
    Agented: the terminated pair stands so the agent can reach its
    inference line, and the member's one interface is the wire to the
    appliance. The workload's airgap claim is then not "no nic" but
    "no world beyond the judged appliance line", which
    ``test_egress_is_denied`` asserts independently.
    """
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert interfaces in (["lo"], ["eth0", "lo"]), (
        f"unexpected interfaces for an air-gapped task: {interfaces}"
    )
    assert report()["net_interfaces"] == interfaces


def test_egress_is_denied():
    # Re-verified in-guest, independent of the agent's report.
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=5):
            reached = True
    except OSError:
        reached = False
    assert reached is False, "TCP to an external IP succeeded on an air-gapped task"
