#!/usr/bin/env bash
# cella-runner.sh -- the reflexive runner (docs/runners/CELLA-RUNNER.md).
#
# Usage: cella-runner.sh <inner-task> <inner-env> [jobs-dir]
#
#   <inner-task>      repo-relative path of a task with a task.toml
#   <inner-env>       the environment the inner run drives in the guest:
#                     docker or cella (CELLA-RUNNER.md §2)
#   jobs-dir          the home for this runner's jobs (default:
#                     ./.run/cella-runner/<inner-task-basename>). Each run lands
#                     in its own <jobs-dir>/<YYYY-MM-DD__HH-MM-SS>, the job name
#                     titanium mints for a trial, so runs never overwrite each
#                     other and a cella-runner job lists beside the other smokes'.
#
# It bakes the whole tracked workspace into one Cella micro-VM, boots it so a
# systemd oneshot runs `titanium run --env <inner-env>` against <inner-task>
# inside the sealed guest, waits for the guest's forced reset, and extracts the
# payload. An escape from the inner environment lands in the guest, never on
# the host.
#
# This is the judged-network path: the guest is a member of a terminated pair
# (CELLA-RUNNER.md §3, §7). It stands its own appliance (the terminator, holding
# the world) beside the reflexive guest, one engine.py pump per border, and a
# cella-engine bridge relaying each machine's parks to its pump. The membrane
# grants the agent's inference line only; every other crossing is refused on
# the record. This supervisor owns both pumps and reaps them itself -- they
# are its children, not a separately started service.
#
#   exit 0  the run completed and the payload was extracted
#   exit 1  a real failure
#   exit 2  a precondition is missing, so nothing ran
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DRIVER="$HERE/cella_runner_convert.py"
PY="$ROOT/.venv/bin/python"

INNER_TASK="${1:-}"
INNER_ENV="${2:-}"
[ -n "$INNER_TASK" ] && [ -n "$INNER_ENV" ] \
    || { echo "usage: cella-runner.sh <inner-task> <inner-env> [jobs-dir]" >&2; exit 2; }
INNER_BASENAME="$(basename "$INNER_TASK")"
JOBS_DIR="${3:-$ROOT/.run/cella-runner/$INNER_BASENAME}"
# One job per run, named as titanium names a trial's job (JobConfig.job_name).
JOB="$(date +%Y-%m-%d__%H-%M-%S)"
OUT="$JOBS_DIR/$JOB"

FLAVOR="titanium-cella-runner-$INNER_BASENAME"
# The machine name is part of the translator's edge.sock path, which must fit
# sockaddr_un (~108 chars). Keep it short: a tag plus the pid, not the full
# task path. The FLAVOR (rootfs dir) is not in any socket path, so it stays
# descriptive.
_TAG="$(printf '%s' "$INNER_BASENAME" | tr -cd 'a-z0-9' | cut -c1-8)"
VM="cr-$_TAG-$$"
EXT4_BYTES="${TITANIUM_CELLA_RUNNER_EXT4_BYTES:-10737418240}"   # 10 GiB: workspace + inner image layers
# The guest's memory ceiling is the inner environment's (InnerEnv.guest_mem_mb
# in the driver): the cella inner machines' --mem-mb comes out of it.
case "$INNER_ENV" in
    cella) _MEM_DEFAULT=6144 ;;
    *)     _MEM_DEFAULT=4096 ;;
esac
GUEST_MEM_MB="${TITANIUM_CELLA_RUNNER_MEM_MB:-$_MEM_DEFAULT}"
# The wait for the guest's reset. A nested run boots a VM inside the VM, and
# each of its machines pays mkfs, create and a one-vCPU boot in minutes.
case "$INNER_ENV" in
    cella) _TIMEOUT_DEFAULT=10800 ;;
    *)     _TIMEOUT_DEFAULT=2700 ;;
esac
BOOT_TIMEOUT_SECS="${TITANIUM_CELLA_RUNNER_TIMEOUT:-$_TIMEOUT_DEFAULT}"
# The inner agent. Its build-time install (uv, PyPI) is baked on the host,
# since the membrane grants the inference line only.
AGENT="${TITANIUM_CELLA_RUNNER_AGENT:-mini-swe-agent}"
# Keep the guest rootfs in the payload (cella-env/rootfs.ext4.zst) so the run
# reproduces from an image, not just a digest -- the env-cella proof's
# rootfs-source.tar analogue. On by default; set false for fast local
# iteration, where the ~10 GiB image and its compression are dead weight.
KEEP_ROOTFS="${CELLA_RUNNER_KEEP_ROOTFS:-true}"
# /var/tmp, not /tmp: the reflexive bake (the workspace, a venv, docker layers,
# a multi-GB ext4) outgrows a tmpfs /tmp, and podman then fails mid-build with
# "disk quota exceeded". /var/tmp is disk-backed on every mainstream layout.
WORKDIR_BASE="${TITANIUM_CELLA_WORKDIR:-/var/tmp}"

