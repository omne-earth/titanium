"""Titanium's boot layer: the filesystem entries it contributes to a guest.

The old seam rendered exactly one thing -- ``/sbin/init`` -- and its cardinality
was baked into every type that touched it: one ``bytes``, one path, one mode.
That shape is obsolete. The distro's ``systemd`` owns PID 1, put there by
:mod:`titanium.environments.cella.systemd_boot` before the filesystem is
exported, and what Titanium contributes on top is a *small set* of files and
symlinks. That set is currently empty.

This module is the datatype and validation half of that seam. It answers only
one question:

    is this set of entries installable into a guest filesystem at all?

It answers none of these, deliberately:

    what the entries should be
    how systemd is installed
    what ``default.target`` points at
    what a controller unit contains
    which identity anything runs as

Those are :func:`render_boot_layer`'s business, and that function is
intentionally unimplemented. Validation here is mechanical and total: a rule
that would require knowing what an entry is *for* does not belong in it.

Note the direction of the import. The obsolete init-shim seam reached back
into ``converter`` for its input type, which made the decision module depend on
the pipeline that calls it. :class:`BootLayerInputs` lives here instead, so the
seam owns its own vocabulary and the dependency runs one way.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from titanium.environments.cella.image_config import ImageRecord

# Every bit a POSIX mode can carry: setuid, setgid, sticky, and the three
# permission triads. Not a policy about which of them any entry should use.
MAX_GUEST_FILE_MODE = 0o7777


class BootLayerError(ValueError):
    """A boot layer cannot be installed into a guest filesystem as given.

    Raised for a value that is the right *type* and still unusable -- a
    relative path, a mode outside the POSIX range, two entries claiming one
    destination. A structurally wrong type raises :class:`TypeError` instead,
    matching ``identity.resolve_standard_guest_user``.
    """


@dataclass(frozen=True)
class BootLayerInputs:
    """What the boot-layer decision gets to see.

    ``image`` alone is not enough, because of one measured fact:
    ``agent_setup.dockerfile_install_commands`` emits a ``USER`` directive per
    install step and restores nothing, so the last one becomes the built
    image's ``Config.User``. A task declaring ``USER 1234:5678`` inspects as
    ``1234:5678`` with no agent and as ``root`` once an install ran. Reading
    the runtime user off ``Config.User`` would therefore silently promote an
    install-plumbing artifact into the guest's identity.

    So the explicit Titanium decision travels alongside the image, and the
    contamination is flagged rather than papered over.

    Attributes:
        image: The whole ``podman image inspect`` record.
        agent_user: Titanium's explicit runtime-user decision --
            ``[agent].user`` from task.toml, threaded through
            ``BaseEnvironment.default_user``. ``None`` carries Titanium's own
            meaning: *use the image's declared USER*.
        agent_install_applied: True when install steps rewrote the image. When
            true, ``image.config["User"]`` describes those steps, not the
            task, and resolving ``agent_user=None`` from it would be wrong.
    """

    image: ImageRecord
    agent_user: str | int | None
    agent_install_applied: bool


@dataclass(frozen=True)
class GuestFile:
    """One regular file to place in the guest filesystem.

    Attributes:
        path: An absolute guest path. Where the file lands, not where it came
            from; nothing on the host is named here.
        contents: The file's bytes, verbatim.
        mode: A POSIX mode, ``0o0000`` through ``0o7777``.
        uid: The owning uid, as a number. Numeric because the guest's NSS
            database is the image's, and a name resolved on the host would be
            a different user there -- the same reason ``podman export`` is
            extracted with ``--numeric-owner``.
        gid: The owning gid, as a number.
    """

    path: str
    contents: bytes
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class GuestSymlink:
    """One symbolic link to create in the guest filesystem.

    There is no ``mode``: Linux ignores the permission bits on a symlink, so
    carrying one would record a value nothing honors.

    Attributes:
        path: An absolute guest path -- the link itself.
        target: What the link points at. Not required to be absolute: a
            relative target is how a systemd unit directory normally refers to
            its neighbours, and rewriting one would change its meaning.
        uid: The owning uid, as a number.
        gid: The owning gid, as a number.
    """

    path: str
    target: str
    uid: int
    gid: int


#: One entry in a boot layer. New kinds are added here, and every consumer
#: that switches on the kind fails loudly rather than silently skipping.
BootEntry = GuestFile | GuestSymlink


@dataclass(frozen=True)
class BootLayer:
    """Everything Titanium contributes to the guest filesystem, in order.

    A tuple, not a list: what the decision returned is a record of that
    decision, and a consumer that could append to it could change what boots.

    Order is the caller's and is preserved. Placement runs front to back, so a
    layer that creates a directory's contents before a symlink into it can say
    so by ordering its entries.

    Attributes:
        entries: The files and symlinks to place. Empty is structurally valid
            -- whether a layer that contributes nothing is *correct* is a
            question about boot policy, which this module does not hold.
    """

    entries: tuple[BootEntry, ...]


def _require_int(value: object, *, label: str) -> int:
    """A real integer, with ``bool`` refused by name.

    ``isinstance(True, int)`` is true, so an unguarded check would accept
    ``True`` as mode ``0o1`` or uid 1. Refused the same way
    ``identity.resolve_standard_guest_user`` refuses it.
    """
    if isinstance(value, bool):
        raise BootLayerError(f"{label} must be a number, not a bool.")
    if not isinstance(value, int):
        raise TypeError(f"{label} must be an int, got {type(value).__name__}.")
    return value


def _validate_path(path: object) -> str:
    """An absolute guest destination in its one canonical spelling.

    Every alias is refused rather than resolved. ``/sbin/init``,
    ``/sbin//init``, ``/sbin/./init``, ``//sbin/init`` and ``/sbin/init/`` all
    name the same file, and a layer that spells one destination four ways is
    either confused or aiming somewhere its reader would not predict.
    Normalizing would hide which, and it would place a file at a path the
    decision never wrote down.

    Refusing aliases is also what makes duplicate detection sound: two entries
    can only collide by being spelled identically, so a string comparison
    catches every collision there is.
    """
    if not isinstance(path, str):
        raise TypeError(f"A guest path must be a str, got {type(path).__name__}.")
    if not path:
        raise BootLayerError("A guest path cannot be empty.")
    if "\x00" in path:
        raise BootLayerError(f"Guest path {path!r} contains a NUL byte.")
    if not path.startswith("/"):
        raise BootLayerError(f"Guest path {path!r} is not absolute.")
    if path == "/":
        raise BootLayerError("The guest root is not a destination a boot layer places.")

    # The leading "" is the root itself; every component after it must be a
    # real name. An empty one is a doubled or trailing separator.
    for component in path.split("/")[1:]:
        if component == "..":
            raise BootLayerError(f"Guest path {path!r} contains a '..' component.")
        if not component:
            raise BootLayerError(
                f"Guest path {path!r} is not canonical: a doubled or trailing "
                f"'/' is a second spelling of one destination."
            )
        if component == ".":
            raise BootLayerError(
                f"Guest path {path!r} is not canonical: a '.' component is a "
                f"second spelling of one destination."
            )
    return path


def _validate_owner(path: str, *, uid: object, gid: object) -> None:
    for label, value in (("uid", uid), ("gid", gid)):
        number = _require_int(value, label=f"{path}: {label}")
        if number < 0:
            raise BootLayerError(f"{path}: {label} cannot be negative, got {number}.")


def _validate_mode(path: str, mode: object) -> None:
    number = _require_int(mode, label=f"{path}: mode")
    if not 0 <= number <= MAX_GUEST_FILE_MODE:
        raise BootLayerError(f"{path}: mode {number:#o} is outside 0o0000..0o7777.")


def _validate_target(path: str, target: object) -> None:
    if not isinstance(target, str):
        raise TypeError(
            f"{path}: a symlink target must be a str, got {type(target).__name__}."
        )
    if not target:
        raise BootLayerError(f"{path}: a symlink target cannot be empty.")
    if "\x00" in target:
        raise BootLayerError(f"{path}: symlink target {target!r} contains a NUL byte.")


def validate_boot_layer(layer: BootLayer) -> BootLayer:
    """Check *layer* is installable, and return it unchanged.

    Returns the layer so it can wrap a renderer call directly, which is what
    makes the check impossible to skip at the seam.

    Nothing is normalized, defaulted, or repaired. A layer that arrives wrong
    leaves as an exception, because the alternative -- a quietly corrected
    path or a substituted mode -- would place a file the decision did not
    describe.

    Args:
        layer: What the boot-layer decision returned.

    Returns:
        The same object, when every entry is installable.

    Raises:
        TypeError: when the layer, its entries, or a field is not the type the
            datatypes declare.
        BootLayerError: when a well-typed value cannot be installed -- a
            relative or empty path, ``..``, a NUL byte, the guest root, a
            negative uid or gid, a bool where a number belongs, a mode outside
            the POSIX range, an empty symlink target, or two entries claiming
            one destination.
    """
    if not isinstance(layer, BootLayer):
        raise TypeError(
            f"A boot layer must be a BootLayer, got {type(layer).__name__}."
        )
    if not isinstance(layer.entries, tuple):
        raise TypeError(
            f"BootLayer.entries must be a tuple, got {type(layer.entries).__name__}."
        )

    claimed: set[str] = set()
    for entry in layer.entries:
        if isinstance(entry, GuestFile):
            path = _validate_path(entry.path)
            if not isinstance(entry.contents, bytes):
                raise TypeError(
                    f"{path}: file contents must be bytes, got "
                    f"{type(entry.contents).__name__}."
                )
            _validate_mode(path, entry.mode)
            _validate_owner(path, uid=entry.uid, gid=entry.gid)
        elif isinstance(entry, GuestSymlink):
            path = _validate_path(entry.path)
            _validate_target(path, entry.target)
            _validate_owner(path, uid=entry.uid, gid=entry.gid)
        else:
            raise TypeError(
                f"A boot entry must be a GuestFile or GuestSymlink, got "
                f"{type(entry).__name__}."
            )

        # Two entries for one destination is a decision that contradicts
        # itself. Last-write-wins would resolve it by picking one at random
        # from the reader's point of view.
        if path in claimed:
            raise BootLayerError(f"Two boot entries claim {path!r}.")
        claimed.add(path)

    return layer


def _absorb(digest, *parts: bytes) -> None:
    """Feed length-prefixed parts, so no value can imitate a field boundary."""
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)


def boot_layer_digest(layer: BootLayer) -> str:
    """A deterministic sha3-256 over everything the layer would place.

    Same layer, same digest, in every process and on every host: entries are
    hashed in their own order, which the layer already defines as meaningful,
    and every field is length-prefixed so a path ending in what looks like a
    separator cannot be confused with the field after it.

    This is what lets a published artifact be checked against the layer that
    produced it. A controller binary that changed by one byte, or a unit file
    with one extra line, lands here as a different digest.
    """
    validate_boot_layer(layer)
    digest = hashlib.sha3_256()
    _absorb(digest, b"titanium-boot-layer-v1")
    digest.update(len(layer.entries).to_bytes(8, "big"))
    for entry in layer.entries:
        if isinstance(entry, GuestFile):
            _absorb(
                digest,
                b"file",
                entry.path.encode("utf-8"),
                entry.contents,
                entry.mode.to_bytes(8, "big"),
                entry.uid.to_bytes(8, "big"),
                entry.gid.to_bytes(8, "big"),
            )
        else:
            _absorb(
                digest,
                b"symlink",
                entry.path.encode("utf-8"),
                entry.target.encode("utf-8"),
                entry.uid.to_bytes(8, "big"),
                entry.gid.to_bytes(8, "big"),
            )
    return digest.hexdigest()


def render_boot_layer(inputs: BootLayerInputs) -> BootLayer:
    """Return the boot layer Titanium contributes to this guest.

    Empty, and correctly so. The distro's own systemd is PID 1, and it is put
    there by the provisioning stage in
    :mod:`titanium.environments.cella.systemd_boot`, which changes the
    filesystem before it is exported -- not by a file placed on top of it
    afterwards. There is nothing left for this layer to contribute.

    Controller boot material is supplied separately. Until it exists there is
    nothing to put here, and a placeholder unit would be a boot policy
    invented by scaffolding.

    Args:
        inputs: The built image's whole inspect record, plus Titanium's
            explicit runtime-user decision and whether agent install steps
            rewrote the image. Unused while the layer is empty, and kept
            because controller entries depend on all three.
    """
    return BootLayer(entries=())


__all__ = [
    "MAX_GUEST_FILE_MODE",
    "BootEntry",
    "BootLayer",
    "BootLayerError",
    "BootLayerInputs",
    "GuestFile",
    "GuestSymlink",
    "boot_layer_digest",
    "render_boot_layer",
    "validate_boot_layer",
]
