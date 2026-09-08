"""ext4 construction: the script it runs, and the image it produces.

The end-to-end tests need the cached builder image, which is built once from
the pinned Alpine base. They skip with a named reason when podman is absent.
Inspecting the produced image (dumpe2fs / debugfs) happens *inside that same
builder container* -- it is how these tests read the converter's own output,
and it is not a path the converter itself has or is allowed to grow.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from titanium.environments.cella import rootfs as cella_rootfs
from titanium.environments.cella.boot_layer import (
    BootLayer,
    GuestFile,
    GuestSymlink,
)
from titanium.environments.cella.podman import (
    PodmanError,
    build_image,
    export_rootfs_tar,
    image_exists,
    new_build_tag,
    podman_bin,
    untag_image,
)
from titanium.environments.cella.rootfs import (
    ROOTFS_BUILDER_BASE_IMAGE,
    ROOTFS_BUILDER_CONTAINERFILE,
    ROOTFS_BUILDER_IMAGE,
    RootfsBuildError,
    build_ext4,
    ensure_rootfs_builder_image,
    sha3_256_file,
)

requires_podman = pytest.mark.skipif(
    shutil.which(podman_bin()) is None,
    reason=f"{podman_bin()} is not on PATH",
)

CAPACITY = 32 * 1024 * 1024

# Several entries, several destinations, and every field varied: two modes,
# a non-zero numeric owner, a setuid bit, and a relative symlink. None of
# these paths is special to this module -- they are just what the layer says.
LAYER = BootLayer(
    entries=(
        GuestFile(
            path="/etc/titanium/first.conf",
            contents=b"first\n",
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestFile(
            path="/etc/titanium/second.conf",
            contents=b"second payload\n",
            mode=0o600,
            uid=1234,
            gid=5678,
        ),
        GuestFile(
            path="/usr/local/bin/probe",
            contents=b"#!/bin/sh\nexit 0\n",
            mode=0o4755,
            uid=0,
            gid=0,
        ),
        GuestSymlink(
            path="/etc/titanium/current.conf",
            target="second.conf",
            uid=1234,
            gid=5678,
        ),
    )
)


# ------------------------------------------------------------------- digesting


def test_sha3_256_matches_an_independent_computation(tmp_path):
    payload = b"x" * (3 * 1024 * 1024 + 7)  # crosses the streaming chunk size
    artifact = tmp_path / "rootfs.ext4"
    artifact.write_bytes(payload)
    assert sha3_256_file(artifact) == hashlib.sha3_256(payload).hexdigest()


# ------------------------------------------------------------ builder identity


def test_the_builder_base_is_pinned_never_floating():
    assert ROOTFS_BUILDER_BASE_IMAGE == "docker.io/library/alpine:3.22"
    assert not ROOTFS_BUILDER_BASE_IMAGE.endswith(":latest")
    assert ROOTFS_BUILDER_BASE_IMAGE in ROOTFS_BUILDER_CONTAINERFILE
    # GNU tar, explicitly: busybox tar has no --numeric-owner, and losing
    # numeric ownership silently is the failure this pipeline exists to avoid.
    assert "e2fsprogs" in ROOTFS_BUILDER_CONTAINERFILE
    assert "tar" in ROOTFS_BUILDER_CONTAINERFILE


def test_the_builder_recipe_installs_no_filesystem_inspection_tools():
    """Keep the builder to what it needs, and no more.

    This is a recipe assertion, not a capability claim. It does not prove the
    builder "cannot read a filesystem" -- Alpine's busybox alone can do plenty,
    and mkfs.ext4 itself reads. What it does is stop e2fsprogs-extra
    (debugfs, dumpe2fs) drifting into the image the converter runs, which is a
    smaller and honest thing.

    The invariant that actually matters is enforced on the *product code*, in
    tests/test_cella_no_host_escape.py: the converter must never read a Cella
    disk host-side, whatever tools happen to exist.
    """
    assert "e2fsprogs-extra" not in ROOTFS_BUILDER_CONTAINERFILE


def test_a_present_builder_is_never_rebuilt(monkeypatch):
    monkeypatch.setattr(cella_rootfs, "image_exists", lambda _ref: True)

    def refuse(*_args, **_kwargs):
        raise AssertionError("apk must not run once the builder is cached")

    monkeypatch.setattr(cella_rootfs, "run_podman", refuse)
    assert ensure_rootfs_builder_image() == ROOTFS_BUILDER_IMAGE


def test_a_builder_that_cannot_be_built_has_no_fallback(monkeypatch):
    monkeypatch.setattr(cella_rootfs, "image_exists", lambda _ref: False)

    def fail(*_args, **_kwargs):
        raise PodmanError("no network")

    monkeypatch.setattr(cella_rootfs, "run_podman", fail)
    with pytest.raises(PodmanError, match="no network"):
        ensure_rootfs_builder_image()


# ------------------------------------------------------------ script contents


def _script(boot_layer=LAYER, size_bytes=CAPACITY) -> str:
    return cella_rootfs._mkfs_script(size_bytes=size_bytes, boot_layer=boot_layer)


def test_the_script_honors_the_requested_capacity_exactly():
    script = _script()
    assert f"truncate -s {CAPACITY} /out/rootfs.ext4" in script
    # No heuristic rounding, padding, or growth: the number the caller gave.
    assert str(CAPACITY) in script


def test_the_script_preserves_numeric_ownership():
    script = _script()
    assert "--numeric-owner" in script
    assert "tar -xpf" in script


def test_the_script_places_every_declared_entry_and_nothing_else():
    script = _script()
    assert (
        "install -m 0644 -o 0 -g 0 /in/entry-0000 /work/root/etc/titanium/first.conf"
        in script
    )
    assert (
        "install -m 0600 -o 1234 -g 5678 /in/entry-0001 "
        "/work/root/etc/titanium/second.conf" in script
    )
    assert (
        "install -m 4755 -o 0 -g 0 /in/entry-0002 /work/root/usr/local/bin/probe"
        in script
    )
    assert "ln -s -- second.conf /work/root/etc/titanium/current.conf" in script
    assert "chown -h 1234:5678 /work/root/etc/titanium/current.conf" in script
    assert script.count("install -m ") == 3
    assert script.count("ln -s ") == 1


def test_the_script_places_nothing_when_no_boot_layer_is_supplied():
    script = _script(boot_layer=None)
    assert "install -m" not in script
    assert "ln -s" not in script
    assert "place_parents" not in script
    assert "mkfs.ext4" in script


def test_an_empty_boot_layer_places_nothing():
    script = _script(boot_layer=BootLayer(entries=()))
    assert "place_parents" not in script
    assert "install -m" not in script


def test_the_script_hard_codes_no_init_path():
    """/sbin/init is not special here; it is a path a layer may declare."""
    script = _script()
    assert "/sbin/init" not in script
    assert "/in/init" not in script


def test_the_script_hard_codes_no_init_path_even_when_a_layer_declares_one():
    """And when one *is* declared it is placed like anything else."""
    layer = BootLayer(
        entries=(
            GuestFile(
                path="/sbin/init", contents=b"#!/bin/sh\n", mode=0o755, uid=0, gid=0
            ),
        )
    )
    script = _script(boot_layer=layer)
    assert "install -m 0755 -o 0 -g 0 /in/entry-0000 /work/root/sbin/init" in script


def test_an_entry_is_staged_under_a_generated_name_not_its_guest_path():
    """A guest path is the layer's data; it must not name a host-side source."""
    layer = BootLayer(
        entries=(
            GuestFile(path="/etc/rootfs.tar", contents=b"x", mode=0o644, uid=0, gid=0),
        )
    )
    script = _script(boot_layer=layer)
    assert "/in/entry-0000 /work/root/etc/rootfs.tar" in script
    assert "/in/etc/rootfs.tar" not in script


