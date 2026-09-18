#!/usr/bin/env bash
# Undo the host-level provisioning of `make init` — the inverse of
# scripts/init/{titanium,krun-podman,runsc-podman,runsc,docker}.sh, in
# reverse order.
# Repo-local state is `make reset`'s job (collect + git clean); this script
# touches only the host. Idempotent: every step skips when already absent.
# Deliberately NOT undone: distro packages (podman, docker, uv, gcc) and
# the docker service — reset owns titanium's state, not the machine's.
set -ueo pipefail

RUNNER=${RUNNER:-titanium}
OPERATOR=$(id -un)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 0. Quiesce before removing the account that owns the processes. Order
#    matters: stop the shim's transient units, then terminate the user's
#    session tree — never blanket-pkill the runner UID, which SIGKILLs
#    user@<uid>.service and strands a failed user manager.
sudo systemctl stop 'run-p*.service' 2>/dev/null || true
sudo systemctl reset-failed 'run-p*.service' 2>/dev/null || true
if id "$RUNNER" >/dev/null 2>&1; then
  sudo loginctl terminate-user "$RUNNER" 2>/dev/null || true
  sudo loginctl disable-linger "$RUNNER" 2>/dev/null || true
  # userdel refuses while runner processes linger; give the tree a moment.
  for _ in $(seq 1 10); do
    pgrep -u "$RUNNER" >/dev/null 2>&1 || break
    sleep 1
  done
fi

# 1. ACLs granted to the runner (titanium.sh §6) — dropped while the uid
#    still resolves; after userdel they would linger as numeric orphans.
if id "$RUNNER" >/dev/null 2>&1; then
  component=$REPO_ROOT
  while [[ "$(dirname "$component")" != "/" ]]; do
    component=$(dirname "$component")
    sudo setfacl -x "u:$RUNNER" "$component" 2>/dev/null || true
  done
  sudo setfacl -R -x "u:$RUNNER" -x "d:u:$RUNNER" "$REPO_ROOT" 2>/dev/null || true
fi

# 2. The runner account: home (container storage included) and mail spool go
#    with -r; modern shadow's userdel also releases the /etc/sub[ug]id range.
if id "$RUNNER" >/dev/null 2>&1; then
  sudo userdel -r "$RUNNER" 2>/dev/null || sudo userdel -rf "$RUNNER"
  echo "removed user $RUNNER"
fi

# 3. Cgroup delegation drop-in (titanium.sh §4, also doctor --bootstrap).
if [[ -f /etc/systemd/system/user@.service.d/titanium-delegate.conf ]]; then
  sudo rm /etc/systemd/system/user@.service.d/titanium-delegate.conf
  sudo rmdir --ignore-fail-on-non-empty /etc/systemd/system/user@.service.d
  sudo systemctl daemon-reload
  echo "removed user@.service delegation drop-in"
fi

# 4. Podman runtimes (runsc-podman.sh, krun-podman.sh): registrations before
#    binaries, so no window where a runtime name resolves to a missing path;
#    then the wrapper, the runsc binaries, and the digest pins + provisioned
#    stamp directory (the krun pin lives there too). The krun binary itself
#    is the distro's crun-krun package and stays, per the package policy in
#    the header; its registration and pin are titanium's and go. The kvm
#    group grants die with the runner account (§2) and, for the operator,
#    follow the docker-group rationale below — not revoked.
sudo rm -f /etc/containers/containers.conf.d/titanium-runsc.conf
sudo rm -f /etc/containers/containers.conf.d/titanium-krun.conf
sudo rm -f /usr/local/bin/runsc-ignorecg \
  /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
sudo rm -rf /usr/local/share/titanium

# 5. runsc for docker (runsc.sh): remove the daemon.json entry the same way
#    it was merged in — edit, temp + rename, restart only on change.
if grep -qs '"runsc"' /etc/docker/daemon.json; then
  python3 - <<'PY' | sudo tee /etc/docker/.daemon.json.tmp >/dev/null
import json
path = "/etc/docker/daemon.json"
data = json.load(open(path))
data.get("runtimes", {}).pop("runsc", None)
if not data.get("runtimes"):
    data.pop("runtimes", None)
print(json.dumps(data, indent=2))
PY
  sudo mv /etc/docker/.daemon.json.tmp /etc/docker/daemon.json
  if systemctl is-active -q docker; then sudo systemctl restart docker; fi
  echo "deregistered runsc from docker"
fi

# 6. Cella's operator-side state (cella.sh, cella-debug.sh): the installed
#    field binaries, kernel golden, and rootfs golden under ~/.cella, plus
#    the pinned source clone and its release/lab build output under the
#    cella-src cache — none of it is reproducible except by re-cloning and
#    rebuilding, so this is the slowest step `make init` will have to redo.
#    The $HOME traversal ACLs cella.sh grants to the operator's own sub-uid
#    range (§"$HOME traversal for the machine sub-uids") go with it — they
#    exist only to let cella's jailed machines resolve into ~/.cella.
if [[ -d "$HOME/.cella" ]]; then
  rm -rf "$HOME/.cella"
  echo "removed $HOME/.cella"
fi
SUBUID_START=$(awk -F: -v u="$OPERATOR" '$1==u {print $2; exit}' /etc/subuid)
if [[ -n "$SUBUID_START" ]]; then
  for ((uid = SUBUID_START; uid < SUBUID_START + 16; uid++)); do
    setfacl -x "u:$uid" "$HOME" 2>/dev/null || true
  done
  echo "dropped $HOME traversal ACLs for sub-uids $SUBUID_START-$((SUBUID_START + 15))"
fi
CELLA_SRC="${XDG_CACHE_HOME:-$HOME/.cache}/titanium/cella-src"
if [[ -d "$CELLA_SRC" ]]; then
  rm -rf "$CELLA_SRC"
  echo "removed $CELLA_SRC"
fi
# cella's own build tool shells out to podman for a throwaway container it
# names cella-build (the kernel/rootfs golden compile happens in there, not
# on the host) — a failed or interrupted build leaves it behind as the
# operator, not the runner, so userdel never touches it.
podman rm -f cella-build >/dev/null 2>&1 && echo "removed leftover cella-build container" || true

# The operator's docker-group grant (docker.sh) is deliberately NOT revoked:
# the grant only goes live across a reboot, so revoke-and-reinit would cost a
# reboot cycle every reset. It is also root-equivalent access the operator
# already holds via sudo — nothing is gained by clawing it back. Revoke by
# hand if the machine is leaving titanium duty: sudo gpasswd -d $USER docker

echo "host deprovisioned — packages and services left installed"
