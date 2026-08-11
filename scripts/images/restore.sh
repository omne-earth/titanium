#!/usr/bin/env bash
# Restore a vendor.sh archive on an (airgapped) host: after this, every image
# the task set references resolves locally and nothing is pulled.
set -ueo pipefail
ARCHIVE=${1:?usage: restore.sh <archive.tar>}
podman load -i "$ARCHIVE"
