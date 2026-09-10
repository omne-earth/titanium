# The subject of smoke-cella (verify-cella-env-*).
#
# Deliberately a stock Debian that ships neither systemd nor
# /sbin/init: the rootfs conversion's provisioning policy is on the
# path. python3 baked because the probe and the offline verifier are
# stdlib-only.
FROM docker.io/library/debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
