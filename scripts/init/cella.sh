#!/bin/bash
# Provision Cella for the sealed-VM rung: clone at the pinned rev, run
# Cella's own field installer, ensure the canonical kernel golden, and
# digest-pin the installed shim.
#
# Cella is *found, never built from a worktree* everywhere else in this
# repository (scripts/smoke/cella-rootfs.sh states why: the integration's
# claim is that it works against unchanged Cella). This script is the one
# place a build happens, and it builds only the pinned rev from the pinned
# https URL in runtime.env — never whatever sits in someone's checkout.
#
# What lands where, all idempotent:
#   ${XDG_CACHE_HOME:-~/.cache}/titanium/cella-src    the pinned source
#   ~/.cella/bin/*                                     the field binaries
#                                        (Cella's scripts/setup/install.sh)
#   ~/.cella/kernel/canonical/                         the kernel golden
#   ~/.cella/rootfs/terminator/{rootfs.ext4,ca.pem}    the terminator golden
#                                        (the pair CA for allow_internet=true)
#   /usr/local/share/titanium/cella.sha3-512           the digest pin
#
# The field flavor only: no console, the production posture. The lab
# flavor never installs (Cella's own ruling); developers point CELLA_BIN
# at a lab build from a checkout when a smoke needs the console.
set -ueo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/runtime.env" ]] || {
  echo "missing $REPO_ROOT/runtime.env — the checked-in runtime dependency pins" >&2
  exit 1
}
source "$REPO_ROOT/runtime.env"

# Cella boots KVM guests; without /dev/kvm nothing downstream can work, so
# fail here rather than as a create-time error later. Cella's own installer
# handles the group grant; this is only the existence check.
[[ -c /dev/kvm ]] || {
  echo "no /dev/kvm on this host — cella needs KVM (bare metal or nested virt)" >&2
  exit 1
}

# --- the pinned source -----------------------------------------------------

SRC="${XDG_CACHE_HOME:-$HOME/.cache}/titanium/cella-src"
if [[ ! -d "$SRC/.git" ]]; then
  mkdir -p "$(dirname "$SRC")"
  git clone "$CELLA_GIT_URL" "$SRC"
fi
# Fetch only when the pin is not already present locally: a host that has
# the rev needs no network, so provisioning re-runs stay offline-safe.
if ! git -C "$SRC" cat-file -e "$CELLA_GIT_REV^{commit}" 2>/dev/null; then
  git -C "$SRC" fetch origin
  git -C "$SRC" cat-file -e "$CELLA_GIT_REV^{commit}" 2>/dev/null || {
    echo "rev $CELLA_GIT_REV is not reachable from $CELLA_GIT_URL" >&2
    exit 1
  }
fi
# Detached at exactly the pin. --force: the clone is provisioning material
# owned by this script, not a workspace anyone edits.
git -C "$SRC" checkout --force --detach "$CELLA_GIT_REV"
echo "cella source at $SRC ($CELLA_GIT_REV)"

# --- the field install -----------------------------------------------------

CELLA_BIN_DIR="$HOME/.cella/bin"
CELLA="$CELLA_BIN_DIR/cella"

PIN=/usr/local/share/titanium/cella.sha3-512

# A pin whose witness names a different rev is a half-finished upgrade:
# rebuilding here would put a new binary under an old blessing, and
# re-pinning silently would make the upgrade nobody's decision. Refuse,
# exactly as the krun pin does, and name the manual step.
if [[ -f "$PIN" ]] && ! grep -q "git-witness: .*@$CELLA_GIT_REV" "$PIN"; then
  echo "the digest pin $PIN witnesses a different cella rev than runtime.env" >&2
  echo "pins: $(grep '^#' "$PIN")" >&2
  echo "want: $CELLA_GIT_REV" >&2
  echo "an upgrade is deliberate: delete the pin, re-run this script" >&2
  exit 1
fi

# Cella's own installer is the authority: host packages, sub-id delegation,
# the kvm group grant, cargo build --release, and the persona binaries into
# ~/.cella/bin. It is idempotent by its own contract, and it runs from the
# pinned checkout so what it builds is the pin. Skipped only when a pin
# already blesses this rev; in particular a binary present *without* a pin
# is rebuilt from the pinned source rather than adopted, so the git witness
# written below is never claimed for a binary of unknown provenance.
if [[ ! -f "$PIN" ]]; then
  (cd "$SRC" && bash scripts/setup/install.sh)
fi
[[ -x "$CELLA" ]] || {
  echo "cella install did not produce $CELLA" >&2
  exit 1
}

# --- $HOME traversal for the machine sub-uids -------------------------------

