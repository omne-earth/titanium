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
    assert r["cpu_hypervisor"] is True
    assert r["vsock_dev"] is True
    assert int(r["nproc"]) == 1
    assert 900_000 <= int(r["mem_total_kb"]) <= 1_250_000
    assert "eth0" not in r["net_interfaces"]
    assert "lo" in r["net_interfaces"]


# The verifier runs inside the same guest, so re-verify the
# security-relevant claims independently of the agent's report.

def test_engine_sockets_absent():
    for s in ("/var/run/docker.sock", "/run/podman/podman.sock"):
        assert not os.path.exists(s), f"engine socket leaked into guest: {s}"


def test_dns_and_egress():
    socket.getaddrinfo("example.com", 443)
    rc = subprocess.run(
        ["curl", "-fsS", "--max-time", "20", "https://example.com"],
        capture_output=True,
    ).returncode
    assert rc == 0


# Three independent in-guest signals that a KVM microVM, not the host
# kernel and not Sentry, serves this workload. In-guest evidence, not the
# gate: the environment's host-side runtime check is authoritative.

def test_no_nic_in_the_guest():
    # TSI impersonates sockets host-side; the guest has no NIC. lo plus a
    # dummy placeholder only, never a veth/eth device.
    interfaces = sorted(os.listdir("/sys/class/net"))
    assert "lo" in interfaces
    assert "eth0" not in interfaces


def test_cpu_reports_a_hypervisor():
    flags = [line for line in open("/proc/cpuinfo") if line.startswith("flags")]
    assert flags and all(" hypervisor" in line for line in flags)


def test_guest_is_sized_to_the_task_declaration():
    # task.toml declares cpus = 1 and memory_mb = 1024; the environment
    # sizes the microVM with krun.cpus / krun.ram_mib annotations, so
    # the guest must see exactly that, not the host's cores.
    assert os.cpu_count() == 1
    mem_kb = int(next(
        l for l in open('/proc/meminfo') if l.startswith('MemTotal')).split()[1])
    assert 900_000 <= mem_kb <= 1_250_000


def test_uname_is_not_gvisor():
    # A real guest kernel, not Sentry's fiction.
    assert "gvisor" not in os.uname().release.lower()
