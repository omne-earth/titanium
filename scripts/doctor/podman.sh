#!/usr/bin/env bash
# Host preflight for running Titanium on Podman without a socket.
# Checks the things that actually break, in the order they break.
#
#   ./podman.sh              # report only
#   ./podman.sh --bootstrap  # also provision what is missing (host-wide
#                            # concerns only; the runner user is provisioned
#                            # by scripts/init/titanium.sh)
#
# --fix is accepted as a deprecated alias for --bootstrap.

set -uo pipefail
FIX=0
[[ "${1:-}" == "--bootstrap" || "${1:-}" == "--fix" ]] && FIX=1

# podman-compose and titanium are project deps — prefer the repo venv over system PATH
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -d "$REPO_ROOT/.venv/bin" ]] && PATH="$REPO_ROOT/.venv/bin:$PATH"

# Version floors and pins live in runtime.env (checked in); a preflight that
# guesses its own minimums would drift from what init actually installs.
[[ -f "$REPO_ROOT/runtime.env" ]] || {
  echo "missing $REPO_ROOT/runtime.env — the checked-in runtime dependency pins" >&2
  exit 1
}
source "$REPO_ROOT/runtime.env"

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; FAILED=1; }
FAILED=0

echo "== binaries =="
command -v podman >/dev/null \
  && ok "podman $(podman --version | awk '{print $3}')" \
  || bad "podman not on PATH"