WORK=""
BIN=""
M=""
# The terminated pair, as the cella environment stands it: the member (the
# reflexive guest) on a wire, and the terminator appliance holding the world.
# Each border has its own engine.py pump and bridge.
APPLIANCE="cra-$$"
WIRE="crw$$"
M_PUMP_PID=""; M_BRIDGE_PID=""
A_PUMP_PID=""; A_BRIDGE_PID=""; A_THAW_PID=""

# Collection vs enforcement on the appliance border: dry-run releases every
# world crossing and records it for review (CELLA.md §3.1).
DRY_RUN="${CELLA_RUNNER_DRY_RUN:-false}"

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
pass() { echo "PASS: $*"; }
skip() { echo; echo "SKIP: $* -- nothing ran"; exit 2; }
fail() { echo; echo "FAIL: $*"; exit 1; }

# Only what this script created: its two machines, their pumps, bridges, and
# the appliance thaw loop, inside its own CELLA_HOME and mktemp directory.
# Never the operator's ~/.cella. Order matches the environment's reap: bridges
# first, pumps next, machines last. No pump outlives its border.
teardown() {
    for p in "$M_BRIDGE_PID" "$A_BRIDGE_PID" "$A_THAW_PID" "$M_PUMP_PID" "$A_PUMP_PID"; do
        [ -n "$p" ] && kill "$p" 2>/dev/null
    done
    if [ -n "$BIN" ]; then
        for vm in "$VM" "$APPLIANCE"; do
            [ -d "${CELLA_HOME:-}/machines/$vm" ] || continue
            "$BIN" stop "$vm" >/dev/null 2>&1
            "$BIN" destroy "$vm" >/dev/null 2>&1
        done
    fi
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap teardown EXIT

# ------------------------------------------------------------------ step 0

step "step 0: preconditions"

# The inner environment is a runner choice, checked before anything is stood.
case "$INNER_ENV" in
    docker|cella) ;;
    *) skip "unknown inner environment '$INNER_ENV' (docker|cella)" ;;
esac
[ -x "$PY" ] || skip "no interpreter at $PY -- run: make sync"
[ -f "$ROOT/$INNER_TASK/task.toml" ] || skip "no task at $INNER_TASK ($INNER_TASK/task.toml not found)"
[ -c /dev/kvm ] || skip "no /dev/kvm -- cella needs KVM (bare metal or nested virt)"

PODMAN="${TITANIUM_PODMAN_BIN:-podman}"
command -v "$PODMAN" >/dev/null 2>&1 \
    || skip "$PODMAN is not on PATH -- run: make .podman, or set TITANIUM_PODMAN_BIN"

# Cella is a peer tool: found, never built.
BIN="${CELLA_BIN:-$(command -v cella 2>/dev/null)}"
[ -n "$BIN" ] && [ -x "$BIN" ] || skip "no cella CLI: set CELLA_BIN, or put cella on PATH"
note "cella:  $BIN"

# Flavor. The field flavor is production: it opens no console, so the guest
# is unobservable by design. The lab flavor writes the guest's console.log
# and is for debugging only (`make smoke-cella-runner-docker-debug`, which sets
# CELLA_RUNNER_DEBUG=true). A lab cella reaching this script without that flag
# is a mistake -- production runs must not ship on an observed guest.
LAB=false
case "$("$BIN" doctor check 2>/dev/null | grep -E 'flavor:')" in
    *"the lab"*) LAB=true ;;
esac
if [ "$LAB" = "true" ] && [ "${CELLA_RUNNER_DEBUG:-false}" != "true" ]; then
    skip "cella at $BIN is the lab flavor; production runs use the field cella. For a debug run: make smoke-cella-runner-docker-debug"
fi
[ "$LAB" = "true" ] && note "flavor: the lab (debug run; the guest console is recorded)"

