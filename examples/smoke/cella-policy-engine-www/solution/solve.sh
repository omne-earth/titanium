#!/bin/bash
set -uo pipefail

# The terminated pair (allow_internet=true): the member reaches the
# world only through the terminator appliance, by NAME. A granted name
# resolves to the appliance, whose minted leaf verifies against the
# pair CA titanium folded into the trust bundle; an ungranted name is
# refused on the appliance's world-leg border. Names, not ips: the
# terminator reads the SNI, so the probe must speak TLS (a bare TCP
# connect carries no name and the appliance cannot route it).
#
# The probe times three sequential calls to the same granted name. This
# makes the membrane-memory warming visible: the first call pays a
# deep-thaw freeze at each first crossing (upstream DNS, the world leg),
# and the standing memory makes the second and third run live.
python3 - <<'PY'
import json, os, socket, time, urllib.request

def probe(fn, default=False):
    try:
        return fn()
    except Exception:
        return default

def timed_get(url, timeout):
    # Default context: verifies the terminator's minted leaf against the
    # system trust bundle, into which the prelude folded the pair CA.
    # measured_at is the guest wall clock (time.time), and secs is guest
    # elapsed (time.monotonic) -- BOTH cryogenic: deep thaw restores them
    # across a freeze, so from inside the VM they exclude the frozen
    # time. Pairing measured_at against the host clock cella stamps on
    # every event (host_ns in the preserved audit book) is what reveals
    # how long the guest slept frozen: host_elapsed - guest secs.
    at = round(time.time(), 3)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            ok = r.status < 500
    except Exception:
        ok = False
    return ok, {"measured_at": at, "secs": round(time.monotonic() - t0, 2)}

url = "https://example.com/"
# Retry until the first success rides across the boot and the first
# crossings; the successful attempt's own time is the cold call.
first_ok, first_c = False, None
deadline = time.monotonic() + 240
while time.monotonic() < deadline and not first_ok:
    first_ok, first_c = timed_get(url, timeout=60)
    if not first_ok:
        time.sleep(5)

# Two more immediately: the destinations are remembered now, so these
# run live -- markedly faster than the cold call.
second_ok, second_c = timed_get(url, timeout=60) if first_ok else (False, None)
third_ok, third_c = timed_get(url, timeout=60) if first_ok else (False, None)

r = {
    "granted_https_ok": first_ok and second_ok and third_ok,
    # The warming curve: each call's guest-perceived seconds and the
    # guest wall clock it was measured at (cold, then two live).
    "calls": [first_c, second_c, third_c],
    # An ungranted name: the appliance refuses its world leg.
    "denied_https_blocked": not timed_get("https://example.org/", timeout=60)[0],
    # The interceptor answers every name with the appliance's address.
    "resolver_is_appliance": probe(
        lambda: socket.gethostbyname("example.com") == "10.77.0.1"),
    "pid1_comm": probe(lambda: open("/proc/1/comm").read().strip(), ""),
    "uid": os.getuid(),
    "nproc": probe(lambda: os.cpu_count(), 0),
    "mem_total_kb": probe(lambda: int(next(
        l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]), 0),
    "kernel_release": probe(lambda: os.uname().release, ""),
}

json.dump(r, open("/app/report.json", "w"), indent=2)
print(json.dumps(r, indent=2))
PY
