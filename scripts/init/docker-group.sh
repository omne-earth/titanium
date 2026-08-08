#!/bin/bash
# Final init step: the docker group .docker granted only goes live in a new
# login session. gVisor talks to the docker socket, so a stale session can't
# run it. Reboot to activate; a no-op once the group is already live.
set -uo pipefail

if docker ps >/dev/null 2>&1; then
  echo "init complete — try: make smoke-env BACKEND=openrouter"
  exit 0
fi

if id -nG "$USER" | grep -qw docker; then
  echo "docker group added but not active in this login session."
  echo "gvisor runs need it live; a reboot activates it."
  if [ -t 0 ]; then
    read -rp "reboot now? [y/N] " ans
    case "$ans" in
      y | Y) sudo reboot ;;
    esac
  fi
  echo "reboot when ready, then: make smoke-env BACKEND=openrouter"
  exit 0
fi

echo "init complete, but docker is not usable — check scripts/init/docker.sh output"
