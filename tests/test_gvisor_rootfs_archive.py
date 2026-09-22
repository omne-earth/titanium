"""Tests for reading and classifying gVisor upper-layer tar entries."""

import tarfile
import io
import pytest

from titanium.environments.base import ArchiveError
from titanium.environments.gvisor.rootfs_archive import (
    classify_upper_member,
    normalize_member_path,
    select_merged_members,
    validate_member_links,
    validate_selected_members,
    merge_rootfs_archives
)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("app/file.txt", "app/file.txt"),
        ("./app/file.txt", "app/file.txt"),
        ("./etc/issue", "etc/issue"),
        ("app/./file.txt", "app/file.txt"),
        ("./", "."),
    ],
)
def test_normalize_member_path(original, expected):
    assert normalize_member_path(original) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/etc/passwd",
        "../outside",
        "app/../../outside",
        "app/\x00/file",
    ],
)
def test_reject_invalid_member_paths(name):
    with pytest.raises(ArchiveError):
        normalize_member_path(name)


def test_new_file_is_a_replacement_action():
    member = tarfile.TarInfo("./app/archive-marker.txt")

    assert classify_upper_member(member) == (
        "app/archive-marker.txt",
        "replace",
    )


def test_gvisor_whiteout_is_a_deletion_action():
    member = tarfile.TarInfo("./etc/issue.net")
    member.type = tarfile.CHRTYPE
    member.devmajor = 0
    member.devminor = 0

    assert classify_upper_member(member) == (
        "etc/issue.net",
        "delete",
    )


def test_other_character_device_is_not_assumed_to_be_a_whiteout():
    member = tarfile.TarInfo("./dev/example")
    member.type = tarfile.CHRTYPE
    member.devmajor = 1
    member.devminor = 3

    with pytest.raises(ArchiveError):
        classify_upper_member(member)


def test_absolute_guest_symlink_target_is_allowed():
    member = tarfile.TarInfo("./app/tool")
    member.type = tarfile.SYMTYPE
    member.linkname = "/usr/bin/tool"

    validate_member_links(member)

    assert classify_upper_member(member) == (
        "app/tool",
        "replace",
    )


def test_hardlink_cannot_name_a_path_outside_the_archive():
    member = tarfile.TarInfo("./app/link")
    member.type = tarfile.LNKTYPE
    member.linkname = "../../outside"

    with pytest.raises(ArchiveError):
        validate_member_links(member)


def test_archive_root_must_be_a_directory():
    member = tarfile.TarInfo(".")

    with pytest.raises(ArchiveError):
        classify_upper_member(member)

    member.type = tarfile.DIRTYPE

    assert classify_upper_member(member) == (".", "root")

def _file(name: str) -> tarfile.TarInfo:
    """Make a regular-file entry for a tiny merge example."""
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.REGTYPE
    return entry


def _directory(name: str) -> tarfile.TarInfo:
    """Make a directory entry."""
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.DIRTYPE
    return entry


def _whiteout(name: str) -> tarfile.TarInfo:
    """Make the deletion marker observed in our real gVisor snapshot."""
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.CHRTYPE
    entry.devmajor = 0
    entry.devminor = 0
    return entry


def test_merge_keeps_unchanged_base_files():
    base_file = _file("bin/sh")

    result = select_merged_members(
        base={"bin/sh": base_file},
        upper={},
    )

    assert result == {
        "bin/sh": ("base", base_file),
    }


def test_merge_replaces_and_adds_files():
    old_config = _file("app/config.txt")
    new_config = _file("./app/config.txt")
    new_result = _file("./app/result.txt")

    result = select_merged_members(
        base={
            "app/config.txt": old_config,
        },
        upper={
            "app/config.txt": new_config,
            "app/result.txt": new_result,
        },
    )

    assert result == {
        "app/config.txt": ("upper", new_config),
        "app/result.txt": ("upper", new_result),
    }


