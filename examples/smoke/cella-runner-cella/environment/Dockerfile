# The subject of smoke-cella-runner-cella. A stock Debian with neither
# systemd nor /sbin/init: the rootfs conversion's provisioning policy is on
# the path, on the host, before the tar is seeded into the outer guest.
# python3 is baked because the probe and the offline verifier are
# stdlib-only and the inner guest has no network.
FROM docker.io/library/debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