def test_parents_are_walked_a_component_at_a_time_never_with_mkdir_p():
    """`mkdir -p` succeeds through a symlinked component. The walk refuses one."""
    script = _script()
    # The only `mkdir -p` is the build root itself, before anything untrusted
    # has been extracted.
    assert script.count("mkdir -p") == 1
    assert "mkdir -p /work/root\n" in script
    assert '[ -L "$dir" ]' in script
    assert "refusing to place through it" in script


def test_an_existing_leaf_is_cleared_without_being_followed():
    script = _script()
    assert '[ -L "$root$1" ] || [ -f "$root$1" ]' in script
    assert 'rm -f "$root$1"' in script


def test_guest_paths_and_symlink_targets_are_shell_quoted():
    layer = BootLayer(
        entries=(
            GuestFile(path="/etc/$(id).d", contents=b"x", mode=0o600, uid=0, gid=0),
            GuestSymlink(path="/etc/a b", target="$(id)", uid=0, gid=0),
        )
    )
    script = _script(boot_layer=layer)
    assert "'/work/root/etc/$(id).d'" in script
    assert "'$(id)'" in script
    assert "'/work/root/etc/a b'" in script
    # Never the bare, runnable spelling.
    assert "/in/entry-0000 /work/root/etc/$(id).d\n" not in script