def test_merge_applies_a_file_deletion():
    deleted_file = _file("etc/issue.net")
    deletion_marker = _whiteout("./etc/issue.net")

    result = select_merged_members(
        base={
            "etc/issue.net": deleted_file,
            "etc/issue": _file("etc/issue"),
        },
        upper={
            "etc/issue.net": deletion_marker,
        },
    )

    assert "etc/issue.net" not in result
    assert result["etc/issue"][0] == "base"


def test_merge_deleting_directory_removes_its_children():
    directory = _directory("app/old-dir")
    child = _file("app/old-dir/file.txt")

    result = select_merged_members(
        base={
            "app/old-dir": directory,
            "app/old-dir/file.txt": child,
        },
        upper={
            "app/old-dir": _whiteout("./app/old-dir"),
        },
    )

    assert "app/old-dir" not in result
    assert "app/old-dir/file.txt" not in result


def test_merge_replacing_directory_with_file_removes_old_children():
    directory = _directory("app/old-dir")
    child = _file("app/old-dir/file.txt")
    replacement = _file("./app/old-dir")

    result = select_merged_members(
        base={
            "app/old-dir": directory,
            "app/old-dir/file.txt": child,
        },
        upper={
            "app/old-dir": replacement,
        },
    )

    assert result == {
        "app/old-dir": ("upper", replacement),
    }

def test_selected_filesystem_rejects_file_with_child():
    parent = tarfile.TarInfo("app/config.txt")
    parent.type = tarfile.REGTYPE

    child = tarfile.TarInfo("app/config.txt/child.txt")
    child.type = tarfile.REGTYPE

    selected = {
        "app/config.txt": ("upper", parent),
        "app/config.txt/child.txt": ("upper", child),
    }

    with pytest.raises(
        ArchiveError,
        match="beneath non-directory",
    ):
        validate_selected_members(selected)


def test_selected_filesystem_rejects_symlink_with_child():
    parent = tarfile.TarInfo("app/link")
    parent.type = tarfile.SYMTYPE
    parent.linkname = "/somewhere"

    child = tarfile.TarInfo("app/link/child.txt")
    child.type = tarfile.REGTYPE

    selected = {
        "app/link": ("upper", parent),
        "app/link/child.txt": ("upper", child),
    }

    with pytest.raises(
        ArchiveError,
        match="beneath non-directory",
    ):
        validate_selected_members(selected)


def test_selected_filesystem_accepts_normal_directory():
    directory = tarfile.TarInfo("app/data")
    directory.type = tarfile.DIRTYPE

    child = tarfile.TarInfo("app/data/file.txt")
    child.type = tarfile.REGTYPE

    validate_selected_members({
        "app/data": ("base", directory),
        "app/data/file.txt": ("upper", child),
    })


def test_selected_filesystem_rejects_missing_hardlink_target():
    link = tarfile.TarInfo("app/copy.txt")
    link.type = tarfile.LNKTYPE
    link.linkname = "app/missing.txt"

    with pytest.raises(
        ArchiveError,
        match="missing archive entry",
    ):
        validate_selected_members({
            "app/copy.txt": ("upper", link),
        })


def test_selected_filesystem_rejects_hardlink_cycle():
    first = tarfile.TarInfo("app/first")
    first.type = tarfile.LNKTYPE
    first.linkname = "app/second"

    second = tarfile.TarInfo("app/second")
    second.type = tarfile.LNKTYPE
    second.linkname = "app/first"

    with pytest.raises(
        ArchiveError,
        match="Hardlink cycle",
    ):
        validate_selected_members({
            "app/first": ("upper", first),
            "app/second": ("upper", second),
        })


def test_selected_filesystem_accepts_hardlink_to_regular_file():
    original = tarfile.TarInfo("app/original")
    original.type = tarfile.REGTYPE

    link = tarfile.TarInfo("app/copy")
    link.type = tarfile.LNKTYPE
    link.linkname = "app/original"

    validate_selected_members({
        "app/original": ("base", original),
        "app/copy": ("upper", link),
    })