# Cella's own preflight verb names any unmet precondition on its own stdout.
REAL_CELLA_HOME="${CELLA_HOME:-$HOME/.cella}"
CELLA_HOME="$REAL_CELLA_HOME" "$BIN" doctor gate kvm bwrap golden:kernel:canonical \
    || skip "cella doctor gate reported the unmet precondition above"

# ------------------------------------------------------------------ step 1

step "step 1: a disposable CELLA_HOME"
note "job: $OUT"

[ -d "$WORKDIR_BASE" ] || skip "TITANIUM_CELLA_WORKDIR=$WORKDIR_BASE does not exist"
# Short subdir names: the CELLA_HOME path is a prefix of the translator's
# edge.sock, which must fit sockaddr_un (~108 chars).
WORK="$(mktemp -d "$WORKDIR_BASE/cr.XXXXXX")" \
    || skip "could not create a work directory under $WORKDIR_BASE"
chmod 0755 "$WORK"
export CELLA_HOME="$WORK/h"
M="$CELLA_HOME/machines/$VM"
# The world translator's socket path must fit sockaddr_un. Fail clearly here
# rather than as a cryptic "socket path too long" at start.
EDGE_SOCK="$M/edge.sock"
if [ "${#EDGE_SOCK}" -ge 108 ]; then
    skip "edge.sock path is ${#EDGE_SOCK} chars (limit 108): set TITANIUM_CELLA_WORKDIR to a shorter path"
fi
mkdir -p "$CELLA_HOME/kernel/canonical" "$CELLA_HOME/bin"
chmod 0755 "$CELLA_HOME/kernel" "$CELLA_HOME/kernel/canonical" "$CELLA_HOME/bin"
note "CELLA_HOME: $CELLA_HOME (disposable; not your ~/.cella, reaped at exit)"
note "watch:      CELLA_HOME=$CELLA_HOME $BIN list"
note "            CELLA_HOME=$CELLA_HOME $BIN gateway $VM show"
note "            tail -f $WORK/pump-appliance.log   # every world crossing, judged by name"
note "            tail -f $WORK/pump-member.log      # the member border's own judgments"
note "            tail -f $M/vmm.log                 # the member VMM: parks, releases, the exit"
note "            $BIN --dump $M/network/ledger      # the chronicle: every crossing, decoded (also audit, verdict)"

# The reflexive rootfs export is multi-GB (the workspace, docker, a full
# titanium venv). Route the converter's tempdir into this run's workdir, so
# one knob -- TITANIUM_CELLA_WORKDIR -- governs all the space this run needs,
# and a small /tmp tmpfs never bounds the bake.
export TMPDIR="$WORK/tmp"
mkdir -p "$TMPDIR"

# cella's own `extract` (step 4) runs a helper VM that defaults to the
# canonical kernel, so that golden must be staged too -- copied, never built.
mkdir -p "$CELLA_HOME/kernel/canonical"
chmod 0755 "$CELLA_HOME/kernel/canonical"
cp "$REAL_CELLA_HOME/kernel/canonical/bzImage" "$CELLA_HOME/kernel/canonical/" \
    || fail "could not copy the canonical kernel golden"
cp "$REAL_CELLA_HOME/kernel/canonical/golden.json" "$CELLA_HOME/kernel/canonical/" 2>/dev/null

# The guest kernel is titanium's own, one per inner environment (built by
# scripts/cella-runner/build-kernel.sh, `make .cella-runner-kernel-<env>`), not a cella
# golden. cella boots any bzImage that carries the virtio-mmio boot configs;
# it checks only that the file exists. Stage it as the "container" flavor.
CELLA_RUNNER_KERNEL="${CELLA_RUNNER_KERNEL:-${XDG_CACHE_HOME:-$HOME/.cache}/titanium/cella-runner-kernel/$INNER_ENV/bzImage}"
[ -f "$CELLA_RUNNER_KERNEL" ] || skip "no $INNER_ENV guest kernel at $CELLA_RUNNER_KERNEL -- run: make .cella-runner-kernel-$INNER_ENV"
mkdir -p "$CELLA_HOME/kernel/container"
chmod 0755 "$CELLA_HOME/kernel/container"
cp "$CELLA_RUNNER_KERNEL" "$CELLA_HOME/kernel/container/bzImage" \
    || fail "could not stage the container kernel"

