#!/usr/bin/env bash
# Provision the dedicated runner user `titanium`: trial execution runs wholly
# as this user (scripts/titanium-run.sh), so a container escape lands as an
# account owning nothing but trial state — not the operator's keys and source.
# Idempotent; every step is skipped when already provisioned.
set -ueo pipefail

RUNNER=titanium
OPERATOR=$(id -un)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 1. The user. nologin: nothing interactive ever runs as titanium; systemd-run
#    needs no shell. create-home: rootless podman needs a real HOME for
#    storage (~/.local/share/containers) and config.
if ! id "$RUNNER" >/dev/null 2>&1; then
  sudo useradd --create-home --shell /usr/sbin/nologin "$RUNNER"
  echo "created user $RUNNER"
fi

# 2. Subordinate IDs — rootless podman's user namespace range. Modern shadow
#    allocates them at useradd; provision explicitly where it didn't.
if ! grep -qs "^$RUNNER:" /etc/subuid; then
  sudo usermod --add-subuids 2000000-2065535 --add-subgids 2000000-2065535 "$RUNNER"
  echo "allocated subuid/subgid range for $RUNNER"
fi

# 3. Linger: the user manager (and /run/user/<uid>, and cgroup delegation)
#    must exist without an interactive login.
sudo loginctl enable-linger "$RUNNER"

# 4. Cgroup delegation for user@.service — same drop-in the doctor provisions;
#    duplicated here so titanium works on a host that never ran --bootstrap.
DELEGATE_DROPIN=/etc/systemd/system/user@.service.d/titanium-delegate.conf
if [[ ! -f "$DELEGATE_DROPIN" ]]; then
  sudo mkdir -p "$(dirname "$DELEGATE_DROPIN")"
  printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee "$DELEGATE_DROPIN" >/dev/null
  sudo systemctl daemon-reload
fi

# 5. Root-provision the user-level container config directory. The
#    `containers` directory is owned by root and not writable by titanium, so
#    the user-level [engine.runtimes] redirect documented as
#    GVISOR-PODMAN.md §2.3's residual is closed structurally: only root
#    decides what "runsc" means for the runner. `.config` itself stays
#    runner-owned — podman refuses to run when $HOME/.config is not owned by
#    the invoking user.
RUNNER_HOME=$(getent passwd "$RUNNER" | cut -d: -f6)
sudo mkdir -p "$RUNNER_HOME/.config/containers"
sudo chown "$RUNNER:$RUNNER" "$RUNNER_HOME/.config"
sudo chown root:root "$RUNNER_HOME/.config/containers"
sudo chmod 755 "$RUNNER_HOME/.config" "$RUNNER_HOME/.config/containers"

# 6. Filesystem access. Traversal-only (x) on every path component above the
#    repo — execute without read, so titanium can pass through but not list —
#    read on the repo, read-write on the run state; default ACLs keep
#    titanium-written trial output readable and writable by the operator.
component=$REPO_ROOT
while [[ "$(dirname "$component")" != "/" ]]; do
  component=$(dirname "$component")
  sudo setfacl -m "u:$RUNNER:x" "$component"
done
sudo setfacl -R -m "u:$RUNNER:rX" "$REPO_ROOT"
mkdir -p "$REPO_ROOT/.run"
sudo setfacl -R -m "u:$RUNNER:rwX" -m "d:u:$RUNNER:rwX" -m "d:u:$OPERATOR:rwX" "$REPO_ROOT/.run"

# 7. /dev/kvm access for the krun-podman environment. Mirror of the grant in
#    scripts/init/krun-podman.sh, which cannot cover fresh hosts: make init
#    runs it before this script, so the runner does not exist yet when it
#    checks. Guarded on the device: hosts without KVM provision the runner
#    fine and only krun trials need the device. Group membership reaches
#    trials without a login — systemd-run reads it fresh per invocation.
if [[ -c /dev/kvm ]]; then
  if ! sudo -u "$RUNNER" test -r /dev/kvm || ! sudo -u "$RUNNER" test -w /dev/kvm; then
    KVM_GROUP=$(stat -c %G /dev/kvm)
    sudo usermod -aG "$KVM_GROUP" "$RUNNER"
    echo "added $RUNNER to '$KVM_GROUP' for /dev/kvm access (krun-podman)"
  fi
fi

# 8. Prove rootless podman works for the runner, in its own session scope —
#    the same invocation shape titanium-run.sh uses for trials. stdio is
#    detached from the caller's fds: over ssh those are sshd-created pipes
#    (sshd_session_t), which SELinux forbids dbus-broker to read when
#    systemd-run passes them ("Connection reset by peer"); pipefail keeps
#    the probe's failure fatal through the `cat`.
sudo systemd-run --uid="$RUNNER" --pipe --wait --quiet --collect \
  --setenv=HOME="$RUNNER_HOME" \
  --setenv=XDG_RUNTIME_DIR="/run/user/$(id -u "$RUNNER")" \
  podman info --format 'runner podman ok: rootless={{.Host.Security.Rootless}} cgroups={{.Host.CgroupsVersion}}' \
  </dev/null 2>&1 | cat

# 9. Stamp only after the probe proved the whole shape works: the Makefile
#    guard tests this file, so a partially provisioned host re-runs the
#    script (idempotent) instead of being silently skipped.
sudo mkdir -p /usr/local/share/titanium
sudo touch /usr/local/share/titanium/titanium.provisioned
echo "titanium provisioned — make targets now default to RUNNER=titanium"