# Each machine runs jailed as its own sub-uid, and bwrap resolves the
# VMM binary under ~/.cella as that sub-uid. Cella's spawn grants the
# traversal ACLs on ~/.cella and ~/.cella/machines, but it declares the
# ancestors above them a host prerequisite it cannot satisfy (see
# machine.rs: "a mode-0700 $HOME ... refuses the sub-user before it
# ever reaches this ACL"), and neither its installer nor its doctor
# covers $HOME. Satisfy the prerequisite here: grant execute-only
# traversal of $HOME to the first machine sub-uids of the range the
# installer delegated. Execute-only opens no read on $HOME, and the
# grants are idempotent.
SUBUID_START=$(awk -F: -v u="$USER" '$1==u {print $2; exit}' /etc/subuid)
[[ -n "$SUBUID_START" ]] || {
  echo "no sub-uid range delegated to $USER in /etc/subuid" >&2
  exit 1
}
for ((uid = SUBUID_START; uid < SUBUID_START + 16; uid++)); do
  setfacl -m "u:$uid:x" "$HOME"
done
echo "granted $HOME traversal to sub-uids $SUBUID_START-$((SUBUID_START + 15))"

# --- the bridge ------------------------------------------------------------

# cella's field installer stops at the machine personas: cella-engine
# (the bridge, W.B.1) is the world's side of the seam and does not
# install itself. Titanium is that world, so it adds the one binary --
# from the same pinned build, into the same bin directory. The judged
# network topology (--net world) does not work without it.
BRIDGE="$CELLA_BIN_DIR/cella-engine"
if [[ ! -x "$BRIDGE" ]]; then
  [[ -x "$SRC/target/release/cella-engine" ]] \
    || (cd "$SRC" && cargo build --release -p cella-engine)
  install -m 0755 "$SRC/target/release/cella-engine" "$BRIDGE"
  echo "installed the bridge: $BRIDGE"
fi

# --- the kernel golden -----------------------------------------------------

# Built once; `cella build` recognizes an intact golden and the manifest
# names its inputs, so present-and-verifying means done. The build itself
# runs in Cella's build toolbox (a container), not on the host.
if [[ ! -f "$HOME/.cella/kernel/canonical/bzImage" ]]; then
  echo "building the canonical kernel golden (first run: this compiles a kernel)"
  "$CELLA" build kernel canonical
fi

# --- the terminator golden -------------------------------------------------

# The one network appliance for allow_internet=true (the terminated pair,
# docs/integration/TLS-TERMINATOR.md): one golden per host, and its build
# mints the pair CA. The key is baked into the image and never leaves it;
# ca.pem exports beside the golden, and titanium bakes that ca.pem into
# every task image's trust store so the member consents to the middle.
# Present-and-verifying means done, like the kernel golden.
if [[ ! -f "$HOME/.cella/rootfs/terminator/rootfs.ext4" ]]; then
  echo "building the terminator golden (mints this host's pair CA)"
  # The golden compiles a static cella-terminator from source, and
  # cella-build reads its source from the current directory, so this
  # runs from the checkout -- exactly as the install and bridge builds
  # above do.
  ( cd "$SRC" && "$CELLA" build rootfs terminator )
fi
[[ -f "$HOME/.cella/rootfs/terminator/ca.pem" ]] || {
  echo "cella build rootfs terminator did not export the pair ca.pem" >&2
  exit 1
}

# --- the digest pin --------------------------------------------------------

# Trust-on-first-use, like the krun and runsc pins, with the git rev as the
# witness: this script built the binary from the pinned rev moments ago, so
# the rev names the provenance the way krun's rpm -Vf names its package.
# Never overwritten here — to rotate (a deliberate upgrade), bump
# CELLA_GIT_REV in runtime.env, delete the pin, re-run this script.
if [[ ! -f "$PIN" ]]; then
  sudo mkdir -p "$(dirname "$PIN")"
  { echo "# git-witness: $CELLA_GIT_URL@$CELLA_GIT_REV installed-at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    python3 -c 'import hashlib, sys
print(hashlib.sha3_512(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' \
      "$CELLA"
  } | sudo tee "$PIN" >/dev/null
  echo "pinned $CELLA digest at $PIN (git witness: $CELLA_GIT_REV)"
fi

# --- the gate --------------------------------------------------------------

# Cella's own preflight has the last word: it names any unmet host
# precondition on its own stdout, so nothing is captured or restated here.
"$CELLA" doctor gate kvm bwrap golden:kernel:canonical golden:rootfs:terminator
echo "cella provisioned: $CELLA ($CELLA_GIT_REV)"
