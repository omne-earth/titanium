# The subject of smoke-cella-policy-engine-airgapped.
#
# Deliberately a stock Debian that ships neither systemd nor /sbin/init:
# the rootfs conversion's provisioning policy is on the path, exactly as
# in `make smoke-cella-rootfs`. python3 is baked because the probe and
# the offline verifier are stdlib-only and the guest has no network to
# install anything from -- which is the point of the task.
FROM docker.io/library/debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
