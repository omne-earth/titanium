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
DELEGATE_DROPIN=/etc/systemd/system/user@.service.d/pier-delegate.conf
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

# 6. Filesystem access. Traversal-only (x) on the operator's home, read on
#    the repo, read-write on the run state; default ACLs keep titanium-written
#    trial output readable and writable by the operator.
sudo setfacl -m "u:$RUNNER:x" "$(dirname "$REPO_ROOT")"
sudo setfacl -R -m "u:$RUNNER:rX" "$REPO_ROOT"
mkdir -p "$REPO_ROOT/.run"
sudo setfacl -R -m "u:$RUNNER:rwX" -m "d:u:$RUNNER:rwX" -m "d:u:$OPERATOR:rwX" "$REPO_ROOT/.run"

# 7. Prove rootless podman works for the runner, in its own session scope —
#    the same invocation shape titanium-run.sh uses for trials.
sudo systemd-run --uid="$RUNNER" --pipe --wait --quiet --collect \
  --setenv=HOME="$RUNNER_HOME" \
  --setenv=XDG_RUNTIME_DIR="/run/user/$(id -u "$RUNNER")" \
  podman info --format 'runner podman ok: rootless={{.Host.Security.Rootless}} cgroups={{.Host.CgroupsVersion}}'

# 8. Stamp only after the probe proved the whole shape works: the Makefile
#    guard tests this file, so a partially provisioned host re-runs the
#    script (idempotent) instead of being silently skipped.
sudo mkdir -p /usr/local/share/pier
sudo touch /usr/local/share/pier/titanium.provisioned
echo "titanium provisioned — make targets now default to RUNNER=titanium"
