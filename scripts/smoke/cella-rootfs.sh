#!/usr/bin/env bash
# `make smoke-cella-rootfs`: the acceptance proof for Titanium's Cella rootfs
# conversion. No runner, no agent, no `titanium run`, no control plane.
#
#   Titanium converts environment/Containerfile into a systemd-bootable ext4
#       -> cella create --net none    the guest gets no network
#       -> cella start                the guest proves PID 1 is systemd
#       -> cella freeze
#       -> cella thaw
#       -> cella stop                 archive refuses a running machine
#       -> cella archive
#       -> cella destroy
#       -> PASS
#
# This orchestrates and asserts; it does not re-implement Cella's diagnostics.
# Cella's verbs print their own errors, `doctor gate` names its own unmet
# preconditions, and the freeze/thaw/archive checks below are Cella's own
# evidence rather than new ones.
#
#   exit 0  the proof passed
#   exit 1  the proof failed -- a real regression
#   exit 2  a precondition is missing, so nothing was proven
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENVIRONMENT_DIR="$HERE/cella-rootfs/environment"
DRIVER="$HERE/cella_rootfs_convert.py"
PY="$ROOT/.venv/bin/python"

FLAVOR="titanium-smoke-rootfs"
VM="titanium-smoke-rootfs-$$"
PROBE_MARKER="TITANIUM_CELLA_ROOTFS_PROBE"
EXT4_BYTES=2147483648
GUEST_MEM_MB=1024

BOOT_TIMEOUT_SECS="${TITANIUM_CELLA_BOOT_TIMEOUT:-240}"
# /tmp is a tmpfs on many hosts, and this smoke puts the rootfs.ext4, the
# disk.img copied from it, and a freeze's ram.img in it. All sparse, but on a
# tmpfs they are RAM. Any world-traversable directory works.
WORKDIR_BASE="${TITANIUM_CELLA_WORKDIR:-/tmp}"

WORK=""
BIN=""
M=""

step() { echo; echo "--- $* ---"; }
note() { echo "     $*"; }
pass() { echo "PASS: $*"; }
skip() { echo; echo "SKIP: $* -- nothing was proven"; exit 2; }
fail() { echo; echo "FAIL: $*"; exit 1; }

# Only what this script created: its own machine, inside its own CELLA_HOME,
# inside its own mktemp directory. Never the operator's ~/.cella, never a
# podman prune, never a git clean.
teardown() {
    if [ -n "$BIN" ] && [ -d "${CELLA_HOME:-}/machines/$VM" ]; then
        "$BIN" stop "$VM" >/dev/null 2>&1
        "$BIN" destroy "$VM" >/dev/null 2>&1
    fi
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap teardown EXIT

# ------------------------------------------------------------------ step 0

step "step 0: preconditions"

[ -x "$PY" ] || skip "no interpreter at $PY -- run: make sync"

PODMAN="${TITANIUM_PODMAN_BIN:-podman}"
command -v "$PODMAN" >/dev/null 2>&1 \
    || skip "$PODMAN is not on PATH -- run: make .podman, or set TITANIUM_PODMAN_BIN"
note "podman: $(command -v "$PODMAN")"

# Cella is a peer tool: found, never built. Building it here would compile
# whatever happens to be in someone's Cella worktree, and this integration's
# whole claim is that it works against unchanged Cella.
BIN="${CELLA_BIN:-$(command -v cella 2>/dev/null)}"
[ -n "$BIN" ] && [ -x "$BIN" ] \
    || skip "no cella CLI: set CELLA_BIN, or put cella on PATH"
note "cella:  $BIN"

# The lab flavor is a hard requirement. A release cella opens no console.log
# (cella_libs::machine, "the console exists only in the lab"), so the guest's
# own output -- the entire boot proof -- would be unreadable. Checked here so
# that shows up now rather than as a boot timeout in four minutes.
FLAVOR_LINE="$("$BIN" doctor check 2>/dev/null | grep -E 'flavor:')"
case "$FLAVOR_LINE" in
    *"the lab"*) note "flavor:${FLAVOR_LINE#*flavor:}" ;;
    *) skip "cella at $BIN is the field flavor: it writes no console.log, so
      the guest cannot be observed. Point CELLA_BIN at a lab-flavor build" ;;
esac

