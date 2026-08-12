#!/bin/bash
set -ueox pipefail

# Fedora host prerequisites required before `make init` can run.
sudo dnf install -y make podman

exec make init