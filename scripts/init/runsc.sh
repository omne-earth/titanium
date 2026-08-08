#!/bin/bash
set -ueox pipefail
command -v docker >/dev/null || { echo "run scripts/init/docker.sh first"; exit 1; }

if ! command -v runsc >/dev/null; then
  # Download and checksum-verify in a temp dir, then rename into place: a
  # failed or torn download never leaves a partial binary on PATH.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  cd "$tmp"

  ARCH=$(uname -m)
  URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
  wget ${URL}/runsc ${URL}/runsc.sha512 \
       ${URL}/containerd-shim-runsc-v1 ${URL}/containerd-shim-runsc-v1.sha512
  sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512
  chmod a+rx runsc containerd-shim-runsc-v1

  # Two-step move: cross-fs copy to a temp name, then same-dir rename (atomic).
  for bin in runsc containerd-shim-runsc-v1; do
    sudo mv "$bin" "/usr/local/bin/.$bin.tmp"
    sudo mv "/usr/local/bin/.$bin.tmp" "/usr/local/bin/$bin"
  done
  sudo restorecon -v /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
fi

command -v runsc >/dev/null || exit 1

# Register the runtime only when missing; merge into any existing daemon.json
# instead of clobbering it, and write via temp + rename (atomic) so a crash
# mid-write can't leave docker with truncated config. Restart only on change.
if ! grep -qs '"runsc"' /etc/docker/daemon.json; then
  sudo mkdir -p /etc/docker
  python3 - <<'PY' | sudo tee /etc/docker/.daemon.json.tmp >/dev/null
import json, os
path = "/etc/docker/daemon.json"
data = json.load(open(path)) if os.path.exists(path) else {}
data.setdefault("runtimes", {})["runsc"] = {"path": "/usr/local/bin/runsc"}
print(json.dumps(data, indent=2))
PY
  sudo mv /etc/docker/.daemon.json.tmp /etc/docker/daemon.json
  sudo systemctl restart docker
fi