# Cella's own preflight verb. It names whatever is unmet on its own stdout, so
# this does not capture or restate it. The kernel golden is the operator's; it
# is read from the real home and copied into the sandbox below, never modified.
REAL_CELLA_HOME="${CELLA_HOME:-$HOME/.cella}"
CELLA_HOME="$REAL_CELLA_HOME" "$BIN" doctor gate kvm bwrap golden:kernel:canonical \
    || skip "cella doctor gate reported the unmet precondition above"

# ------------------------------------------------------------------ step 1

step "step 1: a disposable CELLA_HOME"

[ -d "$WORKDIR_BASE" ] || skip "TITANIUM_CELLA_WORKDIR=$WORKDIR_BASE does not exist"
WORK="$(mktemp -d "$WORKDIR_BASE/titanium-cella-rootfs.XXXXXX")" \
    || skip "could not create a work directory under $WORKDIR_BASE"
# The permission fixes this script owns, and only those. Cella's spawn grants
# the machine's sub-uid execute-only ACLs on CELLA_HOME and CELLA_HOME/machines
# and a full ACL on the machine directory, so none of those is touched here.
# What it does not cover: an *ancestor* locked to the invoking user (it says so
# -- "a host prerequisite this function cannot satisfy from here"), and the
# sibling directories a harness creates. mktemp -d is 0700, and kernel/ and
# bin/ would follow the operator's umask, so the jail's sub-uid must be given
# a way in to all three.
chmod 0755 "$WORK"
export CELLA_HOME="$WORK/cella-home"
M="$CELLA_HOME/machines/$VM"
mkdir -p "$CELLA_HOME/kernel/canonical" "$CELLA_HOME/bin"
chmod 0755 "$CELLA_HOME/kernel" "$CELLA_HOME/kernel/canonical" "$CELLA_HOME/bin"
note "CELLA_HOME: $CELLA_HOME"
note "space:      $(df -h "$WORK" | awk 'NR==2 {print $4" free on "$6}')"

cp "$REAL_CELLA_HOME/kernel/canonical/bzImage" "$CELLA_HOME/kernel/canonical/" \
    || fail "could not copy the canonical kernel golden"
cp "$REAL_CELLA_HOME/kernel/canonical/golden.json" "$CELLA_HOME/kernel/canonical/" 2>/dev/null
note "kernel:     canonical, copied from $REAL_CELLA_HOME"

# The persona set is staged into the sandbox, for the same reason cella's own
# field install puts it in ~/.cella/bin: bwrap binds the VMM binary as the
# machine's sub-uid, and a checkout under a 0710 home refuses that uid at
# source resolution, long before KVM. Unconditional, so the smoke behaves
# identically on every host. The whole set, because the shim resolves each
# verb to a sibling beside its own inode.
find "$(dirname "$BIN")" -maxdepth 1 -type f -name 'cella*' -perm -u+x \
    -exec cp -p {} "$CELLA_HOME/bin/" \;
BIN="$CELLA_HOME/bin/$(basename "$BIN")"
note "cella:      staged into $CELLA_HOME/bin"

# ------------------------------------------------------------------ step 2

step "step 2: Titanium builds the rootfs"
note "subject:  $ENVIRONMENT_DIR/Containerfile"
note "This builds images and runs a package manager in one of them, so it"
note "needs egress. The VM it produces does not."

# The driver asserts the conversion's own claim, and the converter re-reads
# the manifest and re-hashes the artifact before publishing, so there is
# nothing left for this script to re-check afterwards.
PYTHONPATH="$ROOT/src" "$PY" "$DRIVER" \
    --environment "$ENVIRONMENT_DIR" \
    --home "$CELLA_HOME" \
    --flavor "$FLAVOR" \
    --size-bytes "$EXT4_BYTES" \
    || fail "the rootfs conversion did not produce a proven-bootable artifact"

# ------------------------------------------------------------------ step 3

step "step 3: boot it under Cella, with no network"

"$BIN" create "$VM" --kernel canonical --rootfs "$FLAVOR" \
    --mem-mb "$GUEST_MEM_MB" --net none --root rw >/dev/null \
    || fail "cella create failed"
"$BIN" start "$VM" >/dev/null || fail "cella start failed"
VMM_PID="$(cat "$M/pid" 2>/dev/null)"
note "started $VM (--net none), vmm pid=${VMM_PID:-unknown}"
note "waiting up to ${BOOT_TIMEOUT_SECS}s for the guest to prove itself"

