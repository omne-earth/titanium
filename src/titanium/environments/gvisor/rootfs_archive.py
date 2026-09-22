"""Read and classify entries from a gVisor rootfs-upper tar.

This module does not extract archive contents onto the host.
"""

import copy
import hashlib
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from titanium.environments.base import ArchiveError

UpperAction = Literal["root", "replace", "delete"]


def normalize_member_path(name: str) -> str:
    """Convert a tar entry name to a root-relative filesystem path."""

    if not name or "\x00" in name or name.startswith("/"):
        raise ArchiveError(f"Invalid archive member path: {name!r}")

    parts = name.split("/")

    if ".." in parts:
        raise ArchiveError(f"Archive member escapes its root: {name!r}")

    normalized = "/".join(
        part for part in parts if part not in ("", ".")
    )

    if not normalized:
        if name in (".", "./"):
            return "."
        raise ArchiveError(f"Invalid archive member path: {name!r}")

    return normalized


def validate_member_links(member: tarfile.TarInfo) -> None:
    """Check link metadata without following a link on the host."""

    if member.issym():
        if not member.linkname or "\x00" in member.linkname:
            raise ArchiveError(
                f"Invalid symlink target at {member.name!r}"
            )

    elif member.islnk():
        target = normalize_member_path(member.linkname)

        if target == ".":
            raise ArchiveError(
                f"Invalid hardlink target at {member.name!r}"
            )


def classify_upper_member(
    member: tarfile.TarInfo,
) -> tuple[str, UpperAction]:
    """Return the normalized path and meaning of one upper-layer entry."""

    path = normalize_member_path(member.name)

    if path == ".":
        if not member.isdir():
            raise ArchiveError("The archive root must be a directory")
        return path, "root"

    if member.ischr():
        if (member.devmajor, member.devminor) == (0, 0):
            return path, "delete"

        raise ArchiveError(
            f"Unsupported character-device entry at {path!r}"
        )

    validate_member_links(member)

    if (
        member.isfile()
        or member.isdir()
        or member.issym()
        or member.islnk()
    ):
        return path, "replace"

    raise ArchiveError(
        f"Unsupported upper-layer entry type at {path!r}: "
        f"{member.type!r}"
    )

ArchiveSource = Literal["base", "upper"]

# Each selected entry remembers where its actual contents come from.
SelectedMember = tuple[ArchiveSource, tarfile.TarInfo]


def index_archive(
    archive: tarfile.TarFile,
    *,
    source: ArchiveSource,
) -> dict[str, tarfile.TarInfo]:
    """Index archive entries by their normalized filesystem paths.

    This reads entry metadata, not file contents. The input archive
    stays open so the later writer can read the selected file contents.
    """

    members: dict[str, tarfile.TarInfo] = {}

    for member in archive:
        if source == "upper":
            path, _action = classify_upper_member(member)
        else:
            path = normalize_member_path(member.name)

            if path == "." and not member.isdir():
                raise ArchiveError(
                    "The base archive root must be a directory"
                )

            validate_member_links(member)

        if path in members:
            raise ArchiveError(
                f"Duplicate path in {source} archive: {path!r}"
            )

        members[path] = member

    return members

def validate_selected_members(
    selected: dict[str, SelectedMember],
) -> None:
    """Reject impossible parent/child paths and broken hardlinks.

    This checks archive metadata only. It does not open any guest path
    on the host or extract any file.
    """

    # CHECK 1: Every selected entry must agree with its dictionary key.
    for path, (_source, member) in selected.items():
        actual_path = normalize_member_path(member.name)

        if actual_path != path:
            raise ArchiveError(
                f"Selected archive path mismatch: "
                f"{path!r} != {actual_path!r}"
            )

        if path == "." and not member.isdir():
            raise ArchiveError(
                "The reconstructed filesystem root must be a directory"
            )

    # CHECK 2: A file or symlink cannot also contain child entries.
    for path in selected:
        if path == ".":
            continue

        parent = path.rpartition("/")[0]

        while parent:
            parent_entry = selected.get(parent)

            if parent_entry is not None:
                _source, parent_member = parent_entry

                if not parent_member.isdir():
                    raise ArchiveError(
                        f"Invalid reconstructed filesystem: "
                        f"{path!r} is beneath non-directory {parent!r}"
                    )

            parent = parent.rpartition("/")[0]

    # CHECK 3: Every hardlink must eventually refer to a regular file.
    for path, (_source, member) in selected.items():
        if not member.islnk():
            continue

        visited = {path}
        target_path = normalize_member_path(member.linkname)

        while True:
            if target_path in visited:
                raise ArchiveError(
                    f"Hardlink cycle involving {path!r}"
                )

            visited.add(target_path)

            target_entry = selected.get(target_path)

            if target_entry is None:
                raise ArchiveError(
                    f"Hardlink {path!r} refers to missing "
                    f"archive entry {target_path!r}"
                )

            _target_source, target_member = target_entry

            if target_member.isfile():
                break

            if target_member.islnk():
                target_path = normalize_member_path(
                    target_member.linkname
                )
                continue

            raise ArchiveError(
                f"Hardlink {path!r} does not resolve to a regular file"
            )