def _add_regular(archive, name, contents, *, mode=0o644):
    """Add an actual file, including its bytes, to a test tar."""
    data = contents.encode("utf-8")
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    archive.addfile(member, io.BytesIO(data))


def _add_directory(archive, name, *, mode=0o755):
    """Add a directory entry to a test tar."""
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = mode
    archive.addfile(member)


def _add_whiteout(archive, name):
    """Add the deletion-marker format observed in our gVisor probe."""
    member = tarfile.TarInfo(name)
    member.type = tarfile.CHRTYPE
    member.devmajor = 0
    member.devminor = 0
    archive.addfile(member)


def _add_symlink(archive, name, target):
    """Add a symlink without creating or following it on the host."""
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = target
    archive.addfile(member)


def _add_hardlink(archive, name, target):
    """Add a tar hardlink referring to another archive member."""
    member = tarfile.TarInfo(name)
    member.type = tarfile.LNKTYPE
    member.linkname = target
    archive.addfile(member)


def _read_regular(archive, name):
    """Read a regular file's bytes from a tar without extracting to disk."""
    member = archive.getmember(name)
    stream = archive.extractfile(member)
    assert stream is not None, f"Cannot read {name!r}"

    with stream:
        return stream.read()


# ---------------------------------------------------------------------------
# GROUP A — These should PASS with your current writer.
# ---------------------------------------------------------------------------


def test_real_tar_add_modify_delete_and_normalize(tmp_path):
    """The exact add/modify/delete pattern from our successful gVisor probe."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_directory(archive, "app")
        _add_directory(archive, "etc")
        _add_regular(archive, "etc/issue", "ORIGINAL\n")
        _add_regular(archive, "etc/issue.net", "DELETE ME\n")

    with tarfile.open(upper_path, "w:") as archive:
        _add_directory(archive, "./app")
        _add_regular(
            archive,
            "./app/archive-marker.txt",
            "gvisor-upper-probe-content\n",
        )
        _add_regular(archive, "./etc/issue", "MODIFIED\n")
        _add_whiteout(archive, "./etc/issue.net")

    count = merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        names = [member.name for member in archive]

        assert set(names) == {
            "app",
            "etc",
            "etc/issue",
            "app/archive-marker.txt",
        }
        assert len(names) == len(set(names)), "Output contains duplicate paths"

        assert _read_regular(archive, "etc/issue") == b"MODIFIED\n"
        assert _read_regular(
            archive, "app/archive-marker.txt"
        ) == b"gvisor-upper-probe-content\n"

        assert "etc/issue.net" not in names
        assert archive.getmember("app").isdir()

    assert count == 4

    print("\nPASS: New and modified file bytes survived.")
    print("PASS: Deleted file and its whiteout are absent.")
    print("PASS: Output names are normalized and unique.")


def test_real_tar_directory_deletion_preserves_similar_sibling(tmp_path):
    """Deleting old-dir must not accidentally delete old-directory."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_directory(archive, "app/old-dir")
        _add_regular(archive, "app/old-dir/obsolete.txt", "OLD")
        _add_directory(archive, "app/old-directory")
        _add_regular(archive, "app/old-directory/keep.txt", "KEEP")

    with tarfile.open(upper_path, "w:") as archive:
        _add_whiteout(archive, "./app/old-dir")

    merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        names = {member.name for member in archive}

        assert "app/old-dir" not in names
        assert "app/old-dir/obsolete.txt" not in names

        assert "app/old-directory" in names
        assert _read_regular(
            archive, "app/old-directory/keep.txt"
        ) == b"KEEP"

    print("\nPASS: The deleted directory and its children are gone.")
    print("PASS: The similarly named sibling was preserved.")


