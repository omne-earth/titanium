#!/usr/bin/env bash
# Run a command wholly as the titanium runner user, in its own systemd scope.
#
# Whole-execution-as-user by design: per-command `sudo -u` breaks rootless
# podman's session assumptions, and podman's API socket would reintroduce the
# socket this environment exists to avoid. The scope gets the runner's HOME
# and XDG_RUNTIME_DIR (valid because titanium lingers) and inherits only the
# environment variables a trial legitimately needs.
set -ueo pipefail
RUNNER=${RUNNER:-titanium}
RUNNER_HOME=$(getent passwd "$RUNNER" | cut -d: -f6) || {
  echo "runner user '$RUNNER' does not exist — run: bash scripts/init/titanium.sh" >&2
  exit 1
}

PASS_VARS=(OPENROUTER_API_KEY PIER_API_BASE PIER_IMAGE_SOURCE PIER_PODMAN_SELINUX_RELABEL PIER_PODMAN_CGROUP_FAIL_CLOSED PIER_RUNSC_DIGEST_PIN)
setenv_args=()
for var in "${PASS_VARS[@]}"; do
  [[ -n "${!var:-}" ]] && setenv_args+=("--setenv=$var")
done

exec sudo --preserve-env="$(IFS=,; echo "${PASS_VARS[*]}")" \
  systemd-run --uid="$RUNNER" --pipe --wait --quiet --collect \
  --working-directory="$PWD" \
  --setenv=HOME="$RUNNER_HOME" \
  --setenv=XDG_RUNTIME_DIR="/run/user/$(id -u "$RUNNER")" \
  "${setenv_args[@]}" \
  -- "$@"
