#!/usr/bin/env bash
# Doctor for hosting libvirt guests on the same machine as the Docker daemon
# (which titanium's own init installs for the docker/gvisor environments).
#
#   ./libvirt-docker.sh        # report only
#   ./libvirt-docker.sh --fix  # also repair, atomically (see below)
#
# Why this exists:
#
#   * Docker sets the iptables FORWARD chain to policy DROP host-wide. A
#     forwarded packet must be accepted by *every* firewall table, and
#     libvirt's default nftables backend keeps its accepts in its own
#     table — so guest traffic sails through libvirt's rules and still
#     dies in Docker's. Guests keep DHCP and gateway DNS (host-local, no
#     forwarding) while all internet traffic times out, which reads as a
#     mystery outage. Fix: firewall_backend = "iptables" in
#     /etc/libvirt/network.conf, so libvirt plants LIBVIRT_FW* accepts at
#     the top of the same FORWARD chain, recreated on every net start.
#
#   * firewalld can lose the bridge's libvirt-zone binding across reloads
#     (Docker's firewalld integration is a known disturber). The default
#     zone then rejects guest DNS to the bridge address. Fix: bind the
#     bridge permanently.
#
# Atomicity: applying the backend live requires net-destroy/net-start,
# which orphans the taps of running guests — their NICs silently leave the
# rebuilt bridge. The fix path snapshots every tap enslaved to the bridge
# before the restart and reattaches each one after, so a running VM comes
# out connected, not stranded.
set -uo pipefail

FIX=0
[[ "${1:-}" == "--fix" || "${1:-}" == "--bootstrap" ]] && FIX=1

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; FAILED=1; }
FAILED=0

NETWORK_CONF=/etc/libvirt/network.conf

# ------------------------------------------------------------ applicability
command -v virsh >/dev/null || { ok "no virsh — host runs no libvirt guests"; exit 0; }
mapfile -t NETS < <(sudo virsh net-list --name 2>/dev/null | sed '/^$/d')
((${#NETS[@]})) || { ok "no active libvirt networks — nothing to protect"; exit 0; }
if ! { command -v docker && systemctl is-active -q docker; } >/dev/null 2>&1; then
  ok "docker not running — no FORWARD-drop hazard for libvirt guests"
  exit 0
fi

echo "== docker + libvirt coexistence =="

# --------------------------------------------------- libvirt firewall backend
if sudo grep -Eqs '^\s*firewall_backend\s*=\s*"iptables"' "$NETWORK_CONF"; then
  ok "libvirt firewall_backend is iptables ($NETWORK_CONF)"
else
  bad "libvirt is not on the iptables firewall backend — Docker's FORWARD drop eats guest traffic"
  if ((FIX)); then
    printf 'firewall_backend = "iptables"\n' | sudo tee -a "$NETWORK_CONF" >/dev/null
    sudo systemctl try-restart virtnetworkd virtqemud libvirtd 2>/dev/null
    ok "set firewall_backend=iptables and restarted libvirt daemons"
  fi
fi

# The observable truth, config aside: libvirt's accepts must sit in the
# FORWARD chain whenever its policy is DROP.
NEED_NET_RESTART=0
if sudo iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP'; then
  if sudo iptables -S FORWARD | grep -q -- '-j LIBVIRT_FW'; then
    ok "FORWARD is policy DROP but carries LIBVIRT_FW* jumps"
  else
    bad "FORWARD is policy DROP with no LIBVIRT_FW* jumps — guest forwarding is dead right now"
    NEED_NET_RESTART=1
  fi
else
  ok "FORWARD policy is not DROP — no accepts required"
fi

# The daemons track their global chains in memory: with the jumps gone from
# the live chain, a bare net-restart recreates nothing — libvirt still
# believes they exist. Restart the daemons first so the coming net-start
# rebuilds from scratch. (Learned the hard way; a net-only restart passes
# silently and leaves forwarding dead.)
if ((FIX && NEED_NET_RESTART)); then
  sudo systemctl try-restart virtnetworkd virtqemud libvirtd 2>/dev/null
  ok "restarted libvirt daemons to reset their firewall-rule bookkeeping"
fi

# ------------------------------------------------------- per-network checks
for net in "${NETS[@]}"; do
  bridge=$(sudo virsh net-info "$net" | awk '/^Bridge:/{print $2}')
  [[ -n "$bridge" ]] || continue

  if command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
    zone=$(sudo firewall-cmd --get-zone-of-interface="$bridge" 2>/dev/null || echo "none")
    if [[ "$zone" == libvirt* ]]; then
      ok "$bridge is in firewalld zone '$zone'"
    else
      bad "$bridge is in firewalld zone '$zone' — default zones reject guest DNS to the bridge"
      ((FIX)) && sudo firewall-cmd --zone=libvirt --change-interface="$bridge" >/dev/null \
        && ok "moved $bridge to the libvirt zone (runtime)"
    fi
    if sudo firewall-cmd --permanent --zone=libvirt --list-interfaces 2>/dev/null | grep -qw "$bridge"; then
      ok "$bridge libvirt-zone binding is permanent"
    else
      bad "$bridge zone binding is runtime-only — the next reboot or firewalld reload loses it"
      ((FIX)) && sudo firewall-cmd --permanent --zone=libvirt --change-interface="$bridge" >/dev/null \
        && ok "made $bridge zone binding permanent"
    fi
  fi

  # ------------------------------------------- atomic network restart (fix)
  # Only when the live FORWARD chain proved broken: rebuild the network so
  # the (now-iptables) backend plants its rules, keeping running guests
  # attached across the bridge teardown.
  if ((FIX && NEED_NET_RESTART)); then
    mapfile -t taps < <(ip -br link show master "$bridge" 2>/dev/null | awk '{print $1}')
    sudo virsh net-destroy "$net" >/dev/null && sudo virsh net-start "$net" >/dev/null \
      && ok "restarted network '$net'"
    for tap in "${taps[@]}"; do
      ip link show "$tap" >/dev/null 2>&1 || continue   # guest gone meanwhile
      sudo ip link set "$tap" master "$bridge" && sudo ip link set "$tap" up \
        && ok "reattached $tap to $bridge"
    done
    if sudo iptables -S FORWARD | grep -q -- '-j LIBVIRT_FW'; then
      ok "LIBVIRT_FW* jumps present after restart"
      FAILED=0
    else
      bad "LIBVIRT_FW* jumps still missing after restart — inspect 'sudo iptables -S FORWARD'"
    fi
  fi
done

((FAILED)) && { echo; echo "run again with --fix to repair"; exit 1; }
exit 0