def test_real_tar_directory_replaced_by_file(tmp_path):
    """A file replacing a directory must not retain the old children."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_directory(archive, "app/old-dir")
        _add_regular(archive, "app/old-dir/old.txt", "OLD")

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "./app/old-dir", "NOW A FILE")

    merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        names = {member.name for member in archive}

        assert names == {"app/old-dir"}
        assert _read_regular(archive, "app/old-dir") == b"NOW A FILE"

    print("\nPASS: The new file replaced the directory.")
    print("PASS: The directory's old children were removed.")


def test_real_tar_directory_metadata_update_keeps_children(tmp_path):
    """Changing directory metadata alone must not remove its old files."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_directory(archive, "app/data", mode=0o755)
        _add_regular(archive, "app/data/keep.txt", "KEEP")

    with tarfile.open(upper_path, "w:") as archive:
        _add_directory(archive, "./app/data", mode=0o700)

    merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        assert archive.getmember("app/data").mode == 0o700
        assert _read_regular(archive, "app/data/keep.txt") == b"KEEP"

    print("\nPASS: The directory received its new permissions.")
    print("PASS: Its existing child remained intact.")


def test_real_tar_preserves_guest_symlink_without_following_it(tmp_path):
    """An absolute guest symlink is metadata, not a host path to open."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/keep.txt", "KEEP")

    with tarfile.open(upper_path, "w:") as archive:
        _add_symlink(archive, "./app/tool", "/usr/bin/tool")

    merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        link = archive.getmember("app/tool")

        assert link.issym()
        assert link.linkname == "/usr/bin/tool"
        assert _read_regular(archive, "app/keep.txt") == b"KEEP"

    print("\nPASS: The guest symlink was preserved as a tar entry.")
    print("PASS: The original unrelated file was preserved.")


def test_real_tar_rejects_unsafe_upper_path(tmp_path):
    """An upper entry must not name a path outside the archived root."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/keep.txt", "KEEP")

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "../outside.txt", "UNSAFE")

    with pytest.raises(ArchiveError):
        merge_rootfs_archives(base_path, upper_path, output_path)

    assert not output_path.exists()

    print("\nPASS: Unsafe member rejected before output creation.")


def test_real_tar_rejects_duplicate_normalized_paths(tmp_path):
    """./app/file and app/file must not silently become competing entries."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/file.txt", "FIRST")
        _add_regular(archive, "./app/file.txt", "SECOND")

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "./app/other.txt", "OTHER")

    with pytest.raises(ArchiveError, match="Duplicate path"):
        merge_rootfs_archives(base_path, upper_path, output_path)

    assert not output_path.exists()

    print("\nPASS: Conflicting normalized paths were rejected.")


# ---------------------------------------------------------------------------
# GROUP B — Known safety questions. These tests are EXPECTED TO FAIL
# with the current writer.
#
# XFAIL = the test exposed a known missing protection.
# XPASS = the test unexpectedly passed; investigate whether the
# implementation already handles that case.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="Current writer overwrites an existing output path",
    strict=False,
)
def test_output_must_not_overwrite_existing_file(tmp_path):
    """A merger should not destroy an artifact that already exists."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/file.txt", "BASE")

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "./app/file.txt", "UPPER")

    output_path.write_bytes(b"EXISTING ARTIFACT")

    try:
        merge_rootfs_archives(base_path, upper_path, output_path)
    except ArchiveError:
        pass

    assert output_path.read_bytes() == b"EXISTING ARTIFACT", (
        "An existing output file was overwritten"
    )


