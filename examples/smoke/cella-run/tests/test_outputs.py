"""Verifier pins for the cella-run inner probe.

The report is read as evidence. The claim under test is containment: the probe
ran inside a docker container, and the container boundary held from the inside.
"""

import json
from pathlib import Path

REPORT = Path("/app/report.json")


def _report():
    return json.loads(REPORT.read_text())


def test_report_is_valid_json():
    _report()


def test_ran_in_a_container():
    # The whole premise: the inner run put the probe in a docker container.
    assert _report()["in_container"] is True


def test_host_root_not_reachable():
    # No host filesystem bind-mounted in. The boundary held.
    assert _report()["host_root_reachable"] is False


def test_pid1_is_not_host_init():
    # A shared PID namespace with the guest would surface systemd here. The
    # contained workload must see its own entrypoint, not the guest's init.
    assert _report()["pid1_comm"] != "systemd"


def test_egress_denied():
    # This task declares allow_internet = false, so the container has no route
    # out. Recorded as a boundary fact, not the escape claim.
    assert _report()["egress_tcp_denied"] is True
