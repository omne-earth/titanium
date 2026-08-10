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

# Prove Podman itself resolves the runtime, the same way the environment's
# preflight does: an image-free create against an empty rootfs. This catches a
# containers.conf that shadows the default paths with a broken entry.
rootfs=$(mktemp -d)
name="pier-runsc-probe-$$"
trap 'podman rm --force "$name" >/dev/null 2>&1; rm -rf "$rootfs"' EXIT
podman create --name "$name" --network none --runtime runsc --rootfs "$rootfs" true >/dev/null
podman rm --force "$name" >/dev/null
echo "runsc resolvable by podman"
