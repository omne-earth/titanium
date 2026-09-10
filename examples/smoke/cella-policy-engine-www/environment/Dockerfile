# The subject of smoke-cella-policy-engine-www.
#
# The same stock Debian as the airgapped variant: no init, so the
# rootfs conversion's provisioning policy runs; python3 baked because
# the probe and the offline verifier are stdlib-only.
FROM docker.io/library/debian:12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