# `cella extract` (step 4) runs a helper VM off the "cella" golden rootfs, so
# that golden must live in this disposable home too. Copied, never built here.
# The terminator golden is the appliance's rootfs (and carries the pair CA
# the driver bakes into the member). Both copied, never built here.
mkdir -p "$CELLA_HOME/rootfs"
for g in cella terminator; do
    [ -d "$REAL_CELLA_HOME/rootfs/$g" ] \
        || skip "no '$g' golden rootfs in $REAL_CELLA_HOME/rootfs -- run: make .cella"
    cp -r "$REAL_CELLA_HOME/rootfs/$g" "$CELLA_HOME/rootfs/" \
        || fail "could not copy the $g golden"
done

# Stage the whole persona set into the sandbox home, as the field install does:
# bwrap binds the VMM as the machine's sub-uid, which a checkout under a 0710
# home would refuse at source resolution.
find "$(dirname "$BIN")" -maxdepth 1 -type f -name 'cella*' -perm -u+x \
    -exec cp -p {} "$CELLA_HOME/bin/" \;
BIN="$CELLA_HOME/bin/$(basename "$BIN")"

# ------------------------------------------------------------------ step 2

step "step 2: bake the reflexive rootfs"
note "The whole tracked workspace, then titanium provisioned inside it. This"
note "builds images and runs a package manager, so it needs egress. The VM it"
note "produces does not."

PYTHONPATH="$ROOT/src" "$PY" "$DRIVER" \
    --workspace "$ROOT" \
    --inner-task "$INNER_TASK" \
    --context "$WORK/context" \
    --home "$CELLA_HOME" \
    --flavor "$FLAVOR" \
    --size-bytes "$EXT4_BYTES" \
    --policy-dir "$WORK/policy" \
    --agent "$AGENT" \
    --inner-env "$INNER_ENV" \
    || fail "the reflexive bake failed"

# ------------------------------------------------------------------ step 3

step "step 3: boot the terminated pair"
note "member $VM on wire $WIRE; appliance $APPLIANCE (terminator) holds the world"
note "two borders, two engine.py pumps; appliance dry-run=$DRY_RUN"

# cella writes its own diagnostics to stdout, so capture them: a failed verb
# must show why, not vanish into /dev/null.
cella_verb() {
    if ! "$BIN" "$@" >>"$WORK/cella.log" 2>&1; then
        echo "--- cella $* failed; cella.log tail ---"
        tail -15 "$WORK/cella.log" | sed 's/^/   /'
        fail "cella $1 failed"
    fi
}

# One border's judge: an engine.py pump on a per-run ephemeral port, then the
# bridge that relays the machine's parks to it. The bridge does not retry, so
# it dials only once the pump listens. Sets PUMP_PID and BRIDGE_PID.
stand_border() {  # <machine> <policy> <tag> [--dry-run]
    local machine="$1" policy="$2" tag="$3"; shift 3
    local port
    port="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    PYTHONPATH="$ROOT/src" "$PY" -m titanium.environments.cella.engine \
        --listen "127.0.0.1:$port" --policy "$policy" "$@" > "$WORK/pump-$tag.log" 2>&1 &
    PUMP_PID=$!
    for _ in $(seq 1 100); do
        "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1',$port))==0 else 1)" && break
        kill -0 "$PUMP_PID" 2>/dev/null || fail "the $tag pump exited before listening: $(tail -3 "$WORK/pump-$tag.log")"
        sleep 0.1
    done
    "$(dirname "$BIN")/cella-engine" "$machine" --dial "127.0.0.1:$port" \
        > "$WORK/bridge-$tag.log" 2>&1 &
    BRIDGE_PID=$!
}

# The appliance first: it holds the world and is the member's resolver.
cella_verb create "$APPLIANCE" --kernel canonical --rootfs terminator \
    --mem-mb 512 --net "world,wire:$WIRE" --root rw
cella_verb start "$APPLIANCE"
cella_verb gateway "$APPLIANCE" open
if [ "$DRY_RUN" = "true" ]; then
    cp "$WORK/policy/appliance.policy" "$WORK/collected.policy"
    stand_border "$APPLIANCE" "$WORK/collected.policy" appliance --dry-run
else
    stand_border "$APPLIANCE" "$WORK/policy/appliance.policy" appliance
fi
A_PUMP_PID=$PUMP_PID; A_BRIDGE_PID=$BRIDGE_PID
# The appliance parks on flows it forwards; thaw it for its whole life, as
# the environment's _thaw_forever does.
A_DIR="$CELLA_HOME/machines/$APPLIANCE"
( while :; do
      [ -f "$A_DIR/state" ] && "$BIN" thaw "$APPLIANCE" >/dev/null 2>&1
      sleep 0.5
  done ) &
