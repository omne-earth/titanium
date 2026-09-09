#!/usr/bin/env python3
"""Drive Titanium's Cella rootfs conversion once, for `make smoke-cella-rootfs`.

This file exists because `convert_task_to_rootfs_flavor` takes two of its
decisions as injected callables, so it cannot be invoked from shell:

    render_boot_layer        the probe unit below -- SMOKE-ONLY
    compute_flavor_identity  a fixed name -- SMOKE-ONLY

Both are scaffolding for a disposable CELLA_HOME. Neither is a production
policy and neither is written into the package: production
``render_boot_layer`` stays empty, and no production ``compute_flavor_identity``
exists yet. Do not promote either of these.

The provisioning policy is *not* stubbed. ``plan_systemd_provisioning`` is the
real one, so the derived podman build and the real package manager inside it
are what run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from titanium.environments.cella.boot_layer import BootLayer, GuestFile, GuestSymlink
from titanium.environments.cella.converter import (
    BuildFacts,
    FlavorIdentity,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.systemd_boot import plan_systemd_provisioning

# SMOKE-ONLY. One oneshot unit, whose whole job is to let the host tell a guest
# that booted from a guest that did not. It reads PID 1's own name out of /proc
# rather than announcing it, so the evidence comes from the kernel's view of the
# process tree and not from a string this file chose. Adapted from the donor
# prototype's A2 boot proof (a08fc08, tests/test_cella_systemd_boot.py). A boot
# proof only: freeze and thaw use Cella's own evidence and add nothing here.
PROBE_MARKER = "TITANIUM_CELLA_ROOTFS_PROBE"
PROBE_UNIT = f"""\
[Unit]
Description=Titanium Cella rootfs boot proof
After=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo {PROBE_MARKER}; echo PID1=$(cat /proc/1/comm)'
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
""".encode()


def probe_boot_layer(_inputs) -> BootLayer:
    """SMOKE-ONLY. The unit, plus the symlink that is how systemd enablement is
    actually spelled: a relative link from the target's .wants directory back
    to the unit."""
    return BootLayer(
        entries=(
            GuestFile(
                path="/etc/systemd/system/titanium-rootfs-probe.service",
                contents=PROBE_UNIT,
                mode=0o644,
                uid=0,
                gid=0,
            ),
            GuestSymlink(
                path="/etc/systemd/system/multi-user.target.wants/titanium-rootfs-probe.service",
                target="../titanium-rootfs-probe.service",
                uid=0,
                gid=0,
            ),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--flavor", required=True)
    parser.add_argument("--size-bytes", required=True, type=int)
    args = parser.parse_args()

    seen: dict[str, BuildFacts] = {}

    def identity(facts: BuildFacts) -> FlavorIdentity:
        """SMOKE-ONLY. A fixed name, pinning nothing: choosing which of `facts`'
        fields belong in a cache key is the production flavor-identity
        decision, and this smoke is not where it gets made."""
        seen["facts"] = facts
        return FlavorIdentity(flavor_name=args.flavor, manifest_fields={})

    result = convert_task_to_rootfs_flavor(
        environment_dir=args.environment,
        ext4_size_bytes=args.size_bytes,
        render_boot_layer=probe_boot_layer,
        compute_flavor_identity=identity,
        plan_systemd_provisioning=plan_systemd_provisioning,
        home=args.home,
        build_timeout_sec=1800.0,
    )

    facts = seen["facts"]
    source, final = facts.systemd_source_os, facts.systemd_final_os
    print(f"[convert] subject  : {source.pretty_name!r}")
    print(
        f"[convert] source   : bootable={source.systemd_bootable} "
        f"init={source.init_resolved_path!r}"
    )
    print(f"[convert] strategy : {facts.systemd_strategy} @ {facts.boot_image_id}")
    print(
        f"[convert] final    : bootable={final.systemd_bootable} "
        f"init={final.init_resolved_path!r}"
    )
    print(f"[convert] artifact : {result.artifact_path} ({result.size_bytes} bytes)")
    print(f"[convert] sha3-256 : {result.sha3_256}")

    # The claim, checked rather than assumed: the subject really was a guest
    # that could not boot, and the conversion really did fix it -- both read
    # off exported tars, never predicted. The artifact itself needs no check
    # here; the converter re-reads the manifest and re-hashes it before
    # publishing, and raises rather than returning.
    failed = False
    for wrong, message in (
        (
            source.systemd_bootable,
            "the subject was already systemd-bootable; the policy never ran",
        ),
        (source.init_present, "the subject already carried a /sbin/init"),
        (
            facts.systemd_strategy != "debian-systemd",
            f"expected the debian-systemd strategy, got {facts.systemd_strategy!r}",
        ),
        (
            not final.systemd_bootable,
            "the conversion published a filesystem it did not prove bootable",
        ),
    ):
        if wrong:
            print(f"FAIL: {message}", file=sys.stderr)
            failed = True

    if failed:
        return 1
    print("[convert] OK: a non-bootable Debian became a systemd-bootable ext4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