@pytest.mark.xfail(
    reason="Current writer does not prevent output_path from equaling an input",
    strict=False,
)
def test_output_must_not_destroy_base_input(tmp_path):
    """Writing the result over base.tar can destroy the source data."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/file.txt", "BASE")

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "./app/file.txt", "UPPER")

    original_base = base_path.read_bytes()

    try:
        merge_rootfs_archives(
            base_path,
            upper_path,
            base_path,  # Deliberately invalid: output equals input.
        )
    except (ArchiveError, OSError, tarfile.TarError):
        pass

    assert base_path.read_bytes() == original_base, (
        "The original base archive was overwritten"
    )


@pytest.mark.xfail(
    reason="Current writer leaves its output behind when writing fails",
    strict=False,
)
def test_failed_write_must_not_leave_partial_output(tmp_path, monkeypatch):
    """Simulate a write failure after the first output entry."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/first.txt", "FIRST")
        _add_regular(archive, "app/second.txt", "SECOND")

    with tarfile.open(upper_path, "w:") as archive:
        pass

    original_addfile = tarfile.TarFile.addfile
    output_writes = 0

    def fail_during_output(self, member, fileobj=None):
        nonlocal output_writes

        if self.name == str(output_path):
            output_writes += 1

            if output_writes == 2:
                raise OSError("Simulated output write failure")

        return original_addfile(self, member, fileobj)

    monkeypatch.setattr(tarfile.TarFile, "addfile", fail_during_output)

    with pytest.raises(OSError, match="Simulated output write failure"):
        merge_rootfs_archives(base_path, upper_path, output_path)

    assert output_writes == 2, "The simulated failure was not reached"
    assert not output_path.exists(), (
        "An incomplete output file was left behind"
    )


@pytest.mark.xfail(
    reason="Current writer does not independently verify the completed tar",
    strict=False,
)
def test_writer_must_detect_missing_output_entry(tmp_path, monkeypatch):
    """Simulate a writer silently omitting one selected entry."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/first.txt", "FIRST")
        _add_regular(archive, "app/second.txt", "SECOND")

    with tarfile.open(upper_path, "w:") as archive:
        pass

    original_addfile = tarfile.TarFile.addfile

    def silently_skip_second(self, member, fileobj=None):
        if (
            self.name == str(output_path)
            and member.name == "app/second.txt"
        ):
            return None

        return original_addfile(self, member, fileobj)

    monkeypatch.setattr(
        tarfile.TarFile,
        "addfile",
        silently_skip_second,
    )

    # A validated merger should reject the incomplete result.
    with pytest.raises(ArchiveError):
        merge_rootfs_archives(base_path, upper_path, output_path)


@pytest.mark.xfail(
    reason="Current writer does not resolve cross-layer hardlink semantics",
    strict=False,
)
def test_cross_layer_hardlink_requires_explicit_handling(tmp_path):
    """A base hardlink's target may be replaced by the upper layer."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_regular(archive, "app/original.txt", "OLD")
        _add_hardlink(
            archive,
            "app/copy.txt",
            "app/original.txt",
        )

    with tarfile.open(upper_path, "w:") as archive:
        _add_regular(archive, "./app/original.txt", "NEW")

    # We have not established whether the old hardlink should retain
    # OLD bytes or refer to the NEW upper-layer version. Until that
    # semantics question is settled, fail rather than silently guess.
    with pytest.raises(ArchiveError):
        merge_rootfs_archives(base_path, upper_path, output_path)


@pytest.mark.xfail(
    reason="Current writer does not order hardlink targets before hardlinks",
    strict=False,
)
def test_hardlink_target_must_precede_link_in_output(tmp_path):
    """A tar hardlink should not precede the entry holding its file data."""
    base_path = tmp_path / "base.tar"
    upper_path = tmp_path / "upper.tar"
    output_path = tmp_path / "merged.tar"

    with tarfile.open(base_path, "w:") as archive:
        _add_hardlink(archive, "app/copy.txt", "app/original.txt")
        _add_regular(archive, "app/original.txt", "CONTENTS")

    with tarfile.open(upper_path, "w:") as archive:
        pass

    merge_rootfs_archives(base_path, upper_path, output_path)

    with tarfile.open(output_path, "r:") as archive:
        names = [member.name for member in archive]

    assert names.index("app/original.txt") < names.index("app/copy.txt"), (
        "Hardlink was written before the member containing its data"
    )