def select_merged_members(
    base: dict[str, tarfile.TarInfo],
    upper: dict[str, tarfile.TarInfo],
) -> dict[str, SelectedMember]:
    """Decide which entries belong in the reconstructed filesystem.

    Return a mapping:

        normalized path -> ("base" or "upper", TarInfo)

    This function only SELECTS entries. It does not write a tar or
    extract anything onto the host.
    """

    selected: dict[str, SelectedMember] = {}

    # Start with the entries from the original filesystem.
    for path, member in base.items():
        selected[path] = ("base", member)

    # Walk through the gVisor changes.
    for path, member in upper.items():
        normalized_path, action = classify_upper_member(member)

        if normalized_path != path:
            raise ArchiveError(
                f"Upper archive index mismatch: {path!r} != {normalized_path!r}"
            )

        if action == "delete":
            selected.pop(path, None)

            pathprefix = path + "/"
            for key in list(selected.keys()):
                if key.startswith(pathprefix):
                    selected.pop(key)

        elif action == "replace" and not member.isdir():
            pathprefix = path + "/"

            for key in list(selected):
                if key.startswith(pathprefix):
                    selected.pop(key, None)


    # Handle a deleted directory or a directory replaced by a file.
    for path, member in upper.items():

        _, action = classify_upper_member(member)
        #action = classify_upper_member(member)[1]

        if action == "root" or action == "replace":
            selected[path] = ("upper", member)

    validate_selected_members(selected)
    return selected

@contextmanager
def _new_output_tar(path: Path):
    """Create a new tar; remove it if writing fails."""

    output_file = path.open("xb")

    try:
        with output_file:
            with tarfile.open(fileobj=output_file, mode="w:") as archive:
                yield archive

    except BaseException:
        path.unlink(missing_ok=True)
        raise

def merge_rootfs_archives(
    base_path: Path,
    upper_path: Path,
    output_path: Path,
) -> int:
    """Reconstruct a complete rootfs tar from a base and gVisor upper tar.

    Both inputs must remain available throughout the merge.
    Nothing is extracted onto the host filesystem.
    """

    #Open both input archives.
    with tarfile.open(base_path, mode="r:") as base_archive:
        with tarfile.open(upper_path, mode="r:") as upper_archive:

            # Index the entries in both archives.
            base_members = index_archive(
                base_archive,
                source="base",
            )

            upper_members = index_archive(
                upper_archive,
                source="upper",
            )

            #Apply your file-selection rules.
            selected = select_merged_members(
                base_members,
                upper_members,
            )

            # Reject hardlinks whose meaning could change across layers.
            for path, (source, member) in sorted(
                selected.items(),
                key=lambda item: item[1][1].islnk(),
            ):
                if member.islnk():
                    target = normalize_member_path(member.linkname)
                    target_source, target_member = selected[target]

                    if target_source != source or target_member.islnk():
                        raise ArchiveError(
                            f"Unsupported hardlink during merge: {path!r} -> {target!r}"
                        )

            # Never overwrite an existing archive or either input.
            if output_path.exists() or output_path.is_symlink():
                raise ArchiveError(f"Archive output already exists: {output_path}")

            # Write the selected entries into output_path.
            # with tarfile.open(output_path, mode="x:") as output_archive:
            with _new_output_tar(output_path) as output_archive:
                for path, (source, member) in sorted(
                    selected.items(),
                    key=lambda item: item[1][1].islnk(),
                ):
                    source_archive = base_archive if source == "base" else upper_archive

                    output_member = copy.copy(member)
                    output_member.name = path

                    if member.isfile():
                        fdata = source_archive.extractfile(member)

                        if fdata is None:
                            raise ArchiveError(
                                f"Could not read file contents for {path!r}"
                            )
                        with fdata:
                            output_archive.addfile(output_member, fdata)

                    else:
                        # Directories and links have no regular-file payload.
                        output_archive.addfile(output_member)

           # Check the completed output before reporting success.
            try:
                with tarfile.open(output_path, mode="r:") as check_archive:
                    written = index_archive(check_archive, source="base")

                    if set(written) != set(selected):
                        raise ArchiveError(
                            "Reconstructed archive does not match selected paths"
                        )

            except Exception:
                output_path.unlink(missing_ok=True)
                raise

            return len(selected)
