#!/usr/bin/env bash
# Host preflight for running Pier on Podman without a socket.
# Checks the things that actually break, in the order they break.
#
#   ./podman-doctor.sh          # report only
#   ./podman-doctor.sh --fix    # also write registries.conf if missing

set -uo pipefail
FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1

# podman-compose and pier are project deps — prefer the repo venv over system PATH
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -d "$REPO_ROOT/.venv/bin" ]] && PATH="$REPO_ROOT/.venv/bin:$PATH"

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
  if [[ "$(printf '%s\n1.6.0\n' "$PCV" | sort -V | head -1)" == "1.6.0" ]]; then
    ok "podman-compose $PCV"
  else
    bad "podman-compose $PCV is < 1.6.0 (needs depends_on: service_healthy)"
  fi
else
  bad "podman-compose not on PATH — pip install podman-compose"
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
      ok "netavark + aardvark-dns — 'pier-egress-proxy' will resolve"
    else
      bad "netavark present but aardvark-dns missing — install aardvark-dns, or air-gapped tasks will fail at agent install"
    fi ;;
  cni)  bad "CNI backend — needs the podman-plugins/dnsname plugin for service-name DNS, or migrate to netavark" ;;
  *)    warn "unknown network backend: ${BACKEND:-<none>}" ;;
esac

echo
echo "== short-name resolution =="
# Task Dockerfiles use short names (`FROM ubuntu:24.04`), which need search
# registries AND a non-enforcing short-name-mode: enforcing wants to prompt
# for the registry, a hard failure in a non-TTY build.
REG_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/containers/registries.conf"
# The user-level file, when present, replaces /etc's wholesale.
EFFECTIVE_CONF=/etc/containers/registries.conf
[[ -f "$REG_CONF" ]] && EFFECTIVE_CONF="$REG_CONF"
mode=$(grep -hs '^[[:space:]]*short-name-mode' "$EFFECTIVE_CONF" | tail -1 | sed -E 's/.*"([a-z]+)".*/\1/')
mode=${mode:-enforcing}  # podman's compiled-in default

if grep -qs '^[[:space:]]*unqualified-search-registries' "$EFFECTIVE_CONF" && [[ "$mode" != "enforcing" ]]; then
  ok "short names resolve non-interactively ($EFFECTIVE_CONF, short-name-mode=$mode)"
else
  if [[ $FIX -eq 1 ]]; then
    mkdir -p "$(dirname "$REG_CONF")"
    # The user file replaces /etc's, so it must carry both keys itself.
    [[ -f "$REG_CONF" ]] && sed -i -E '/^[[:space:]]*(unqualified-search-registries|short-name-mode)/d' "$REG_CONF"
    cat >> "$REG_CONF" <<'EOF'
unqualified-search-registries = ["docker.io"]
short-name-mode = "permissive"
EOF
    ok "wrote unqualified-search-registries + short-name-mode=permissive to $REG_CONF"
  else
    bad "short names won't resolve ($EFFECTIVE_CONF: short-name-mode=$mode) — 'FROM ubuntu:24.04' will fail. Re-run with --fix, or set in $REG_CONF:
          unqualified-search-registries = [\"docker.io\"]
          short-name-mode = \"permissive\""
  fi
fi

echo
echo "== selinux =="
if command -v getenforce >/dev/null && [[ "$(getenforce)" == "Enforcing" ]]; then
  ok "SELinux enforcing — bind mounts are tagged bind.selinux=z, Podman relabels them at container start.
        Opt out with PIER_PODMAN_SELINUX_RELABEL=none if you relabel externally."
else
  ok "SELinux not enforcing (or not present)"
fi

echo
echo "== pier backend =="
python3 -c "from pier.environments.podman import PodmanEnvironment as E; E.preflight(); print('  ok    PodmanEnvironment.preflight() passed')" 2>&1 \
  || bad "PodmanEnvironment.preflight() failed — is pier installed?"

echo
if [[ $FAILED -eq 0 ]]; then
  echo "All clear. Run:"
  echo "  pier job start -c examples/jobs/mini-swe-agent-podman.yaml"
else
  echo "Fix the failures above first."
  exit 1
fi
