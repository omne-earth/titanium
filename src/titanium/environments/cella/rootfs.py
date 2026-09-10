"""Turning an exported root filesystem into the ext4 image Cella boots.

Constraints this design answers, in order:

* No root. No ``sudo``. No loop device. No host filesystem is attached to
  build the task rootfs. The invoking user's own rootless Podman user
  namespace is the whole privilege budget.
* ``mkfs.ext4`` is not a host dependency. It lives in one small builder image,
  built once from a pinned base and cached by tag.
* Ownership is the image's, not the operator's. ``podman export`` records
  numeric uid/gid/mode; extracting as root *inside the user namespace* with
  ``--numeric-owner`` puts those same numbers into the filesystem.

The uid ceiling is real and deliberate: a rootless user namespace maps only
the delegated sub-uid range (65536 entries on a default Fedora host), so an
image path owned above that range makes ``tar`` fail and the conversion fail
with it. That is the correct outcome. Remapping such a path to something that
fits would hand the guest a file with ownership its author did not write.

On top of the extracted tree this module places Titanium's boot layer: a
declared set of files and symlinks, at the paths the layer names. Placement is
all this module does with them. It renders no entry, invents none, and holds
no opinion about what boots -- ``/sbin/init`` is not special here, it is one
path a boot layer may or may not declare.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from pathlib import Path

from titanium.environments.cella.boot_layer import (
    BootLayer,
    GuestFile,
    GuestSymlink,
    validate_boot_layer,
)
from titanium.environments.cella.podman import (
    PodmanError,
    image_exists,
    inspect_image,
    require_podman,
    run_podman,
)

# The base is pinned and explicit. Never a floating tag: the filesystem this
# produces is the artifact a whole trial is graded from, and "whatever alpine
# meant this week" is not a reproducible input.
ROOTFS_BUILDER_BASE_IMAGE = "docker.io/library/alpine:3.22"

# The cached builder. The tag's trailing integer is the recipe version: change
# ROOTFS_BUILDER_CONTAINERFILE and bump it, so a host never keeps serving a
# builder made from an older recipe under the same name.
ROOTFS_BUILDER_IMAGE = "localhost/titanium-cella-rootfs-builder:3"

# GNU tar explicitly: Alpine's default tar is busybox's, which has no
# --numeric-owner, and silently losing numeric ownership is exactly the
# failure this pipeline exists to prevent. fuse2fs rides along for the
# evidence-extraction container the cella environment runs (see
# environment._harvest): the same pinned image serves both directions,
# writing filesystems and reading them.
ROOTFS_BUILDER_CONTAINERFILE = (
    f"FROM {ROOTFS_BUILDER_BASE_IMAGE}\n"
    # e2fsprogs-extra carries debugfs, which the harvest uses for
    # targeted single-file reads without mounting anything.
    "RUN apk add --no-cache e2fsprogs e2fsprogs-extra tar fuse2fs\n"
)

# Where the tree being populated lives inside the builder container. Every
# boot entry lands under it, and the placement helpers below exist to keep
# that true.
_BUILD_ROOT = "/work/root"

# Names inside the builder container's mounts.
_IN_TAR_NAME = "rootfs.tar"
_IN_ENTRY_PREFIX = "entry-"


class RootfsBuildError(RuntimeError):
    """The task root filesystem could not be built."""


def sha3_256_file(path: Path) -> str:
    """Streamed sha3-256 of a file, lowercase hex.

    Matches ``cella_libs::golden::sha3_256_hex`` -- the digest a Cella manifest
    records and the one ``cella doctor verify`` recomputes.
    """
    digest = hashlib.sha3_256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_rootfs_builder_image(*, timeout_sec: float | None = None) -> str:
    """Build the cached ext4 builder if it is absent, and return its tag.

    Present is done: this is a no-op on every conversion after the first, so
    no package manager runs per build.

    Absent means built from ``ROOTFS_BUILDER_BASE_IMAGE``, and nothing else.
    There is no second tool, no host ``mkfs.ext4``, and no substitute image: a
    builder that cannot be built is a failed conversion, because every
    alternative available here is a weaker one.
    """
    if image_exists(ROOTFS_BUILDER_IMAGE):
        return ROOTFS_BUILDER_IMAGE

    import tempfile

    with tempfile.TemporaryDirectory(prefix="titanium-cella-builder-") as staging:
        containerfile = Path(staging) / "Dockerfile"
        containerfile.write_text(ROOTFS_BUILDER_CONTAINERFILE)
        run_podman(
            [
                "build",
                "-f",
                str(containerfile),
                "-t",
                ROOTFS_BUILDER_IMAGE,
                staging,
            ],
            timeout_sec=timeout_sec,
        )
    return ROOTFS_BUILDER_IMAGE


def rootfs_builder_image_id(*, timeout_sec: float | None = None) -> str:
    """The content id of the builder image on this host.

    The tag and the recipe together do not prove two hosts hold the same
    builder: ``apk add e2fsprogs`` resolves to whatever Alpine's index offered
    that day, and the tag says nothing about it. The image id does.

    Resolved rather than assumed, and then *used* -- :func:`build_ext4` runs
    the container by id, so the value reported here is the image that actually
    produced the filesystem, not a tag that might point somewhere else by then.

    Whether this belongs in a flavor's cache identity is not decided here.
    """
    ensure_rootfs_builder_image(timeout_sec=timeout_sec)
    record = inspect_image(ROOTFS_BUILDER_IMAGE, timeout_sec=timeout_sec)
    if isinstance(record, list):
        if len(record) != 1:
            raise RootfsBuildError(
                f"Expected one inspect record for {ROOTFS_BUILDER_IMAGE}, got "
                f"{len(record)}."
            )
        record = record[0]
    image_id = record.get("Id") if isinstance(record, dict) else None
    if not isinstance(image_id, str) or not image_id:
        raise RootfsBuildError(
            f"{ROOTFS_BUILDER_IMAGE} inspect record carries no 'Id'."
        )
    return image_id


def _staged_entry_name(index: int) -> str:
    """The name a boot entry's contents are staged under inside ``/in``.

    Derived from the entry's position, never from its guest path. A guest path
    is the boot layer's data; letting it name a host file would let a layer
    choose what the builder reads.
    """
    return f"{_IN_ENTRY_PREFIX}{index:04d}"


# Placement helpers, defined once in the script and called per entry.
#
# Both exist for one reason: the tree they write into came out of an untrusted
# `podman export`, so any component of any destination may already be
# something the image chose. Neither helper follows what it finds.
#
# `mkdir -p` is specifically not used. It succeeds through a symlinked
# component, so an exported image shipping `/etc -> /` would silently redirect
# a boot entry out of the tree being built and into the builder container.
_PLACEMENT_HELPERS = """root={root}
place_parents() {{
    dir=$root
    saved_ifs=$IFS
    IFS=/
    for component in ${{1%/*}}; do
        [ -n "$component" ] || continue
        dir=$dir/$component
        if [ -L "$dir" ]; then
            echo "boot layer: $dir is a symlink; refusing to place through it" >&2
            exit 1
        fi
        if [ -e "$dir" ] && [ ! -d "$dir" ]; then
            echo "boot layer: $dir exists and is not a directory" >&2
            exit 1
        fi
        [ -d "$dir" ] || mkdir "$dir"
    done
    IFS=$saved_ifs
}}
clear_leaf() {{
    if [ -L "$root$1" ] || [ -f "$root$1" ]; then
        rm -f "$root$1"
    elif [ -e "$root$1" ]; then
        echo "boot layer: $root$1 exists and is not a regular file" >&2
        exit 1
    fi
}}"""


def _place_entry(
    index: int, entry: GuestFile | GuestSymlink, root: str = _BUILD_ROOT
) -> list[str]:
    """The shell lines that put one boot entry in place.

    Every value that came from the boot layer is quoted with
    :func:`shlex.quote`. A guest path and a symlink target are data; a builder
    script that pasted either in raw would be running it instead.
    """
    guest_path = shlex.quote(entry.path)
    built_path = shlex.quote(f"{root}{entry.path}")
    lines = [
        f"place_parents {guest_path}",
        f"clear_leaf {guest_path}",
    ]
    if isinstance(entry, GuestFile):
        # One `install`, and the order inside it matters: chmod after chown
        # clears setuid and setgid. Measured on the builder's busybox --
        # `install -m 4755 -o 1234` keeps the bit, a hand-rolled
        # cp/chmod/chown chain in the wrong order drops it silently.
        lines.append(
            f"install -m {entry.mode:04o} -o {entry.uid} -g {entry.gid} "
            f"/in/{_staged_entry_name(index)} {built_path}"
        )
    else:
        # `--` because a target may legitimately begin with a dash, and `-h`
        # so the ownership lands on the link. Measured: a plain chown here
        # follows the link and retitles whatever it points at instead.
        lines.append(f"ln -s -- {shlex.quote(entry.target)} {built_path}")
        lines.append(f"chown -h {entry.uid}:{entry.gid} {built_path}")
    return lines


def _mkfs_script(*, size_bytes: int, boot_layer: BootLayer | None) -> str:
    """The builder container's whole program.

    ``mkfs.ext4 -d`` populates an image from a directory tree without
    attaching it anywhere -- which is what keeps this path free of loop
    devices and elevated privilege.

    ``-f`` joins ``-eu`` because the parent walk splits a path on ``/`` with
    ``IFS``, and an unguarded expansion there would glob against the tree
    being built.
    """
    lines = [
        "set -euf",
        f"mkdir -p {_BUILD_ROOT}",
        f"tar -xpf /in/{_IN_TAR_NAME} -C {_BUILD_ROOT} --numeric-owner",
    ]
    if boot_layer is not None and boot_layer.entries:
        lines.append(_PLACEMENT_HELPERS.format(root=_BUILD_ROOT))
        for index, entry in enumerate(boot_layer.entries):
            lines += _place_entry(index, entry, _BUILD_ROOT)
    lines += [
        # Sparse backing at the declared capacity: the guest sees the size the
        # task asked for, and the host stores only what mkfs actually wrote.
        f"truncate -s {size_bytes} /out/rootfs.ext4",
        f"mkfs.ext4 -F -q -d {_BUILD_ROOT} /out/rootfs.ext4",
    ]
    return "\n".join(lines)


def build_ext4(
    *,
    rootfs_tar: Path,
    boot_layer: BootLayer | None,
    size_bytes: int,
    dest: Path,
    builder_image: str | None = None,
    timeout_sec: float | None = None,
    runtime: str | None = None,
) -> None:
    """Build an ext4 image at *dest* from an exported root filesystem tar.

    Args:
        rootfs_tar: A ``podman export`` tar.
        boot_layer: The files and symlinks Titanium contributes, or ``None``
            to place nothing and leave the exported tree as it came. Deciding
            what the entries are is not this module's business; placing them
            is. Revalidated here even though the converter already validated
            it, because this function is public and placement runs against an
            untrusted tree.
        size_bytes: The filesystem's capacity, in bytes. Supplied by the
            caller from the task's declared storage. This module invents no
            sizing heuristic: a capacity the task did not ask for is a task
            running under conditions its author did not describe.
        dest: Where the image lands. Its parent directory is what the builder
            container writes into, so it must already exist and must hold
            nothing else the caller cares about.
        builder_image: The builder to run, by id. Callers that record which
            builder produced an artifact pass the id they recorded, so the two
            cannot drift. ``None`` resolves the cached builder here.
        runtime: A podman ``--runtime`` override, or ``None`` for podman's
            default. The cella environment passes ``"krun"`` so the tar this
            builder extracts -- guest-produced bytes on the exec-cycle path --
            is parsed inside a KVM microVM rather than by a host-side
            container runtime alone.

    Raises:
        RootfsBuildError: on any bad input.
        TypeError, BootLayerError: when *boot_layer* is not an installable
            boot layer.
        PodmanError: when the builder container fails -- including the case
            where the image declares a uid above the delegated sub-uid range,
            and the case where a boot entry's destination is blocked by a
            symlinked parent or by an existing directory.
    """
    if size_bytes <= 0:
        raise RootfsBuildError(f"ext4 capacity must be positive, got {size_bytes}.")
    if not rootfs_tar.is_file():
        raise RootfsBuildError(f"No exported root filesystem at {rootfs_tar}.")
    if dest.exists():
        raise RootfsBuildError(
            f"{dest} already exists; refusing to overwrite a filesystem image."
        )
    out_dir = dest.parent
    if not out_dir.is_dir():
        raise RootfsBuildError(f"Output directory {out_dir} does not exist.")
    if boot_layer is not None:
        validate_boot_layer(boot_layer)

    builder = builder_image or ensure_rootfs_builder_image(timeout_sec=timeout_sec)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="titanium-cella-mkfs-") as staging:
        staging_dir = Path(staging)
        # Hard-linked when possible so a multi-gigabyte export is not copied
        # just to be handed to a bind mount.
        staged_tar = staging_dir / _IN_TAR_NAME
        try:
            staged_tar.hardlink_to(rootfs_tar)
        except OSError:
            shutil.copyfile(rootfs_tar, staged_tar)

        if boot_layer is not None:
            for index, entry in enumerate(boot_layer.entries):
                if isinstance(entry, GuestFile):
                    staged = staging_dir / _staged_entry_name(index)
                    staged.write_bytes(entry.contents)

        runtime_args = [] if runtime is None else ["--runtime", runtime]
        run_podman(
            [
                "run",
                "--rm",
                *runtime_args,
                # The builder resolves nothing and fetches nothing.
                "--network=none",
                "-v",
                f"{staging_dir}:/in:ro,z",
                # `z` on the output as well: out_dir is the converter's own
                # staging directory (`.tmp-<random>`), unlabeled at birth, so
                # on an enforcing host the builder cannot write into it at
                # all. The published flavor's label remains Cella's security
                # phase's to set -- tasks/PHASE2-security.md (1.6.14h) is what
                # implements the labels on the artifact home -- and what gets
                # relabeled here is a private directory that exists only until
                # publication renames it into place.
                "-v",
                f"{out_dir}:/out:z",
                builder,
                "sh",
                "-c",
                _mkfs_script(size_bytes=size_bytes, boot_layer=boot_layer),
            ],
            timeout_sec=timeout_sec,
        )

    if not dest.is_file():
        raise RootfsBuildError(
            f"The builder reported success but produced no image at {dest}."
        )


# Where a live-edited image mounts inside the placement container.
_PLACE_ROOT = "/work/mnt"


def place_into_ext4(
    *,
    image: Path,
    boot_layer: BootLayer,
    purge: tuple[str, ...] = (),
    builder_image: str | None = None,
    timeout_sec: float | None = None,
    runtime: str | None = "krun",
) -> None:
    """Place a boot layer into an existing ext4 image, in place.

    The disk-to-disk path of the cella environment's exec cycle: the
    previous cycle's evidence copy *is* the next machine's filesystem,
    and only the new boot-layer entries change. The image is mounted
    with ``fuse2fs`` inside the builder container -- under krun by
    default, because from the second cycle on the image's contents are
    guest-produced and ext4 metadata is parser input (see
    docs/environments/CELLA.md, "Why krun was needed") -- and the same
    placement helpers as :func:`build_ext4` write the entries, with the
    same refusals: no ``mkdir -p`` through symlinks, every layer value
    quoted.

    Args:
        image: The ext4 image to edit. The caller owns it; this is
            never a machine's live ``disk.img``, only titanium's copy.
        boot_layer: The entries to place. Revalidated here.
        purge: Absolute guest paths removed before placement. The
            cella environment purges the previous cycle's result
            directory here: the image carries the whole prior state,
            and a stale result file would answer the host's completion
            poll before the new guest ever ran.
        builder_image: As in :func:`build_ext4`.
        timeout_sec: Applied to the podman invocation.
        runtime: Podman ``--runtime``; ``None`` for podman's default.

    Raises:
        RootfsBuildError, TypeError, BootLayerError, PodmanError.
    """
    if not image.is_file():
        raise RootfsBuildError(f"No ext4 image at {image}.")
    validate_boot_layer(boot_layer)
    if not boot_layer.entries:
        return

    builder = builder_image or ensure_rootfs_builder_image(timeout_sec=timeout_sec)

    import tempfile

    lines = [
        "set -euf",
        f"mkdir -p {_PLACE_ROOT}",
        # Replay any dirty journal before editing: entries placed on a
        # dirty image are silently undone when the next guest kernel
        # replays the stale journal over them (measured).
        "e2fsck -fy /img >/dev/null 2>&1 || true",
        # fakeroot for full access; rw is the point. fuse2fs cannot
        # write the journal and says so on stderr; harmless here.
        f"fuse2fs -o fakeroot /img {_PLACE_ROOT}",
        _PLACEMENT_HELPERS.format(root=_PLACE_ROOT),
    ]
    for path in purge:
        if not path.startswith("/") or ".." in path:
            raise RootfsBuildError(f"purge path {path!r} is not guest-absolute.")
        lines.append(f"rm -rf {shlex.quote(_PLACE_ROOT + path)}")
    for index, entry in enumerate(boot_layer.entries):
        lines += _place_entry(index, entry, _PLACE_ROOT)
    lines += [f"umount {_PLACE_ROOT}"]

    with tempfile.TemporaryDirectory(prefix="titanium-cella-place-") as staging:
        staging_dir = Path(staging)
        for index, entry in enumerate(boot_layer.entries):
            if isinstance(entry, GuestFile):
                (staging_dir / _staged_entry_name(index)).write_bytes(entry.contents)
        runtime_args = [] if runtime is None else ["--runtime", runtime]
        run_podman(
            [
                "run",
                "--rm",
                *runtime_args,
                "--network=none",
                "--device",
                "/dev/fuse",
                "-v",
                f"{staging_dir}:/in:ro,z",
                "-v",
                f"{image}:/img:z",
                builder,
                "sh",
                "-c",
                "\n".join(lines),
            ],
            timeout_sec=timeout_sec,
        )


__all__ = [
    "ROOTFS_BUILDER_BASE_IMAGE",
    "ROOTFS_BUILDER_CONTAINERFILE",
    "ROOTFS_BUILDER_IMAGE",
    "PodmanError",
    "RootfsBuildError",
    "build_ext4",
    "ensure_rootfs_builder_image",
    "place_into_ext4",
    "require_podman",
    "rootfs_builder_image_id",
    "sha3_256_file",
]