if command -v podman-compose >/dev/null; then
  # --version prints podman's own version line first; take podman-compose's.
  PCV=$(podman-compose --version 2>/dev/null | grep 'podman-compose' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [[ "$(printf '%s\n%s\n' "$PCV" "$PODMAN_COMPOSE_MIN_VERSION" | sort -V | head -1)" == "$PODMAN_COMPOSE_MIN_VERSION" ]]; then
    ok "podman-compose $PCV"
  else
    bad "podman-compose $PCV is < $PODMAN_COMPOSE_MIN_VERSION (needs depends_on: service_healthy)"
  fi
else
  bad "podman-compose not on PATH — pip install podman-compose"
fi

# krun is optional: only the krun-podman environment needs it, so absence is
# never a failure here. When present, hold it to the same floor init demands
# (CRUN_KRUN_MIN_VERSION). Below-floor is a warn, not a fail: it blocks
# nothing but krun-podman, and init and the environment preflight guard
# that path themselves (version warning, digest pin).
if command -v krun >/dev/null; then
  KV=$(krun --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
  KV=${KV:-0}
  if [[ "$(printf '%s\n%s\n' "$KV" "$CRUN_KRUN_MIN_VERSION" | sort -V | head -1)" == "$CRUN_KRUN_MIN_VERSION" ]]; then
    ok "krun $KV"
  else
    warn "krun $KV is < floor $CRUN_KRUN_MIN_VERSION (runtime.env) — sudo dnf upgrade crun-krun, delete the digest pin, re-run scripts/init/krun-podman.sh"
  fi
  [[ -c /dev/kvm ]] \
    || warn "krun installed but no /dev/kvm — krun-podman trials cannot start on this host"
else
  ok "krun not installed — only the krun-podman environment needs it (make .krun-podman)"
fi

echo
echo "== no socket in the path =="

# podman info talks to libpod in-process; it needs no service, no socket unit.
if podman info >/dev/null 2>&1; then
  ok "podman info succeeds (no podman.socket, no systemd user session needed)"
else
  bad "podman info failed — run it directly to see why"
fi

for v in DOCKER_HOST CONTAINER_HOST CONTAINER_CONNECTION; do
  if [[ -n "${!v:-}" ]]; then
    warn "$v is set (${!v}) — the backend clears DOCKER_HOST, but $v may redirect podman to a remote endpoint"
  fi
done

# --- socket unit: presence, then state ---
if ! command -v systemctl >/dev/null 2>&1; then
  ok "systemctl not present — no socket units to check"
else
  for scope in --user --system; do
    label="podman.socket ($scope)"

    # list-unit-files distinguishes "unit does not exist" from "exists but inactive"
    if ! systemctl "$scope" list-unit-files podman.socket >/dev/null 2>&1; then
      ok "$label: cannot query units in this context — nothing to check"
      continue
    fi

    if [[ -z "$(systemctl "$scope" list-unit-files --no-legend podman.socket 2>/dev/null)" ]]; then
      ok "$label: unit file not installed"
      continue
    fi

    state=$(systemctl "$scope" is-active podman.socket 2>/dev/null || true)
    enabled=$(systemctl "$scope" is-enabled podman.socket 2>/dev/null || true)

    if [[ "$state" == "active" ]]; then
      warn "$label: unit installed and ACTIVE (enabled=$enabled) — harmless, but nothing here uses it"
    else
      ok "$label: unit installed but not active (state=$state, enabled=$enabled)"
    fi
  done

  # podman.service is the socket-activated API service; flag it the same way
  for scope in --user --system; do
    label="podman.service ($scope)"
    if [[ -z "$(systemctl "$scope" list-unit-files --no-legend podman.service 2>/dev/null)" ]]; then
      ok "$label: unit file not installed"
      continue
    fi
    state=$(systemctl "$scope" is-active podman.service 2>/dev/null || true)
    if [[ "$state" == "active" ]]; then
      warn "$label: API service ACTIVE — nothing here uses it"
    else
      ok "$label: unit installed but not active (state=$state)"
    fi
  done
fi

# --- socket file on disk, independent of systemd ---
sockets=()
for s in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" /run/podman/podman.sock; do
  [[ -S "$s" ]] && sockets+=("$s")
done
if (( ${#sockets[@]} )); then
  warn "podman socket file(s) present: ${sockets[*]} — not used by this path"
else
  ok "no podman socket file on disk"
fi

echo
echo "== container DNS (egress proxy reachability) =="
BACKEND=$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)
case "$BACKEND" in
  netavark)
    if podman info --format '{{.Host.NetworkBackendInfo.DNS.Path}}' 2>/dev/null | grep -q .; then
      ok "netavark + aardvark-dns — 'titanium-egress-proxy' will resolve"
    else
      bad "netavark present but aardvark-dns missing — install aardvark-dns, or air-gapped tasks will fail at agent install"
    fi ;;
  cni)  bad "CNI backend — needs the podman-plugins/dnsname plugin for service-name DNS, or migrate to netavark" ;;
  *)    warn "unknown network backend: ${BACKEND:-<none>}" ;;
esac

echo
echo "== short-name resolution =="
# Titanium fully qualifies its own image references at build preparation (the
# agent-install Dockerfile rewrite, the prebuilt image name, and the egress
# proxy image), so the standard flow needs no search-registry configuration
# and the doctor no longer writes any. The residual: a task built directly
# from its own Dockerfile with NO agent install bypasses the rewrite, and a
# short name there still resolves through host configuration.
REG_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/containers/registries.conf"
EFFECTIVE_CONF=/etc/containers/registries.conf
[[ -f "$REG_CONF" ]] && EFFECTIVE_CONF="$REG_CONF"
mode=$(grep -hs '^[[:space:]]*short-name-mode' "$EFFECTIVE_CONF" | tail -1 | sed -E 's/.*"([a-z]+)".*/\1/')
mode=${mode:-enforcing}  # podman's compiled-in default

if [[ "$mode" == "enforcing" ]]; then
  ok "short-name-mode=enforcing (podman's supply-chain default) — Titanium qualifies its image references, so the standard flow never needs a search registry.
        Only tasks built from their own Dockerfile without an agent install would hit enforcing; qualify their FROM lines instead of relaxing this."
else
  warn "$EFFECTIVE_CONF relaxes short names (short-name-mode=$mode) — no longer needed by Titanium, which qualifies its image references.
        Every unqualified pull on this host resolves without confirmation; consider restoring enforcing mode."
fi

echo
echo "== cgroup delegation =="
# Rootless Podman enforces --cpus/--memory only on cgroups v2 with the cpu and
# memory controllers delegated to the user; otherwise it warns and silently
# drops the limit. Titanium reports the capability honestly (LIMIT tasks are
# rejected up front) and verifies enforcement after every start, but only
# delegation makes limits actually work.
CG=$(podman info --format '{{.Host.CgroupsVersion}}|{{.Host.CgroupControllers}}' 2>/dev/null)
CG_VERSION=${CG%%|*}
CG_CONTROLLERS=${CG#*|}
DELEGATE_DROPIN=/etc/systemd/system/user@.service.d/titanium-delegate.conf
if [[ "$CG_VERSION" != "v2" ]]; then
  warn "cgroups ${CG_VERSION:-<unknown>} — rootless Podman cannot enforce cpu/memory limits at all.
        LIMIT/GUARANTEE tasks are rejected; AUTO tasks run unbounded. Boot with
        systemd.unified_cgroup_hierarchy=1 to get v2."
elif [[ "$CG_CONTROLLERS" == *cpu* && "$CG_CONTROLLERS" == *memory* ]]; then
  ok "cgroups v2 with cpu+memory delegated ($CG_CONTROLLERS) — limits enforce, and Titanium verifies them post-start"
else
  if [[ $FIX -eq 1 ]]; then
    sudo mkdir -p "$(dirname "$DELEGATE_DROPIN")"
    printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee "$DELEGATE_DROPIN" >/dev/null
    sudo systemctl daemon-reload
    ok "wrote $DELEGATE_DROPIN — log out and back in (or restart user@$(id -u)) for delegation to apply"
  else
    warn "cgroups v2 but cpu/memory not delegated (controllers: ${CG_CONTROLLERS:-<none>}) — limits are silently dropped.
        LIMIT/GUARANTEE tasks are rejected; AUTO tasks run unbounded. Re-run with --bootstrap, or write $DELEGATE_DROPIN:
          [Service]
          Delegate=cpu cpuset io memory pids"
  fi
fi

echo
echo "== selinux =="
if command -v getenforce >/dev/null && [[ "$(getenforce)" == "Enforcing" ]]; then
  ok "SELinux enforcing — bind mounts are tagged bind.selinux=z, Podman relabels them at container start.
        Opt out with TITANIUM_PODMAN_SELINUX_RELABEL=none if you relabel externally."
else
  ok "SELinux not enforcing (or not present)"
fi

echo
echo "== titanium backend =="
python3 -c "from titanium.environments.podman import PodmanEnvironment as E; E.preflight(); print('  ok    PodmanEnvironment.preflight() passed')" 2>&1 \
  || bad "PodmanEnvironment.preflight() failed — is titanium installed?"

echo
if [[ $FAILED -eq 0 ]]; then
  echo "All clear. Try:"
  echo "  make smoke-podman        # one environment"
  echo "  make smoke-env           # all four"
else
  echo "Fix the failures above first."
  exit 1
fi
