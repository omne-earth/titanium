"""Verifier pins for the cella-runner inner probe, cella flavor.

The report is read as evidence. The claim under test is depth: the probe ran
in a whole VM (its own init, a hypervisor above it), that VM hosts no VM of
its own, and it has no route out.
"""

import json
import os
from pathlib import Path

REPORT = Path("/app/report.json")


def _report():
    return json.loads(REPORT.read_text())


def test_report_is_valid_json():
    _report()


def test_pid1_is_systemd():
    # A whole VM with the distro's own init, put there by the provisioning
    # policy on the host -- not a container entrypoint.
    assert _report()["pid1_comm"] == "systemd"
    assert Path("/proc/1/comm").read_text().strip() == "systemd"


def test_under_a_hypervisor():
    assert _report()["cpu_hypervisor"] is True


def test_hosts_no_guests():
    # The depth stops at the inner guest: it carries no KVM of its own.
    assert _report()["kvm_device"] is False
    assert not os.path.exists("/dev/kvm")


def test_no_nic_exists():
    # allow_internet=false is a topology: --net none, loopback only.
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert interfaces == ["lo"]
    assert _report()["net_interfaces"] == interfaces


def test_egress_denied():
    assert _report()["egress_tcp_denied"] is True
