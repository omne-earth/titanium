#!/usr/bin/env bash
# Evidence probes for the relaxation table in
# docs/environments/KRUN-PODMAN.md §5.
#
# Each gVisor-lineage relaxation exists because of a runsc property. These
# probes test, on a live host, whether the property is present under the
# krun runtime. The output converts the table's pending rows into recorded
# decisions. The probes drive podman directly — no trials, no runner shim —
# and clean up everything they create.
#
# Design constraint, learned from the first run: the libkrun handler may
# not implement exec, so no probe is allowed to *depend* on exec. Exec
# support is itself a probe (the whole trial wiring rides compose exec).
# Guest-side facts travel over the container's own stdout: the main
# container runs a bootstrap that prints PROBE:<name>=<value> markers, and
# the host polls `podman logs` for them.
#
# Output discipline: `ok` means the expectation held; `find` is a finding —
# a fact contrary to expectation, recorded, never an error; `fail` is
# infrastructure only (a probe could not run). Findings exit 0.
set -uo pipefail

command -v podman >/dev/null || { echo "install podman first (make .podman)"; exit 1; }
command -v krun >/dev/null || { echo "install krun first (make .krun-podman)"; exit 1; }

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
find_() { printf '  \033[33mfind\033[0m  %s\n' "$1"; FINDINGS=$((FINDINGS+1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; INFRA=1; }
FINDINGS=0
INFRA=0

# Same base image as the egress proxy (agent_setup.py), fully qualified so
# short-name enforcing never fires.
IMAGE=docker.io/library/alpine:3.22
PFX=titanium-krunprobe
NET=$PFX-net
TMP=$(mktemp -d)

cleanup() {
  podman rm --force --time 2 $PFX-main $PFX-peer $PFX-svc $PFX-limits >/dev/null 2>&1
  podman network rm --force "$NET" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

# Poll the main container's logs for a PROBE:<marker> line; print the value
# after '=' when present. Returns non-zero on timeout.
marker() {
  local name=$1 timeout=${2:-20} i=0 line
  while [ "$i" -lt "$timeout" ]; do
    line=$(podman logs $PFX-main 2>/dev/null | grep -o "PROBE:$name\(=.*\)\?$" | tail -1)
    if [[ -n "$line" ]]; then printf '%s' "${line#PROBE:$name}" | sed 's/^=//'; return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

# Guest bootstrap: every fact a probe needs from inside, emitted as markers.
# DNS gets retries because aardvark needs a moment after network attach.
# The in.txt loop serves the cp host->guest probe; sleep keeps the guest
# alive for label inspection and the guest->host cp.
BOOTSTRAP=$(cat <<EOF
echo "PROBE:KERNEL=\$(uname -r)"
echo "PROBE:RESOLV=\$(grep ^nameserver /etc/resolv.conf 2>/dev/null | head -1)"
i=0; R=fail
while [ \$i -lt 10 ]; do nslookup $PFX-peer >/dev/null 2>&1 && { R=ok; break; }; i=\$((i+1)); sleep 1; done
echo "PROBE:DNS_PEER=\$R"
i=0; R=fail
while [ \$i -lt 10 ]; do nslookup example.com >/dev/null 2>&1 && { R=ok; break; }; i=\$((i+1)); sleep 1; done
echo "PROBE:DNS_EXT=\$R"
echo "guest-to-host" > /tmp/out.txt
echo "PROBE:OUT_WRITTEN"
i=0
while [ \$i -lt 60 ]; do
  if [ -f /tmp/in.txt ]; then echo "PROBE:IN=\$(cat /tmp/in.txt)"; break; fi
  i=\$((i+1)); sleep 1
done
sleep 300
EOF
)

echo "== setup =="
podman pull -q "$IMAGE" >/dev/null || { bad "cannot pull $IMAGE"; exit 1; }
podman network create "$NET" >/dev/null 2>&1
podman run -d --name $PFX-peer --network "$NET" "$IMAGE" sleep 300 >/dev/null \
  || { bad "crun peer container did not start"; exit 1; }
podman run -d --name $PFX-main --network "$NET" --runtime krun "$IMAGE" \
  sh -c "$BOOTSTRAP" >/dev/null \
  || { bad "krun container did not start"; exit 1; }
ok "crun peer + krun main running on $NET"

echo "== probe: runtime identity and guest kernel =="
OCI=$(podman inspect --format '{{.OCIRuntime}}' $PFX-main)
[[ "$OCI" == "krun" || "$OCI" == */krun ]] \
  && ok "OCIRuntime=$OCI (host-side, the field verification reads)" \
  || find_ "OCIRuntime=$OCI — expected krun"
if GUEST_KERNEL=$(marker KERNEL 20); then
  HOST_KERNEL=$(uname -r)
  [[ "$GUEST_KERNEL" != "$HOST_KERNEL" ]] \
    && ok "guest kernel $GUEST_KERNEL != host $HOST_KERNEL (microVM confirmed)" \
    || find_ "guest kernel equals host kernel ($GUEST_KERNEL) — no VM boundary visible"
else
  bad "no KERNEL marker from the guest within 20s — bootstrap did not run"
fi

echo "== probe: exec transport (table row 0 — the wiring rides compose exec) =="
EXEC_OUT=$(podman exec $PFX-main true 2>&1)
if [[ $? -eq 0 ]]; then
  ok "podman exec works under krun"
else
  find_ "podman exec unsupported under krun: ${EXEC_OUT:-no output} — transfers, in-sandbox probes, and agent commands all need another transport"
fi

echo "== probe: SELinux process label (table row 5) =="
MODE=$(getenforce 2>/dev/null || echo unavailable)
LABEL=$(podman inspect --format '{{.ProcessLabel}}' $PFX-main)
RUNNING=$(podman inspect --format '{{.State.Running}}' $PFX-main)
echo "        selinux=$MODE label='${LABEL:-<empty>}' running=$RUNNING"
if [[ "$MODE" == "Enforcing" ]]; then
  # Podman assigns KVM runtimes the container_kvm_t domain — confined, with
  # an MCS pair, unlike runsc's forced label=disable. Any container_* domain
  # with categories counts as labeled.
  if [[ "$RUNNING" == "true" && "$LABEL" == *container_*:*c* ]]; then
    ok "main runs confined (${LABEL##*:r:}) under Enforcing — no label=disable needed"
  elif [[ -z "$LABEL" ]]; then
    find_ "main runs with no process label under Enforcing"
  else
    find_ "unexpected label shape '$LABEL' (running=$RUNNING)"
  fi
else
  warn "host not Enforcing — label evidence is weaker on this host"
fi

echo "== probe: podman cp coherence through the rootfs (table rows 1, 2) =="
echo "host-to-guest" > "$TMP/in.txt"
if podman cp "$TMP/in.txt" $PFX-main:/tmp/in.txt 2>"$TMP/cp-err"; then
  if INSIDE=$(marker IN 20) && [[ "$INSIDE" == "host-to-guest" ]]; then
    ok "host -> running guest: cp content visible inside ($INSIDE)"
  else
    find_ "host -> running guest: cp succeeded but the guest never saw the file (virtiofs not coherent mid-run)"
  fi
else
  find_ "host -> running guest: podman cp refused: $(cat "$TMP/cp-err")"
fi
if marker OUT_WRITTEN 20 >/dev/null; then
  if podman cp $PFX-main:/tmp/out.txt "$TMP/out.txt" 2>"$TMP/cp-err" \
     && [[ "$(cat "$TMP/out.txt" 2>/dev/null)" == "guest-to-host" ]]; then
    ok "running guest -> host: mid-run write visible to cp"
  else
    find_ "running guest -> host: cp did not return the guest's write: $(cat "$TMP/cp-err" 2>/dev/null)"
  fi
else
  bad "guest never reported OUT_WRITTEN — cannot judge guest->host cp"
fi

echo "== probe: DNS through TSI (table row 3) =="
RESOLV=$(marker RESOLV 20 || echo "")
echo "        guest resolv.conf: ${RESOLV:-<none>}"
DNS_PEER=$(marker DNS_PEER 30 || echo "no-marker")
[[ "$DNS_PEER" == "ok" ]] \
  && ok "aardvark resolves the peer container name from inside krun" \
  || find_ "peer container name does not resolve from inside krun ($DNS_PEER)"
DNS_EXT=$(marker DNS_EXT 40 || echo "no-marker")
[[ "$DNS_EXT" == "ok" ]] \
  && ok "external name resolves from inside krun" \
  || find_ "external name does not resolve from inside krun ($DNS_EXT)"

echo "== probe: krun container as a reachable service (table row 4 enabler) =="
# The listener is the krun container's own main process — no exec inside
# the guest. The peer is crun, where exec is safe. IP is used so this row
# does not depend on the DNS row.
podman run -d --name $PFX-svc --network "$NET" --runtime krun "$IMAGE" \
  sh -c 'echo krun-service-ok | nc -l -p 8080; sleep 60' >/dev/null \
  || bad "krun listener container did not start"
SVC_IP=$(podman inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $PFX-svc)
REPLY=""
for _ in 1 2 3 4 5; do
  REPLY=$(podman exec $PFX-peer sh -c "nc -w 3 $SVC_IP 8080" 2>/dev/null)
  [[ -n "$REPLY" ]] && break
  sleep 1
done
[[ "$REPLY" == "krun-service-ok" ]] \
  && ok "peer connected to a listener inside krun at $SVC_IP:8080" \
  || find_ "no reply from the krun listener at ${SVC_IP:-<no ip>}:8080 (got '${REPLY:-<nothing>}')"

echo "== probe: cgroup limit enforcement, no wrapper (table row 8) =="
if podman run -d --name $PFX-limits --network none --runtime krun --cpus 0.5 -m 256m \
  "$IMAGE" sleep 300 >/dev/null; then
  CG=$(podman inspect --format '{{.State.CgroupPath}}' $PFX-limits)
  CPU=$(cat "/sys/fs/cgroup$CG/cpu.max" 2>/dev/null || echo unreadable)
  MEM=$(cat "/sys/fs/cgroup$CG/memory.max" 2>/dev/null || echo unreadable)
  echo "        cgroup=$CG cpu.max='$CPU' memory.max='$MEM'"
  [[ "$CPU" == "50000 100000" ]] \
    && ok "cpu.max enforces the declared 0.5 CPU" \
    || find_ "cpu.max is '$CPU' — expected '50000 100000'"
  [[ "$MEM" == "268435456" ]] \
    && ok "memory.max enforces the declared 256m" \
    || find_ "memory.max is '$MEM' — expected 268435456"
else
  bad "krun container with --cpus/-m did not start"
fi

echo
if [[ $INFRA -ne 0 ]]; then
  echo "some probes could not run — fix the probe infrastructure and re-run"
  exit 1
fi
echo "all probes ran with $FINDINGS finding(s); record the facts in KRUN-PODMAN.md §5"
