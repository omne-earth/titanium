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
# Design constraint, learned from the first run: the libkrun handler does
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
  podman rm --force --time 2 $PFX-main $PFX-peer $PFX-limits \
    $PFX-vmjson $PFX-vsock >/dev/null 2>&1
  podman rmi --force $PFX-socat-img >/dev/null 2>&1
  podman network rm --force "$NET" >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

# Poll a container's logs for a PROBE:<marker> line; print the value after
# '=' when present. Returns non-zero on timeout. Container defaults to the
# main probe container.
marker() {
  local name=$1 timeout=${2:-20} from=${3:-$PFX-main} i=0 line
  while [ "$i" -lt "$timeout" ]; do
    line=$(podman logs "$from" 2>/dev/null | grep -o "PROBE:$name\(=.*\)\?$" | tail -1)
    if [[ -n "$line" ]]; then printf '%s' "${line#PROBE:$name}" | sed 's/^=//'; return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

# Guest bootstrap: every fact a probe needs from inside, emitted as markers.
# The in.txt watcher runs in the background so the host's cp probe never
# waits behind the DNS retries. The listeners stay up for the service
# probes; netstat is captured after they start, because TSI-impersonated
# sockets are expected to be invisible to the guest kernel. DNS gets
# retries because aardvark needs a moment after network attach.
BOOTSTRAP=$(cat <<EOF
echo "PROBE:KERNEL=\$(uname -r)"
echo "PROBE:RESOLV=\$(grep ^nameserver /etc/resolv.conf 2>/dev/null | head -1)"
echo "PROBE:IFACES=\$(ip -o addr 2>/dev/null | tr '\n' ';')"
( i=0
  while [ \$i -lt 60 ]; do
    if [ -f /tmp/in.txt ]; then
      echo "PROBE:IN=\$(cat /tmp/in.txt)"
      echo "PROBE:IN_STAT=\$(stat -c %u:%g:%a /tmp/in.txt)"
      break
    fi
    i=\$((i+1)); sleep 1
  done ) &
mkdir -p /tmp/www /tmp/outdir
echo krun-http-ok > /tmp/www/index.html
httpd -p 8080 -h /tmp/www
( i=0; while [ \$i -lt 300 ]; do echo krun-nc-ok | nc -l -p 8082; i=\$((i+1)); done ) &
sleep 1
echo "PROBE:NETSTAT=\$(netstat -tln 2>/dev/null | grep LISTEN | tr '\n' ';')"
echo "PROBE:SELFHTTP=\$(wget -q -T 3 -O - http://127.0.0.1:8080/index.html 2>&1)"
echo "guest-to-host" > /tmp/out.txt
echo "uid-1000-content" > /tmp/out-uid.txt
chown 1000:1000 /tmp/out-uid.txt
echo "dir-content" > /tmp/outdir/f1
echo "PROBE:OUT_WRITTEN"
i=0; R=fail
while [ \$i -lt 10 ]; do nslookup $PFX-peer >/dev/null 2>&1 && { R=ok; break; }; i=\$((i+1)); sleep 1; done
echo "PROBE:DNS_PEER=\$R"
i=0; R=fail
while [ \$i -lt 10 ]; do nslookup example.com >/dev/null 2>&1 && { R=ok; break; }; i=\$((i+1)); sleep 1; done
echo "PROBE:DNS_EXT=\$R"
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

echo "== probe: podman cp as the exec-free transfer path (table rows 0, 1, 2) =="
# The staging pipeline runs cp/chown inside the sandbox via exec. Without
# exec, podman cp must carry transfers alone, so these probes cover the
# semantics that pipeline provided: content both ways against a running
# guest, ownership as the guest sees uploads, and ownership as the host
# receives exports — root-written, non-root-written, and a directory.
echo "host-to-guest" > "$TMP/in.txt"
if podman cp "$TMP/in.txt" $PFX-main:/tmp/in.txt 2>"$TMP/cp-err"; then
  if INSIDE=$(marker IN 30) && [[ "$INSIDE" == "host-to-guest" ]]; then
    ok "host -> running guest: cp content visible inside ($INSIDE)"
    IN_STAT=$(marker IN_STAT 10 || echo none)
    [[ "$IN_STAT" == "0:0:"* ]] \
      && ok "upload lands root-owned in the guest (uid:gid:mode $IN_STAT)" \
      || find_ "upload ownership in the guest is $IN_STAT — expected root-owned"
  else
    find_ "host -> running guest: cp succeeded but the guest never saw the file (virtiofs not coherent mid-run)"
  fi
else
  find_ "host -> running guest: podman cp refused: $(cat "$TMP/cp-err")"
fi
if marker OUT_WRITTEN 30 >/dev/null; then
  if podman cp $PFX-main:/tmp/out.txt "$TMP/out.txt" 2>"$TMP/cp-err" \
     && [[ "$(cat "$TMP/out.txt" 2>/dev/null)" == "guest-to-host" ]]; then
    OWNER=$(stat -c %U "$TMP/out.txt")
    ok "running guest -> host: mid-run root-written export arrives, owned by $OWNER"
    [[ "$OWNER" == "$(id -un)" ]] \
      || find_ "export owner is $OWNER, not the invoking user — the download contract needs a chown step"
  else
    find_ "running guest -> host: cp did not return the guest's write: $(cat "$TMP/cp-err" 2>/dev/null)"
  fi
  if podman cp $PFX-main:/tmp/out-uid.txt "$TMP/out-uid.txt" 2>/dev/null \
     && [[ "$(cat "$TMP/out-uid.txt" 2>/dev/null)" == "uid-1000-content" ]]; then
    ok "non-root (uid 1000) guest write exports, owned by $(stat -c %U:%G "$TMP/out-uid.txt") on the host"
  else
    find_ "non-root guest write did not export cleanly"
  fi
  if podman cp $PFX-main:/tmp/outdir "$TMP/outdir" 2>/dev/null \
     && [[ "$(cat "$TMP/outdir/f1" 2>/dev/null)" == "dir-content" ]]; then
    ok "directory export works (outdir/f1 intact)"
  else
    find_ "directory export failed or arrived incomplete"
  fi
else
  bad "guest never reported OUT_WRITTEN — cannot judge guest->host cp"
fi

echo "== probe: guest network shape under TSI =="
IFACES=$(marker IFACES 10 || echo none)
echo "        guest interfaces: $IFACES"
if [[ "$IFACES" == *dummy0* && "$IFACES" != *eth0* ]]; then
  ok "no NIC in the guest (lo + dummy0 placeholder) — TSI socket impersonation confirmed"
elif [[ -z "$IFACES" || "$IFACES" == none ]]; then
  bad "guest reported no interface list — probe could not read the network shape"
else
  find_ "guest has a network interface beyond lo/dummy0: $IFACES"
fi
NETSTAT=$(marker NETSTAT 10 || echo none)
[[ -z "$NETSTAT" || "$NETSTAT" == "none" ]] \
  && ok "guest netstat shows no LISTEN sockets while listeners run — sockets live host-side" \
  || find_ "guest netstat sees its own listeners: $NETSTAT"

echo "== probe: DNS through TSI (table row 3) =="
RESOLV=$(marker RESOLV 20 || echo "")
echo "        guest resolv.conf: ${RESOLV:-<none>}"
DNS_PEER=$(marker DNS_PEER 40 || echo "no-marker")
[[ "$DNS_PEER" == "ok" ]] \
  && ok "aardvark resolves the peer container name from inside krun" \
  || find_ "peer container name does not resolve from inside krun ($DNS_PEER) — TSI resolves host-side, bypassing aardvark names"
DNS_EXT=$(marker DNS_EXT 60 || echo "no-marker")
[[ "$DNS_EXT" == "ok" ]] \
  && ok "external name resolves from inside krun" \
  || find_ "external name does not resolve from inside krun ($DNS_EXT)"

echo "== probe: krun container as a reachable service (table row 4 enabler) =="
# Three separate facts, so a busybox-nc quirk cannot masquerade as a TSI
# limitation: does TCP connect at all, does data flow through a one-shot
# nc listener, does data flow through a real accepting server (httpd).
# Plus the guest's own loopback view of its server. IP is used so none of
# this depends on the DNS row.
MAIN_IP=$(podman inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $PFX-main)
CONNECT=$(podman exec $PFX-peer sh -c "nc -z -w 3 $MAIN_IP 8080 2>&1; echo rc=\$?")
[[ "$CONNECT" == *rc=0* ]] \
  && ok "TCP connect to the krun guest succeeds ($MAIN_IP:8080)" \
  || find_ "TCP connect to the krun guest fails: $CONNECT"
HTTP=$(podman exec $PFX-peer sh -c "wget -q -T 3 -O - http://$MAIN_IP:8080/index.html" 2>/dev/null)
[[ "$HTTP" == "krun-http-ok" ]] \
  && ok "httpd inside krun serves a peer (data path works)" \
  || find_ "httpd inside krun returned '${HTTP:-<nothing>}' to a peer — connect opens but data does not flow"
NCDATA=$(podman exec $PFX-peer sh -c "nc -w 3 $MAIN_IP 8082" 2>/dev/null)
[[ "$NCDATA" == "krun-nc-ok" ]] \
  && ok "one-shot nc listener inside krun delivers data to a peer" \
  || find_ "nc listener inside krun delivered '${NCDATA:-<nothing>}' — nc semantics differ under TSI"
SELFHTTP=$(marker SELFHTTP 20 || echo "no-marker")
[[ "$SELFHTTP" == "krun-http-ok" ]] \
  && ok "guest reaches its own server over loopback" \
  || find_ "guest loopback fetch of its own server returned '${SELFHTTP}'"

echo "== probe: vsock control channel (row 0 candidate) =="
# libkrun maps guest vsock ports to host unix sockets (krun_add_vsock_port);
# TSI itself rides vsock, so the channel exists in this stack. Whether the
# crun-krun handler exposes it to an OCI container is the question. Facts
# collected: what this build's installed docs say about vsock and the
# .krun_vm.json config surface; whether the handler honors .krun_vm.json
# at all (VM sizing knobs, observable from the guest); whether the guest
# kernel exposes /dev/vsock; whether guest userspace can bind and connect
# AF_VSOCK (socat, built into a probe image).
echo "        krun --version: $(krun --version 2>/dev/null | head -1)"
DOCS=$(rpm -qd crun-krun crun 2>/dev/null)
if [[ -n "$DOCS" ]]; then
  KDOC=$(zgrep -l -i 'krun_vm' $DOCS 2>/dev/null | head -1)
  VDOC=$(zgrep -h -i 'vsock' $DOCS 2>/dev/null | head -3)
  [[ -n "$KDOC" ]] \
    && ok "installed docs describe the .krun_vm.json surface ($KDOC)" \
    || find_ "installed docs do not mention .krun_vm.json"
  if [[ -n "$VDOC" ]]; then
    ok "installed docs mention vsock:"
    echo "        $VDOC"
  else
    find_ "installed docs never mention vsock — no documented mapping surface in crun-krun $(krun --version 2>/dev/null | grep -oE '[0-9.]+' | head -1)"
  fi
else
  warn "crun-krun not rpm-managed here — no installed docs to consult"
fi

printf '{"width":1,"ram_mib":333}\n' > "$TMP/krun_vm.json"
podman create --name $PFX-vmjson --network none --runtime krun "$IMAGE" sh -c \
  'echo "PROBE:NPROC=$(nproc)"; echo "PROBE:MEMTOTAL=$(grep MemTotal /proc/meminfo)"; echo "PROBE:VSOCKDEV=$(ls /dev/vsock 2>&1)"; sleep 60' >/dev/null \
  && podman cp "$TMP/krun_vm.json" $PFX-vmjson:/.krun_vm.json \
  && podman start $PFX-vmjson >/dev/null
if [[ $? -eq 0 ]]; then
  NPROC=$(marker NPROC 20 $PFX-vmjson || echo none)
  MEMT=$(marker MEMTOTAL 10 $PFX-vmjson || echo none)
  MEMKB=$(echo "$MEMT" | grep -oE '[0-9]+' | head -1); MEMKB=${MEMKB:-0}
  echo "        guest sizing: nproc=$NPROC $MEMT (host nproc=$(nproc))"
  # Judged per key: a partially honored file is a different fact from an
  # ignored one.
  if [[ "$MEMKB" -gt 250000 && "$MEMKB" -lt 450000 ]]; then
    ok ".krun_vm.json ram_mib honored (asked 333, guest MemTotal ${MEMKB}kB)"
  else
    find_ ".krun_vm.json ram_mib not honored (asked 333, guest MemTotal ${MEMKB}kB)"
  fi
  [[ "$NPROC" == "1" ]] \
    && ok ".krun_vm.json width honored (nproc=1)" \
    || find_ ".krun_vm.json width not honored (asked 1, guest nproc=$NPROC)"
  VSOCKDEV=$(marker VSOCKDEV 10 $PFX-vmjson || echo none)
  [[ "$VSOCKDEV" == "/dev/vsock" ]] \
    && ok "guest exposes /dev/vsock" \
    || find_ "no /dev/vsock in the guest ($VSOCKDEV)"
else
  bad "could not stage .krun_vm.json into a created krun container"
fi

mkdir -p "$TMP/socat-img"
printf 'FROM %s\nRUN apk add --no-cache socat\n' "$IMAGE" > "$TMP/socat-img/Dockerfile"
if podman build -q -t $PFX-socat-img "$TMP/socat-img" >/dev/null 2>&1; then
  podman run -d --name $PFX-vsock --network none --runtime krun $PFX-socat-img sh -c '
timeout 2 socat VSOCK-LISTEN:1234 - </dev/null >/dev/null 2>/tmp/e
echo "PROBE:VSOCKBIND=rc=$? err=$(head -1 /tmp/e)"
socat -u - VSOCK-CONNECT:2:9999 </dev/null >/dev/null 2>/tmp/e2
echo "PROBE:VSOCKCONN=rc=$? err=$(head -1 /tmp/e2)"
sleep 60' >/dev/null
  VSOCKBIND=$(marker VSOCKBIND 20 $PFX-vsock || echo none)
  # A held listener dies by the probe's own timeout: GNU timeout exits 124,
  # busybox timeout TERM-kills the child (rc=143, socat reports signal 15).
  # An address-family failure surfaces immediately with a socat E line.
  if [[ "$VSOCKBIND" == rc=124* || ( "$VSOCKBIND" == rc=143* && "$VSOCKBIND" == *"signal 15"* ) ]]; then
    ok "guest userspace binds AF_VSOCK (listener held until the probe timeout)"
  else
    find_ "guest AF_VSOCK bind: $VSOCKBIND"
  fi
  VSOCKCONN=$(marker VSOCKCONN 20 $PFX-vsock || echo none)
  echo "        guest connect to host CID 2 (unmapped port, expect refusal): $VSOCKCONN"
else
  bad "could not build the socat probe image (needs egress)"
fi

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