A_THAW_PID=$!

# The member: the reflexive guest, wire-only, its border fixed.
cella_verb create "$VM" --kernel container --rootfs "$FLAVOR" \
    --mem-mb "$GUEST_MEM_MB" --net "wire:$WIRE" --root rw
cella_verb start "$VM"
cella_verb gateway "$VM" open
stand_border "$VM" "$WORK/policy/member.policy" member
M_PUMP_PID=$PUMP_PID; M_BRIDGE_PID=$BRIDGE_PID

VMM_PID="$(cat "$M/pid" 2>/dev/null)"
note "member vmm pid=${VMM_PID:-unknown}; pumps member=$M_PUMP_PID appliance=$A_PUMP_PID"
note "the inner run is the boot: waiting up to ${BOOT_TIMEOUT_SECS}s for its reset"

# Completion is two host-side facts: the VMM pid is gone and no frozen state
# file exists. Every judged crossing parks (freezes) the machine, so the wait
# pumps thaws through them -- the agent's inference line freezes here.
deadline=$(( $(date +%s) + BOOT_TIMEOUT_SECS ))
reason=""
while :; do
    if [ -f "$M/state" ]; then
        "$BIN" thaw "$VM" >/dev/null 2>&1
        sleep 1; continue
    fi
    CUR_PID="$(cat "$M/pid" 2>/dev/null)"
    if [ -z "$CUR_PID" ] || ! kill -0 "$CUR_PID" 2>/dev/null; then
        [ -f "$M/state" ] && continue
        break   # reset: pid gone, no frozen state
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        reason="the inner run did not reset within ${BOOT_TIMEOUT_SECS}s"; break
    fi
    sleep 2
done

# Keep the border evidence before any fail: the work directory is reaped.
#
# Payload shape matches a `make smoke-cella` trial (environment.py): each
# machine's audit books land under cella-chronicle/<machine>/ (with cella's
# own --dump rendering the decodable ones to .txt beside the raw bytes), and
# its pump and bridge logs under cella-engine/<machine>/ as engine.log and
# edge.log. So a cella-runner proof reads the same as an env-cella proof.
CHRONICLE_FILES="network/ledger network/names verdict audit membrane-memory manifest.json vmm.log valve uid"
CHRONICLE_DUMPABLE="network/ledger network/names verdict audit membrane-memory"
preserve_chronicle() {   # <machine-name> <machine-dir>
    local name="$1" dir="$2" rel dest
    for rel in $CHRONICLE_FILES; do
        [ -f "$dir/$rel" ] || continue
        dest="$OUT/cella-chronicle/$name/$rel"
        mkdir -p "$(dirname "$dest")"
        cp "$dir/$rel" "$dest" 2>/dev/null || continue
        case " $CHRONICLE_DUMPABLE " in
            *" $rel "*) "$BIN" --dump "$dest" > "$dest.txt" 2>/dev/null || rm -f "$dest.txt" ;;
        esac
    done
    # Lab flavor only: the guest console, kept beside the chronicle it explains.
    if [ "$LAB" = "true" ] && [ -f "$dir/console.log" ]; then
        mkdir -p "$OUT/cella-chronicle/$name"
        cp "$dir/console.log" "$OUT/cella-chronicle/$name/console.log" 2>/dev/null || true
    fi
}
preserve_engine() {   # <machine-name> <border-tag>
    local name="$1" tag="$2"
    mkdir -p "$OUT/cella-engine/$name"
    cp "$WORK/pump-$tag.log" "$OUT/cella-engine/$name/engine.log" 2>/dev/null || true
    cp "$WORK/bridge-$tag.log" "$OUT/cella-engine/$name/edge.log" 2>/dev/null || true
}
keep_borders() {
    preserve_chronicle "$VM" "$M"
    preserve_chronicle "$APPLIANCE" "$CELLA_HOME/machines/$APPLIANCE"
    preserve_engine "$VM" member
    preserve_engine "$APPLIANCE" appliance
}
# The guest rootfs titanium built for this run: a single ext4 published under
# the flavor dir, present until teardown reaps $WORK. It is a sparse ~10 GiB
# image, so zstd (which collapses the free space) is the form that is kept,
# with the flavor's golden.json manifest beside it. This is the reproducible
# artifact -- the env-cella proof keeps rootfs-source.tar for the same reason.
preserve_rootfs() {
    [ "$KEEP_ROOTFS" = "true" ] || return 0
    local flavor_dir="$CELLA_HOME/rootfs/$FLAVOR" out="$OUT/cella-env"
    [ -f "$flavor_dir/rootfs.ext4" ] || { note "no rootfs to keep at $flavor_dir"; return 0; }
    mkdir -p "$out"
    cp "$flavor_dir/golden.json" "$out/golden.json" 2>/dev/null || true
    if zstd -q -f -T0 -19 --long=27 --sparse "$flavor_dir/rootfs.ext4" -o "$out/rootfs.ext4.zst"; then
        note "guest rootfs kept: $out/rootfs.ext4.zst ($(du -h "$out/rootfs.ext4.zst" | cut -f1))"
    else
        note "zstd failed; keeping the raw ext4 instead"
        cp "$flavor_dir/rootfs.ext4" "$out/rootfs.ext4" 2>/dev/null || true
    fi
}
mkdir -p "$OUT"
if [ -n "$reason" ]; then
    keep_borders
    echo "--- vmm.log (tail) ---"; tail -30 "$M/vmm.log" 2>/dev/null | sed 's/^/   /'
    if [ "$LAB" = "true" ]; then
        echo "--- console.log (tail) ---"; tail -60 "$M/console.log" 2>/dev/null | sed 's/^/   /'
    fi
    # The guest's own record of how far it got (phases.log, run.log, the
    # inner trial so far) is on its disk; stop it and take it, best effort,
    # so a stall leaves evidence and not only a deadline.
    "$BIN" stop "$VM" >/dev/null 2>&1
    if "$BIN" extract "$VM" /titanium > "$WORK/payload.tar" 2>"$WORK/extract.err" \
        && tar -C "$OUT" --delay-directory-restore -xf "$WORK/payload.tar" 2>/dev/null; then
        note "partial payload extracted to $OUT (the run did not complete)"
        [ -f "$OUT/titanium/result/phases.log" ] && { echo "--- phases.log ---"; sed 's/^/   /' "$OUT/titanium/result/phases.log"; }
    fi
    fail "$reason"
