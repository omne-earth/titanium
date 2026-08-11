#!/usr/bin/env bash
# titanium-run.sh — execute a command wholly as the dedicated runner user.
#
# Usage: [RUNNER=<user>] titanium-run.sh <command> [args...]
#
# Trial execution runs in a single transient systemd scope owned by the
# runner user (default: titanium, provisioned by scripts/init/titanium.sh),
# so that a container escape lands as an account owning nothing but trial
# state. Two alternatives were rejected by design:
#
#   * per-command `sudo -u`, which breaks rootless podman's session
#     assumptions (user manager, XDG_RUNTIME_DIR, cgroup ownership);
#   * podman's per-user API socket, which would reintroduce the control
#     socket the podman environments exist to avoid (PODMAN.md §1).
#
# Privilege inventory: the single `sudo systemd-run` below is the only
# privileged command in the run path — its sole job is asking PID 1 to start
# the scope and drop to the runner. Everything inside the scope (pier,
# podman-compose, builds, conmon, the containers) runs unprivileged as the
# runner. The remaining privileged machinery is not specific to this wrapper:
# the setuid `newuidmap`/`newgidmap` helpers that all rootless podman uses,
# capability-scoped to the runner's /etc/subuid range, and PID 1 itself as
# the scope manager.
#
# Environment crossing the boundary is allowlisted: PASS_VARS names the
# variables a trial legitimately needs (model API credentials and PIER_*
# knobs); nothing else from the operator's environment reaches the scope.
set -ueo pipefail

RUNNER=${RUNNER:-titanium}
id -u "$RUNNER" >/dev/null 2>&1 || {
  echo "runner user '$RUNNER' does not exist — run: bash scripts/init/titanium.sh" >&2
  exit 1
}

# stdout/stderr pass through process-substitution pipes rather than the
# caller's file descriptors: `systemd-run --pipe` hands the caller's fds to
# PID 1, which accepts pipes and ttys but rejects regular files ("Failed to
# start transient service unit: Remote peer disconnected") — and
# `make smoke-* > log` is precisely that shape.
exec > >(cat) 2> >(cat >&2)

# systemd's ExecStart= requires an absolute executable path; a relative path
# containing a slash (`.venv/bin/pier`) is resolved against the caller's
# working directory. A bare name is left to systemd-run's PATH resolution.
[[ "${1:-}" == */* && "${1:-}" != /* ]] && set -- "$PWD/$1" "${@:2}"

PASS_VARS=(OPENROUTER_API_KEY PIER_API_BASE PIER_IMAGE_SOURCE PIER_PODMAN_SELINUX_RELABEL PIER_PODMAN_CGROUP_FAIL_CLOSED PIER_RUNSC_DIGEST_PIN)
setenv_args=()
for var in "${PASS_VARS[@]}"; do
  [[ -n "${!var:-}" ]] && setenv_args+=("--setenv=$var")
done

# The scope's entrypoint is /usr/bin/bash, not the target binary: on an
# SELinux-enforcing host the unit's first exec happens in init_t, which is
# not permitted to execute user_home_t files — and a repo venv under /home
# is exactly that. Entering through a system shell transitions into the
# unconfined service domain, which is; relabeling the venv instead would
# not survive the next `uv sync`. HOME needs no --setenv: systemd sets it
# from the user database for any unit with User=. XDG_RUNTIME_DIR does —
# systemd deliberately leaves it unset for system units, and rootless
# podman requires it (valid because the runner lingers).
exec sudo --preserve-env="$(IFS=,; echo "${PASS_VARS[*]}")" \
  systemd-run --uid="$RUNNER" --pipe --wait --quiet --collect \
  --working-directory="$PWD" \
  --setenv=XDG_RUNTIME_DIR="/run/user/$(id -u "$RUNNER")" \
  "${setenv_args[@]}" \
  -- /usr/bin/bash -c 'exec "$0" "$@"' "$@"
