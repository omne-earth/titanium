#!/bin/bash
set -ueo pipefail
command -v podman >/dev/null || { echo "install podman first (make .podman)"; exit 1; }

# Podman needs no daemon registration: it resolves the "runsc" runtime name
# from containers.conf and a compiled-in table of default binary paths that
# includes /usr/local/bin/runsc. Installing the binary there is the whole job.
if ! command -v runsc >/dev/null; then
  # Download and checksum-verify in a temp dir, then rename into place: a
  # failed or torn download never leaves a partial binary on PATH.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  cd "$tmp"

  ARCH=$(uname -m)
  URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
  wget ${URL}/runsc ${URL}/runsc.sha512
  sha512sum -c runsc.sha512
  chmod a+rx runsc

  # Two-step move: cross-fs copy to a temp name, then same-dir rename (atomic).
  sudo mv runsc /usr/local/bin/.runsc.tmp
  sudo mv /usr/local/bin/.runsc.tmp /usr/local/bin/runsc
  sudo restorecon -v /usr/local/bin/runsc 2>/dev/null || true
fi

command -v runsc >/dev/null || exit 1
RUNSC_PATH=$(command -v runsc)

# Pin the installed binary by digest so preflight can verify it did not change
# after install (assert_runtime_digest fails closed on mismatch). SHA3-512,
# not SHA-512: the download above is already verified against upstream's
# SHA-512 checksum, so the pin living in a different hash family means a
# break in either family defeats at most one of the two checks. When runsc
# was already present the pin is trust-on-first-use: it blesses the current
# binary but makes every later change detectable. Never overwrite an existing
# pin from here — replacing the binary and re-running init must not silently
# re-bless it; delete the pin deliberately to rotate.
PIN=/usr/local/share/titanium/runsc.sha3-512
if [[ ! -f "$PIN" ]]; then
  sudo mkdir -p "$(dirname "$PIN")"
  python3 -c 'import hashlib, sys
print(hashlib.sha3_512(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' \
    "$RUNSC_PATH" | sudo tee "$PIN" >/dev/null
  echo "pinned $RUNSC_PATH digest at $PIN"
fi

# Register the runtime in the root-owned system configuration instead of
# relying on Podman's compiled-in path search: restores a root-gated registry
# analogous to Docker's /etc/docker/daemon.json.
DROPIN=/etc/containers/containers.conf.d/titanium-runsc.conf
if [[ ! -f "$DROPIN" ]]; then
  sudo mkdir -p "$(dirname "$DROPIN")"
  printf '[engine.runtimes]\nrunsc = ["%s"]\n' "$RUNSC_PATH" | sudo tee "$DROPIN" >/dev/null
  echo "registered runsc in $DROPIN"
fi

# A user-level containers.conf overrides the system registration without any
# privilege — exactly the redirect the registration exists to prevent.
USER_CONF=${XDG_CONFIG_HOME:-$HOME/.config}/containers/containers.conf
if grep -qs runsc "$USER_CONF"; then
  echo "warning: $USER_CONF mentions runsc and overrides the system registration in $DROPIN" >&2
fi

# Prove Podman itself resolves the runtime, the same way the environment's
# preflight does: an image-free create against an empty rootfs. This catches a
# containers.conf that shadows the default paths with a broken entry.
rootfs=$(mktemp -d)
name="titanium-runsc-probe-$$"
trap 'podman rm --force "$name" >/dev/null 2>&1; rm -rf "$rootfs"' EXIT
podman create --name "$name" --network none --runtime runsc --rootfs "$rootfs" true >/dev/null
podman rm --force "$name" >/dev/null
echo "runsc resolvable by podman"