fi
pass "the inner run reset the machine (completion signal)"

# ------------------------------------------------------------------ step 4

step "step 4: extract the payload"

mkdir -p "$OUT"
if "$BIN" extract "$VM" /titanium > "$WORK/payload.tar" 2>"$WORK/extract.err"; then
    tar -C "$OUT" --delay-directory-restore -xf "$WORK/payload.tar" 2>/dev/null \
        || note "payload tar extracted with warnings"
    pass "payload extracted to $OUT"
    # The reset alone does not prove completion: panic=1 reboot=t turns a
    # guest kernel panic into the same reset. Only the markers the run
    # script writes after titanium exits separate the two.
    if [ ! -f "$OUT/titanium/result/done" ] || [ ! -f "$OUT/titanium/result/exit-code" ]; then
        keep_borders
        [ -f "$OUT/titanium/result/phases.log" ] && { echo "--- phases.log ---"; sed 's/^/   /' "$OUT/titanium/result/phases.log"; }
        fail "the guest reset without its completion markers (done, exit-code) -- a crash, not a finish"
    fi
    inner_rc=$(cat "$OUT/titanium/result/exit-code")
    note "inner titanium exit code: $inner_rc"
    if [ "$inner_rc" != "0" ]; then
        keep_borders
        fail "inner titanium exited $inner_rc"
    fi
else
    echo "--- extract stderr ---"; sed 's/^/   /' "$WORK/extract.err"
    fail "cella extract /titanium failed"
fi

# The border evidence: what each pump judged, what each VMM parked, and on a
# lab run the guest consoles. The work directory is reaped, so this is the
# only copy that survives the run.
keep_borders
preserve_rootfs
# On collection, hand back the crossings the engine observed, for review before
# they are checked in as grants (CELLA.md §3.1).
mkdir -p "$OUT/policy" && cp "$WORK/policy"/*.policy "$OUT/policy/" 2>/dev/null
if [ "$DRY_RUN" = "true" ] && [ -f "$WORK/collected.policy" ]; then
    cp "$WORK/collected.policy" "$OUT/collected.policy"
    note "collected policy written to $OUT/collected.policy -- review, then fold its hosts into INFERENCE_HOSTS in scripts/cella_runner_convert.py"
fi

echo
echo "cella-runner complete: $INNER_TASK ran under $INNER_ENV inside a sealed cella VM; payload in $OUT"