def test_the_script_needs_no_loop_device_root_or_attached_filesystem():
    script = _script()
    for forbidden in ("losetup", "/dev/loop", "sudo", "mknod"):
        assert forbidden not in script
    # `mkfs.ext4 -d` populates from a directory tree; that is what removes the
    # need for elevated privilege here.
    assert "mkfs.ext4 -F -q -d /work/root" in script


def test_the_script_aborts_on_the_first_failing_step():
    script = _script(size_bytes=1)
    # -f as well as -eu: the parent walk splits on IFS, and an unguarded
    # expansion there would glob against the tree being built.
    assert script.startswith("set -euf")


# ------------------------------------------------------------ input refusals


def test_a_non_positive_capacity_is_refused(tmp_path):
    (tmp_path / "rootfs.tar").write_bytes(b"")
    for size in (0, -1):
        with pytest.raises(RootfsBuildError, match="positive"):
            build_ext4(
                rootfs_tar=tmp_path / "rootfs.tar",
                boot_layer=LAYER,
                size_bytes=size,
                dest=tmp_path / "out" / "rootfs.ext4",
            )


def test_a_missing_export_is_refused(tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(RootfsBuildError, match="No exported"):
        build_ext4(
            rootfs_tar=tmp_path / "absent.tar",
            boot_layer=LAYER,
            size_bytes=CAPACITY,
            dest=tmp_path / "out" / "rootfs.ext4",
        )


def test_an_existing_destination_is_never_overwritten(tmp_path):
    (tmp_path / "rootfs.tar").write_bytes(b"")
    (tmp_path / "out").mkdir()
    dest = tmp_path / "out" / "rootfs.ext4"
    dest.write_bytes(b"someone else's filesystem")
    with pytest.raises(RootfsBuildError, match="already exists"):
        build_ext4(
            rootfs_tar=tmp_path / "rootfs.tar",
            boot_layer=LAYER,
            size_bytes=CAPACITY,
            dest=dest,
        )
    assert dest.read_bytes() == b"someone else's filesystem"


# ------------------------------------------------------------------ end to end


@pytest.fixture
def exported_rootfs(tmp_path):
    """A real `podman export` tar from a FROM-scratch image. No registry."""
    context = tmp_path / "ctx"
    (context / "app").mkdir(parents=True)
    (context / "app" / "data.txt").write_text("payload")
    script = context / "app" / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY --chown=1234:5678 app /app\n"
    )
    tag = new_build_tag("titanium-cella-test")
    build_image(
        context_dir=context,
        build_file=context / "Dockerfile",
        tag=tag,
        pull="never",
        timeout_sec=300,
    )
    tar_path = tmp_path / "rootfs.tar"
    try:
        export_rootfs_tar(image=tag, dest_tar=tar_path, timeout_sec=300)
        yield tar_path
    finally:
        untag_image(tag)


# Test-only. The production builder carries mkfs.ext4 and tar and nothing that
# can read a filesystem image back -- see the inventory test below. Reading the
# converter's output is the test's job, so the tools for it live in an image
# the converter never names.
INSPECTOR_IMAGE = "localhost/titanium-cella-test-inspector:1"
INSPECTOR_CONTAINERFILE = (
    f"FROM {ROOTFS_BUILDER_BASE_IMAGE}\n"
    "RUN apk add --no-cache e2fsprogs e2fsprogs-extra\n"
)


def _ensure_inspector(tmp_path: Path) -> str:
    if image_exists(INSPECTOR_IMAGE):
        return INSPECTOR_IMAGE
    staging = tmp_path / "inspector"
    staging.mkdir(exist_ok=True)
    (staging / "Dockerfile").write_text(INSPECTOR_CONTAINERFILE)
    subprocess.run(
        [
            podman_bin(),
            "build",
            "-f",
            str(staging / "Dockerfile"),
            "-t",
            INSPECTOR_IMAGE,
            str(staging),
        ],
        check=True,
        capture_output=True,
    )
    return INSPECTOR_IMAGE


