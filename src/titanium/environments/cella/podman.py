"""Podman, at build time only.

Every call here runs during conversion. Nothing in this module reaches a
running workload: Cella boots a kernel and an ext4, and no container engine,
runtime, or spec exists at run time (Cella's ``docs/integration/TITANIUM.md``,
rule 3).

Rootless throughout -- no daemon, no socket, no ``sudo``, no ``--privileged``.
The binary is selected by ``TITANIUM_PODMAN_BIN``, the same knob
``PodmanEnvironment`` uses, so a host has one Podman selector rather than two.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from titanium.environments.cella.constants import IMAGE_FORMAT


class PodmanError(RuntimeError):
    """A podman invocation failed. Carries the command and its output."""


def podman_bin() -> str:
    return os.environ.get("TITANIUM_PODMAN_BIN", "podman")


def require_podman() -> str:
    binary = podman_bin()
    resolved = shutil.which(binary)
    if resolved is None:
        raise PodmanError(
            f"{binary!r} is not installed or not on PATH. Install Podman, or "
            "point TITANIUM_PODMAN_BIN at the binary."
        )
    return resolved


def run_podman(
    args: Sequence[str],
    *,
    timeout_sec: float | None = None,
    stdout_path: Path | None = None,
) -> str:
    """Run one podman command and return its stdout.

    Any nonzero exit raises. There is no ``check=False`` mode and no
    best-effort variant: a build, export, or filesystem step that failed must
    not leave the caller deciding whether the failure mattered.

    Args:
        stdout_path: When given, stdout is streamed to this file instead of
            captured -- an exported root filesystem does not belong in memory.
    """
    binary = require_podman()
    command = [binary, *args]

    if stdout_path is not None:
        with stdout_path.open("wb") as sink:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.PIPE,
                timeout=timeout_sec,
        check=False,
    )
        stdout = ""
        stderr = completed.stderr.decode(errors="replace")
    else:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_sec,
        check=False,
    )
        stdout = completed.stdout.decode(errors="replace")
        stderr = completed.stderr.decode(errors="replace")

    if completed.returncode != 0:
        raise PodmanError(
            f"`podman {' '.join(args)}` failed ({completed.returncode}): "
            f"{stderr.strip() or stdout.strip() or 'no output'}"
        )
    return stdout


def image_exists(reference: str) -> bool:
    binary = require_podman()
    completed = subprocess.run(
        [binary, "image", "exists", reference],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def new_build_tag(prefix: str = "titanium-cella-build") -> str:
    """A tag no other build shares, so cleanup can untag without collateral."""
    return f"localhost/{prefix}:{uuid.uuid4().hex}"


def build_image(
    *,
    context_dir: Path,
    build_file: Path,
    tag: str,
    timeout_sec: float | None = None,
    pull: str | None = None,
) -> None:
    """``podman build`` the staged context under *tag*.

    Args:
        pull: Podman's ``--pull`` policy, passed through only when given.
            Tests use ``"never"`` to stay offline; production leaves it unset
            so podman's own default applies.
    """
    args = [
        "build",
        "--format",
        IMAGE_FORMAT,
        "-f",
        str(build_file),
        "-t",
        tag,
    ]
    if pull is not None:
        # `--pull=<policy>`, joined: podman gives the flag a no-opt default, so
        # a separated value is parsed as a positional argument instead.
        args.append(f"--pull={pull}")
    args.append(str(context_dir))
    run_podman(args, timeout_sec=timeout_sec)


def inspect_image(reference: str, *, timeout_sec: float | None = None) -> Any:
    """The whole ``podman image inspect`` record, parsed but unfiltered."""
    import json

    raw = run_podman(["image", "inspect", reference], timeout_sec=timeout_sec)
    return json.loads(raw)


def untag_image(tag: str) -> None:
    """Drop a tag this converter created.

    Untag, never remove: the underlying image may be the layer cache of a
    build the operator wants, or -- on a cache hit -- an image that existed
    before this conversion. Removing it would be collateral damage. Whether a
    now-dangling image is pruned is the operator's call.
    """
    binary = require_podman()
    subprocess.run(
        [binary, "image", "untag", tag],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def export_rootfs_tar(
    *,
    image: str,
    dest_tar: Path,
    timeout_sec: float | None = None,
) -> None:
    """Flatten *image* into a tar at *dest_tar*.

    ``podman create`` + ``podman export`` is the honest transport: the tar
    records the image's numeric uid, gid, and mode for every path, so the
    filesystem built from it carries the ownership the task declared rather
    than the ownership of whoever ran the conversion.

    The temporary container is removed on every path, including failure.
    """
    container_id = run_podman(["create", image], timeout_sec=timeout_sec).strip()
    if not container_id:
        raise PodmanError(f"`podman create {image}` returned no container id")
    try:
        run_podman(
            ["export", container_id],
            timeout_sec=timeout_sec,
            stdout_path=dest_tar,
        )
    finally:
        binary = require_podman()
        subprocess.run(
            [binary, "rm", "-f", container_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        check=False,
    )