deadline=$(( $(date +%s) + BOOT_TIMEOUT_SECS ))
reason=""
while :; do
    # Both halves. The marker alone says a shell ran; PID1=systemd is read
    # from /proc/1/comm inside the guest and is what says systemd is PID 1.
    if grep -q "$PROBE_MARKER" "$M/console.log" 2>/dev/null \
       && grep -q "PID1=systemd" "$M/console.log" 2>/dev/null; then
        break
    fi
    # A dead VMM fails in seconds instead of burning the whole timeout.
    if [ -n "$VMM_PID" ] && ! kill -0 "$VMM_PID" 2>/dev/null; then
        reason="the VMM exited before the guest proved itself"; break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        reason="no $PROBE_MARKER and PID1=systemd within ${BOOT_TIMEOUT_SECS}s"; break
    fi
    sleep 1
done

if [ -n "$reason" ]; then
    # The one failure Cella cannot explain: its verbs all succeeded and the
    # guest went quiet. Everywhere else Cella prints its own diagnosis.
    echo "--- console.log (tail) ---"
    tail -40 "$M/console.log" 2>/dev/null | sed 's/^/   /'
    echo "--- vmm.log (tail) ---"
    tail -20 "$M/vmm.log" 2>/dev/null | sed 's/^/   /'
    fail "$reason"
fi

pass "the guest booted, and PID 1 is systemd"
grep -E "$PROBE_MARKER|PID1=" "$M/console.log" | tail -2 | sed 's/^/     /'

# ------------------------------------------------------------------ step 4

step "step 4: freeze"

"$BIN" freeze "$VM" >/dev/null || fail "cella freeze failed"
[ -f "$M/state" ]       || fail "no state file after freeze"
[ -f "$M/ram.img" ]     || fail "no ram.img after freeze"
[ ! -f "$M/state.tmp" ] || fail "state.tmp left behind -- the rename did not happen"
pass "frozen: state and ram.img present, no leftover .tmp"

# ------------------------------------------------------------------ step 5

step "step 5: thaw"

"$BIN" thaw "$VM" >/dev/null || fail "cella thaw failed"
sleep 3
THAW_PID="$(cat "$M/pid" 2>/dev/null)"
[ -n "$THAW_PID" ] || fail "no pid file after thaw"
kill -0 "$THAW_PID" 2>/dev/null || fail "the VMM exited immediately on thaw"
grep -q "thawed" "$M/vmm.log" 2>/dev/null || fail "no 'thawed' message in vmm.log"
# One-shot enforcement: a successful thaw consumes the instant it resumed, so
# the same frozen state can never be resumed twice.
[ ! -f "$M/state" ] || fail "the state file survived thaw -- one-shot did not fire"
pass "thawed: running as pid $THAW_PID, vmm.log says thawed, state consumed"

# ------------------------------------------------------------------ step 6

step "step 6: stop"

# Required, not incidental: cella-universe refuses every universe verb on a
# running machine -- "archive needs a still machine (stop it or freeze it)".
"$BIN" stop "$VM" >/dev/null || fail "cella stop failed"
[ ! -f "$M/pid" ] || fail "the pid file survived stop"
pass "stopped: the machine is still"

# ------------------------------------------------------------------ step 7

step "step 7: archive"

"$BIN" archive "$VM" >/dev/null || fail "cella archive failed"
grep -q '"state"[[:space:]]*:[[:space:]]*"archived"' "$M/manifest.json" 2>/dev/null \
    || fail "the manifest does not record the machine as archived"
# A rock latches the digest of every storage layer it kept, and disk.img is
# always one of them.
grep -q '"digest_disk"' "$M/manifest.json" 2>/dev/null \
    || fail "the manifest records no disk.img digest, so no layer was latched"
pass "archived: a rock -- state=archived, disk.img digest latched"

# ------------------------------------------------------------------ step 8

step "step 8: destroy"

"$BIN" destroy "$VM" >/dev/null || fail "cella destroy failed"
[ ! -d "$M" ] || fail "the machine directory survived destroy: $M"
pass "destroyed: $M is gone"

echo
echo "ALL STEPS PASSED -- a stock debian:12-slim with no init became a"
echo "systemd-bootable ext4, booted under Cella with --net none, and survived"
echo "boot -> freeze -> thaw -> stop -> archive -> destroy."