def _read_image(image_path: Path, command: str, tmp_path: Path) -> str:
    """Read the produced ext4 from inside the test-only inspector container."""
    completed = subprocess.run(
        [
            podman_bin(),
            "run",
            "--rm",
            "--network=none",
            "-v",
            f"{image_path.parent}:/img:ro,z",
            _ensure_inspector(tmp_path),
            "sh",
            "-c",
            command,
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@pytest.fixture
def built_ext4(exported_rootfs, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "rootfs.ext4"
    build_ext4(
        rootfs_tar=exported_rootfs,
        boot_layer=LAYER,
        size_bytes=CAPACITY,
        dest=dest,
        timeout_sec=600,
    )
    return dest


@requires_podman
def test_the_builder_image_is_built_from_the_declared_base(exported_rootfs):
    ensure_rootfs_builder_image(timeout_sec=600)
    assert image_exists(ROOTFS_BUILDER_IMAGE)
    history = subprocess.run(
        [podman_bin(), "image", "history", "--format", "json", ROOTFS_BUILDER_IMAGE],
        capture_output=True,
        text=True,
    ).stdout
    assert "e2fsprogs" in history or "apk add" in history


@requires_podman
def test_the_image_carries_the_declared_capacity(built_ext4, tmp_path):
    assert built_ext4.stat().st_size == CAPACITY
    header = _read_image(
        built_ext4, "dumpe2fs -h /img/rootfs.ext4 2>/dev/null", tmp_path
    )
    block_count = int(
        next(line for line in header.splitlines() if line.startswith("Block count:"))
        .split(":")[1]
        .strip()
    )
    block_size = int(
        next(line for line in header.splitlines() if line.startswith("Block size:"))
        .split(":")[1]
        .strip()
    )
    assert block_count * block_size == CAPACITY


@requires_podman
def test_the_backing_file_is_sparse(built_ext4):
    allocated = built_ext4.stat().st_blocks * 512
    assert allocated < built_ext4.stat().st_size, (
        "mkfs wrote the whole capacity; the backing file is not sparse"
    )


def _stat(image, path, tmp_path) -> str:
    return _read_image(
        image, f"debugfs -R 'stat {path}' /img/rootfs.ext4 2>/dev/null", tmp_path
    )


@requires_podman
def test_every_declared_boot_file_is_placed_with_exact_bytes(built_ext4, tmp_path):
    """More than one file, and each one byte for byte."""
    for path, expected in [
        ("/etc/titanium/first.conf", b"first\n"),
        ("/etc/titanium/second.conf", b"second payload\n"),
        ("/usr/local/bin/probe", b"#!/bin/sh\nexit 0\n"),
    ]:
        dumped = _read_image(
            built_ext4,
            f"debugfs -R 'cat {path}' /img/rootfs.ext4 2>/dev/null",
            tmp_path,
        )
        assert dumped.encode() == expected, path


@requires_podman
def test_each_boot_files_declared_mode_is_what_lands(built_ext4, tmp_path):
    """Including the setuid bit, which a chmod-after-chown order would drop."""
    for path, mode in [
        ("/etc/titanium/first.conf", "0644"),
        ("/etc/titanium/second.conf", "0600"),
        ("/usr/local/bin/probe", "04755"),
    ]:
        assert f"Mode:  {mode}" in _stat(built_ext4, path, tmp_path), path


@requires_podman
def test_numeric_boot_file_ownership_is_preserved(built_ext4, tmp_path):
    """Numeric, because the guest's NSS database is the image's, not ours."""
    owned = _stat(built_ext4, "/etc/titanium/second.conf", tmp_path)
    assert "User:  1234" in owned
    assert "Group:  5678" in owned
    root_owned = _stat(built_ext4, "/etc/titanium/first.conf", tmp_path)
    assert "User:     0" in root_owned
    assert "Group:     0" in root_owned


@requires_podman
def test_a_boot_symlink_becomes_a_real_symlink(built_ext4, tmp_path):
    """A symlink entry is a symlink, not a text file holding its target."""
    link = _stat(built_ext4, "/etc/titanium/current.conf", tmp_path)
    assert "Type: symlink" in link
    assert "Type: regular" not in link


@requires_podman
def test_a_relative_symlink_target_survives_verbatim(built_ext4, tmp_path):
    """Not absolutized, not resolved: the string the layer declared."""
    link = _stat(built_ext4, "/etc/titanium/current.conf", tmp_path)
    assert 'Fast link dest: "second.conf"' in link
    assert "/etc/titanium/second.conf" not in link


@requires_podman
def test_the_symlinks_ownership_lands_on_the_link_itself(built_ext4, tmp_path):
    """A plain chown here would follow the link and retitle its target."""
    link = _stat(built_ext4, "/etc/titanium/current.conf", tmp_path)
    assert "User:  1234" in link
    assert "Group:  5678" in link
    # The target keeps its own declared ownership, unchanged by the link.
    target = _stat(built_ext4, "/etc/titanium/second.conf", tmp_path)
    assert "User:  1234" in target


@requires_podman
def test_parents_the_export_never_carried_are_created(built_ext4, tmp_path):
    """The FROM-scratch export has no /etc and no /usr/local/bin."""
    listing = _read_image(
        built_ext4,
        "debugfs -R 'ls -l /etc/titanium' /img/rootfs.ext4 2>/dev/null",
        tmp_path,
    )
    for name in ("first.conf", "second.conf", "current.conf"):
        assert name in listing, name


@requires_podman
def test_no_init_is_placed_when_the_boot_layer_declares_none(built_ext4, tmp_path):
    """The old pipeline wrote /sbin/init on every build. Nothing does now."""
    listing = _read_image(
        built_ext4, "debugfs -R 'ls -l /' /img/rootfs.ext4 2>/dev/null", tmp_path
    )
    assert "sbin" not in listing
    assert "init" not in listing


# ------------------------------------------------------- placement cannot escape


@pytest.fixture
def hijacking_rootfs(tmp_path):
    """An export whose /hijack is a symlink, as a hostile image could ship."""
    context = tmp_path / "hijackctx"
    context.mkdir()
    (context / "Dockerfile").write_text(
        f"FROM {ROOTFS_BUILDER_BASE_IMAGE}\n"
        "RUN mkdir -p /realdir && ln -s /realdir /hijack\n"
    )
    tag = new_build_tag("titanium-cella-test")
    build_image(
        context_dir=context,
        build_file=context / "Dockerfile",
        tag=tag,
        pull="never",
        timeout_sec=300,
    )
    tar_path = tmp_path / "hijack.tar"
    try:
        export_rootfs_tar(image=tag, dest_tar=tar_path, timeout_sec=300)
        yield tar_path
    finally:
        untag_image(tag)


@requires_podman
def test_a_symlinked_parent_cannot_redirect_a_boot_entry(hijacking_rootfs, tmp_path):
    """The build fails rather than writing through a link the image chose.

    `mkdir -p` would have succeeded here and placed the entry in /realdir --
    or, for an absolute link, outside the tree being built entirely.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    layer = BootLayer(
        entries=(
            GuestFile(path="/hijack/planted", contents=b"x", mode=0o644, uid=0, gid=0),
        )
    )
    with pytest.raises(PodmanError) as caught:
        build_ext4(
            rootfs_tar=hijacking_rootfs,
            boot_layer=layer,
            size_bytes=CAPACITY,
            dest=out_dir / "rootfs.ext4",
            timeout_sec=600,
        )
    assert "is a symlink" in str(caught.value)
    assert not (out_dir / "rootfs.ext4").exists()


@requires_podman
def test_an_unrelated_destination_in_that_same_image_still_places(
    hijacking_rootfs, tmp_path
):
    """The refusal is about the blocked path, not a blanket failure."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "rootfs.ext4"
    layer = BootLayer(
        entries=(
            GuestFile(
                path="/realdir/placed", contents=b"ok\n", mode=0o644, uid=0, gid=0
            ),
        )
    )
    build_ext4(
        rootfs_tar=hijacking_rootfs,
        boot_layer=layer,
        size_bytes=CAPACITY,
        dest=dest,
        timeout_sec=600,
    )
    dumped = _read_image(
        dest, "debugfs -R 'cat /realdir/placed' /img/rootfs.ext4 2>/dev/null", tmp_path
    )
    assert dumped == "ok\n"


@requires_podman
def test_the_images_numeric_ownership_survives_into_the_filesystem(
    built_ext4, tmp_path
):
    stat_out = _read_image(
        built_ext4,
        "debugfs -R 'stat /app/data.txt' /img/rootfs.ext4 2>/dev/null",
        tmp_path,
    )
    assert "User:  1234" in stat_out
    assert "Group:  5678" in stat_out


@requires_podman
def test_executable_modes_survive_into_the_filesystem(built_ext4, tmp_path):
    stat_out = _read_image(
        built_ext4,
        "debugfs -R 'stat /app/run.sh' /img/rootfs.ext4 2>/dev/null",
        tmp_path,
    )
    assert "Mode:  0755" in stat_out


# ------------------------------------------------- filesystem metadata, observed

# These record what the pipeline does today. They are measurements, not
# endorsements: whether file capabilities *should* survive is an open question,
# and nothing here adds --xattrs to change the answer.


@pytest.fixture
def suid_rootfs(tmp_path):
    """A FROM-scratch image carrying setuid, setgid, and sticky bits."""
    context = tmp_path / "suidctx"
    context.mkdir()
    (context / "seed").write_text("x")
    (context / "Dockerfile").write_text(
        "FROM scratch\n"
        "COPY --chmod=4755 seed /suid\n"
        "COPY --chmod=2755 seed /sgid\n"
        "COPY --chmod=1777 seed /sticky\n"
    )
    tag = new_build_tag("titanium-cella-test")
    build_image(
        context_dir=context,
        build_file=context / "Dockerfile",
        tag=tag,
        pull="never",
        timeout_sec=300,
    )
    tar_path = tmp_path / "suid.tar"
    try:
        export_rootfs_tar(image=tag, dest_tar=tar_path, timeout_sec=300)
        yield tar_path
    finally:
        untag_image(tag)


@requires_podman
def test_setuid_setgid_and_sticky_bits_survive_the_pipeline(suid_rootfs, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "rootfs.ext4"
    build_ext4(
        rootfs_tar=suid_rootfs,
        boot_layer=LAYER,
        size_bytes=CAPACITY,
        dest=dest,
        timeout_sec=600,
    )
    for path, mode in [("/suid", "04755"), ("/sgid", "02755"), ("/sticky", "01777")]:
        stat_out = _read_image(
            dest, f"debugfs -R 'stat {path}' /img/rootfs.ext4 2>/dev/null", tmp_path
        )
        assert f"Mode:  {mode}" in stat_out, f"{path} lost its mode bits"


@requires_podman
def test_file_capabilities_do_not_survive_the_pipeline(tmp_path):
    """Observed, and currently unfixed: security.capability is dropped.

    `podman export` *does* record it, as a SCHILY.xattr pax header. It is lost
    at extraction, because the builder runs plain `tar -xpf --numeric-owner`
    and GNU tar does not restore xattrs without --xattrs. A control extraction
    with `--xattrs --xattrs-include=*` puts the attribute into the ext4, so the
    cause is the flag and nothing subtler.

    Whether to add that flag is a real decision -- it changes what a guest can
    do -- and it is not this pass's to make. This test pins the current answer
    so a change to it is deliberate and visible.
    """
    context = tmp_path / "capctx"
    context.mkdir()
    (context / "Dockerfile").write_text(
        f"FROM {ROOTFS_BUILDER_BASE_IMAGE}\n"
        "RUN apk add --no-cache libcap "
        "&& printf '#!/bin/sh\\nexit 0\\n' > /probecap "
        "&& chmod 0755 /probecap "
        "&& setcap cap_net_raw+ep /probecap\n"
    )
    tag = new_build_tag("titanium-cella-test")
    try:
        build_image(
            context_dir=context,
            build_file=context / "Dockerfile",
            tag=tag,
            timeout_sec=600,
        )
    except PodmanError as exc:
        pytest.skip(f"could not build the file-capability fixture: {exc}")

    tar_path = tmp_path / "cap.tar"
    try:
        export_rootfs_tar(image=tag, dest_tar=tar_path, timeout_sec=300)
    finally:
        untag_image(tag)

    # The export records it.
    import tarfile

    with tarfile.open(tar_path) as archive:
        recorded = {
            key
            for member in archive
            if member.name.lstrip("./") == "probecap"
            for key in member.pax_headers
        }
    assert any("security.capability" in key for key in recorded), (
        "podman export no longer records file capabilities; this test's "
        "premise has changed"
    )

    # The extraction drops it.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dest = out_dir / "rootfs.ext4"
    build_ext4(
        rootfs_tar=tar_path,
        boot_layer=LAYER,
        size_bytes=CAPACITY,
        dest=dest,
        timeout_sec=600,
    )
    listing = _read_image(
        dest, "debugfs -R 'ea_list /probecap' /img/rootfs.ext4 2>/dev/null", tmp_path
    )
    assert "security.capability" not in listing
