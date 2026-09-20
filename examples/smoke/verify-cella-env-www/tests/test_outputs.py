import json
import os
import time
import urllib.request
from pathlib import Path

REPORT = Path("/app/report.json")


def report():
    assert REPORT.exists(), "agent did not write /app/report.json"
    return json.loads(REPORT.read_text())


def test_report_claims():
    r = report()
    assert r["granted_https_ok"] is True
    assert r["denied_https_blocked"] is True
    assert r["resolver_is_gateway"] is True
    # The verifier holds the observed address against the machine's
    # own resolution and the gateway it knows: the literal lives here,
    # never in the brief.
    import socket
    assert r["resolver_address"] == "10.77.0.1"
    assert socket.gethostbyname("example.com") == "10.77.0.1"
    assert r["pid1_comm"] == "systemd"
    assert int(r["uid"]) == 0
    assert int(r["nproc"]) == 1
    assert 700_000 <= int(r["mem_total_kb"]) <= 1_100_000


def test_the_warming_curve_is_present_and_live_after_the_cold_call():
    # Three sequential calls to the same granted name: all succeed, and
    # the standing memory makes the second and third run live -- so they
    # are not slower than the cold first call, which paid the first-call pauses.
    # Each call carries its guest-perceived seconds and the guest wall
    # clock it was measured at (environment-paused; paired with the audit book's
    # host_ns it reveals the frozen time).
    r = report()
    calls = r["calls"]
    assert len(calls) == 3, calls
    for c in calls:
        assert isinstance(c["secs"], (int, float))
        assert isinstance(c["measured_at"], (int, float))
    cold, warm2, warm3 = (c["secs"] for c in calls)
    # Warmed calls ride remembered destinations; allow a small margin.
    assert warm2 <= cold + 1 and warm3 <= cold + 1, f"no warming: {calls}"


def _https_ok(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def test_the_grant_releases_by_name_and_the_refusal_holds():
    # Re-verified on the verifier's own member machine, independent of
    # the report: the granted name completes a pair-CA-verified TLS
    # handshake through the gateway (retried across the boot and the
    # the first-call pauses), the ungranted one does not.
    deadline = time.monotonic() + 240
    granted = False
    while time.monotonic() < deadline and not granted:
        granted = _https_ok("https://example.com/", timeout=60)
        if not granted:
            time.sleep(5)
    assert granted, "the granted name example.com was not released"
    assert not _https_ok("https://example.org/", timeout=60), (
        "an ungranted name reached the world"
    )
