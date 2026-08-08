#!/bin/bash
set -ueox pipefail

# Idempotent: every step checks state before changing it, so re-runs are no-ops.
if ! command -v docker >/dev/null; then
  sudo dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo --overwrite
  sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl is-active -q docker || sudo systemctl enable --now docker
id -nG "$USER" | grep -qw docker || sudo usermod -aG docker "$USER"
