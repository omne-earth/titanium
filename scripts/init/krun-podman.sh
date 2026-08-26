#!/bin/bash
set -ueo pipefail
command -v podman >/dev/null || { echo "install podman first (make .podman)"; exit 1; }

# A version floor, not an exact pin: krun comes from the distro crun-krun
# package (crun built with the libkrun handler), and upstream supplies no
# standalone checksummed binary like gVisor does. dnf is the supply chain.
# The floor lives in runtime.env (checked in, single source of truth);
# refuse to run without it. The digest pin below makes the installed binary
# tamper-evident.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/runtime.env" ]] || {
  echo "missing $REPO_ROOT/runtime.env — the checked-in runtime dependency pins" >&2
  exit 1
}
source "$REPO_ROOT/runtime.env"

# krun runs each container in a KVM microVM. Without a usable /dev/kvm the
# runtime cannot start at all, so check KVM before any install step.
[[ -c /dev/kvm ]] || {
  echo "no /dev/kvm on this host — krun needs KVM (bare metal or nested virt)" >&2
  exit 1
}

# Make sure the invoking user can open /dev/kvm. Fedora ships the device as
# 0666; on a host that narrows it to the kvm group, add the user to that
# group. The grant is visible only to new sessions.
if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
  KVM_GROUP=$(stat -c %G /dev/kvm)
  sudo usermod -aG "$KVM_GROUP" "$(id -un)"
  echo "warning: added $(id -un) to '$KVM_GROUP' for /dev/kvm — log in again before you run trials" >&2
fi

# Same check for the titanium runner user, when it is provisioned. The
# runner has no login sessions: trials start through systemd-run, which
# reads group membership fresh from the user database, so the grant applies
# from the next trial with no further step.
if id titanium >/dev/null 2>&1; then
  if ! sudo -u titanium test -r /dev/kvm || ! sudo -u titanium test -w /dev/kvm; then
    KVM_GROUP=$(stat -c %G /dev/kvm)
    sudo usermod -aG "$KVM_GROUP" titanium
    echo "added titanium to '$KVM_GROUP' for /dev/kvm access"
  fi
fi

# Install through dnf, not a download: the package is the only distribution
# channel, and rpm signature checks stand in for the checksum step the runsc
# scripts perform on their downloads.
if ! command -v krun >/dev/null; then
  command -v dnf >/dev/null || {
    echo "krun install needs dnf — install the crun-krun package manually" >&2
    exit 1
  }
  sudo dnf install -y crun-krun
fi

command -v krun >/dev/null || exit 1
KRUN_PATH=$(command -v krun)

# An already-present binary is used as-is (the digest pin below makes it
# tamper-evident), but report a version below the floor loudly.
KRUN_VERSION=$(krun --version | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
if [[ "$(printf '%s\n' "$CRUN_KRUN_MIN_VERSION" "$KRUN_VERSION" | sort -V | head -1)" != "$CRUN_KRUN_MIN_VERSION" ]]; then
  echo "warning: installed krun $KRUN_VERSION < floor $CRUN_KRUN_MIN_VERSION (runtime.env)" >&2
  echo "warning: to converge: sudo dnf upgrade crun-krun, delete the digest pin, re-run this script" >&2
fi

# Pin the installed binary by digest so preflight can verify that it did not
# change after install (assert_runtime_digest fails closed on mismatch).
# SHA3-512 for the same reason as the runsc pin: the hash family differs from
# the rpm signature chain, so a break in either family defeats at most one of
# the two checks. The pin is always trust-on-first-use here — dnf installed
# the binary, this script did not download it. A later `dnf upgrade
# crun-krun` changes the binary and trips the pin. That is by design: an
# upgrade must be blessed deliberately. Never overwrite an existing pin from
# here; to rotate, delete the pin and re-run this script.
PIN=/usr/local/share/titanium/krun.sha3-512
if [[ ! -f "$PIN" ]]; then
  sudo mkdir -p "$(dirname "$PIN")"
  python3 -c 'import hashlib, sys
print(hashlib.sha3_512(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' \
    "$KRUN_PATH" | sudo tee "$PIN" >/dev/null
  echo "pinned $KRUN_PATH digest at $PIN"
fi

# No wrapper analog to runsc-ignorecg: crun manages rootless cgroups through
# the user manager natively, so krun needs no cgroup opt-out.

# Register the runtime in the root-owned system configuration. Podman would
# resolve "krun" from its compiled-in default paths without this, but the
# drop-in keeps the runtime table root-gated and explicit, exactly like the
# runsc registration. Drop-in [engine.runtimes] tables merge across files, so
# this file and titanium-runsc.conf coexist. Rewritten when the content
# drifts from what this script would install (it is configuration, not a
# trust-on-first-use pin).
DROPIN=/etc/containers/containers.conf.d/titanium-krun.conf
WANT=$(printf '[engine.runtimes]\nkrun = ["%s"]\n' "$KRUN_PATH")
if [[ ! -f "$DROPIN" || "$(cat "$DROPIN")" != "$WANT" ]]; then
  sudo mkdir -p "$(dirname "$DROPIN")"
  printf '%s\n' "$WANT" | sudo tee "$DROPIN" >/dev/null
  echo "registered krun in $DROPIN"
fi

# A user-level containers.conf overrides the system registration without any
# privilege — exactly the redirect the registration exists to prevent.
USER_CONF=${XDG_CONFIG_HOME:-$HOME/.config}/containers/containers.conf
if grep -qs krun "$USER_CONF"; then
  echo "warning: $USER_CONF mentions krun and overrides the system registration in $DROPIN" >&2
fi

# Prove that Podman itself resolves the runtime, the same way the
# environment's preflight does: an image-free create against an empty
# rootfs. Create does not start the microVM, so this validates resolution,
# not KVM.
rootfs=$(mktemp -d)
name="titanium-krun-probe-$$"
trap 'podman rm --force "$name" >/dev/null 2>&1; rm -rf "$rootfs"' EXIT
podman create --name "$name" --network none --runtime krun --rootfs "$rootfs" true >/dev/null
podman rm --force "$name" >/dev/null
echo "krun resolvable by podman"
