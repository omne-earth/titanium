#!/usr/bin/env bash
# Prove the host and checkout are at fresh-clone equivalence after
# `make reset`. Read-only: prints every violation it finds and exits
# non-zero if there are any, so a partial teardown cannot pass silently.
set -uo pipefail

RUNNER=${RUNNER:-titanium}
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fail=0
flag() {
  echo "NOT CLEAN: $*" >&2
  fail=1
}

# --- host: the runner account and its residue -------------------------------
id "$RUNNER" >/dev/null 2>&1 && flag "user $RUNNER still exists"
grep -qs "^$RUNNER:" /etc/subuid && flag "/etc/subuid still allocates a range to $RUNNER"
grep -qs "^$RUNNER:" /etc/subgid && flag "/etc/subgid still allocates a range to $RUNNER"
[[ -d "/home/$RUNNER" ]] && flag "/home/$RUNNER still exists"
[[ -e "/var/lib/systemd/linger/$RUNNER" ]] && flag "linger still enabled for $RUNNER"

# --- host: files installed by the init chain --------------------------------
for path in \
  /usr/local/bin/runsc \
  /usr/local/bin/runsc-ignorecg \
  /usr/local/bin/containerd-shim-runsc-v1 \
  /etc/containers/containers.conf.d/titanium-runsc.conf \
  /etc/systemd/system/user@.service.d/titanium-delegate.conf \
  /usr/local/share/titanium; do
  [[ -e "$path" ]] && flag "$path remains"
done
grep -qs '"runsc"' /etc/docker/daemon.json \
  && flag "/etc/docker/daemon.json still registers runsc"
id -nG "$(id -un)" | grep -qw docker \
  && flag "$(id -un) is still in the docker group"

# --- host: ACL residue on the repo and its ancestors -------------------------
# After userdel a leftover grant shows as a numeric uid, so match both forms.
acl_residue() {
  getfacl --absolute-names --skip-base "$1" 2>/dev/null \
    | grep -E "^(default:)?user:($RUNNER|[0-9]+):"
}
component=$REPO_ROOT
while [[ "$component" != "/" ]]; do
  acl_residue "$component" >/dev/null && flag "ACL residue on $component"
  component=$(dirname "$component")
done

# --- host: nothing titanium-shaped still running -----------------------------
systemctl list-units --plain --no-legend 'run-p*.service' 2>/dev/null | grep -q . \
  && flag "shim transient units still running (run-p*.service)"
tmux -L titanium list-sessions >/dev/null 2>&1 \
  && flag "titanium tmux server still has sessions"

# --- checkout: fresh-clone equivalence ---------------------------------------
# Untracked/ignored state must be gone except the sanctioned survivors.
cruft=$(cd "$REPO_ROOT" && git clean -nxd -e .secrets -e .archive)
[[ -n "$cruft" ]] && flag "untracked state survived git clean:" && echo "$cruft" >&2
# Tracked-file edits are reported but never count as failure: reset must not
# decide the fate of work in progress.
if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
  echo "note: tracked files carry local edits (left alone by design):" >&2
  git -C "$REPO_ROOT" status --short --untracked-files=no >&2
fi

if [[ "$fail" -ne 0 ]]; then
  echo "clean-slate check FAILED — see violations above" >&2
  exit 1
fi
echo "clean slate verified: host deprovisioned, checkout at fresh-clone equivalence"
