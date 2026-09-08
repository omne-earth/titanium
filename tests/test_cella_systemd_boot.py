"""Deciding whether an exported filesystem boots systemd, and making it.

Everything here is offline. The probe tests build synthetic tars in memory and
read them with Python; the provisioning tests fake the derived build. Nothing
runs a package manager, reaches a registry, or executes anything from an image
under test -- which is the property the module exists to have, and one test
asserts it directly.
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import pytest

from titanium.environments.cella import systemd_boot as cella_systemd
from titanium.environments.cella.boot_layer import (
    BootLayer,
    GuestFile,
    GuestSymlink,
    validate_boot_layer,
)
from titanium.environments.cella.converter import (
    BuildFacts,
    FlavorIdentity,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.rootfs import sha3_256_file
from titanium.environments.cella.systemd_boot import (
    STRATEGY_ALREADY_SYSTEMD,
    BuildRun,
    GuestOsInfo,
    PreparedSystemdRootfs,
    RootfsArchive,
    SystemdBootError,
    SystemdProvisionPlan,
    plan_systemd_provisioning,
    prepare_systemd_rootfs,
    probe_rootfs_tar,
    render_derived_build_file,
    validate_provision_plan,
)

SYSTEMD_BYTES = b"\x7fELF pretend systemd\n"

DEBIAN_OS_RELEASE = b"""\
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
ID=debian
HOME_URL="https://www.debian.org/"
"""

UBUNTU_OS_RELEASE = b"""\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
ID=ubuntu
ID_LIKE=debian
VERSION_ID="24.04"
"""


# --------------------------------------------------------------- tar building


def write_tar(tmp_path: Path, spec, name: str = "rootfs.tar") -> Path:
    """A synthetic export. *spec* is (kind, guest path, payload-or-target)."""
    dest = tmp_path / name
    with tarfile.open(dest, "w") as archive:
        for kind, path, extra in spec:
            info = tarfile.TarInfo(path.lstrip("/"))
            info.mode = 0o755
            if kind == "file":
                payload = extra if isinstance(extra, bytes) else extra.encode()
                info.type = tarfile.REGTYPE
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = extra
                archive.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = extra.lstrip("/")
                archive.addfile(info)
            else:  # pragma: no cover - a typo in a test spec
                raise AssertionError(f"unknown spec kind {kind!r}")
    return dest


def bootable_spec(os_release: bytes = DEBIAN_OS_RELEASE):
    """The ordinary shape: usr-merged, /sbin/init -> systemd."""
    return [
        ("file", "/etc/os-release", os_release),
        ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
        ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
    ]


# ------------------------------------------------------------------ os-release


def test_os_release_is_read_from_a_regular_file(tmp_path):
    info = probe_rootfs_tar(write_tar(tmp_path, bootable_spec()))
    assert info.os_id == "debian"
    assert info.version_id == "12"
    assert info.pretty_name == "Debian GNU/Linux 12 (bookworm)"
    assert info.id_like == ()


def test_os_release_is_followed_through_a_symlink(tmp_path):
    """/etc/os-release -> /usr/lib/os-release is the normal modern layout."""
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/os-release", UBUNTU_OS_RELEASE),
            ("symlink", "/etc/os-release", "../usr/lib/os-release"),
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
        ],
    )
    info = probe_rootfs_tar(tar)
    assert info.os_id == "ubuntu"
    assert info.id_like == ("debian",)
    assert info.version_id == "24.04"


def test_usr_lib_os_release_is_the_fallback_when_etc_has_none(tmp_path):
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/os-release", UBUNTU_OS_RELEASE),
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
        ],
    )
    assert probe_rootfs_tar(tar).os_id == "ubuntu"


def test_id_like_keeps_every_entry_in_declared_order(tmp_path):
    tar = write_tar(
        tmp_path,
        [("file", "/etc/os-release", b'ID=almalinux\nID_LIKE="rhel centos fedora"\n')],
    )
    assert probe_rootfs_tar(tar).id_like == ("rhel", "centos", "fedora")


def test_an_absent_os_release_is_a_fact_not_a_failure(tmp_path):
    """FROM scratch is legitimate. Whether it is supportable is policy."""
    info = probe_rootfs_tar(
        write_tar(tmp_path, [("file", "/app/run.sh", b"#!/bin/sh\n")])
    )
    assert info.os_id is None
    assert info.id_like == ()
    assert info.systemd_bootable is False


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b'ID="debian\n', "never closes"),
        (b"ID='debian\n", "never closes"),
        (b'PRETTY_NAME="Debian \\q 12"\n', "does not define"),
        # Shell would read the escaped quote and find the string unterminated;
        # this parser finds the trailing backslash first. Different reason,
        # same refusal, and neither one guesses.
        (b'PRETTY_NAME="ends in a backslash\\"\n', "trailing backslash"),
        (b"ID=deb ian\n", "not a plain value"),
        (b"ID=$(rm -rf /)\n", "not a plain value"),
        (b"ID=`whoami`\n", "not a plain value"),
    ],
)
def test_malformed_os_release_fails_clearly_rather_than_guessing(
    tmp_path, line, message
):
    tar = write_tar(tmp_path, [("file", "/etc/os-release", line)])
    with pytest.raises(SystemdBootError, match=message):
        probe_rootfs_tar(tar)


def test_os_release_is_never_evaluated_as_shell(tmp_path):
    """It is shell-*compatible* syntax, which is not the same as safe to source."""
    tar = write_tar(tmp_path, [("file", "/etc/os-release", b'ID="$(id -u)"\n')])
    # Read literally, not expanded -- and certainly not executed.
    assert probe_rootfs_tar(tar).os_id == "$(id -u)"


def test_a_malformed_line_for_an_unreported_key_is_ignored(tmp_path):
    """This parser has no opinion about fields it does not report."""
    tar = write_tar(
        tmp_path, [("file", "/etc/os-release", b"ID=debian\nSUPPORT_END=$(oops\n")]
    )
    assert probe_rootfs_tar(tar).os_id == "debian"


def test_comments_and_blank_lines_are_skipped(tmp_path):
    tar = write_tar(
        tmp_path, [("file", "/etc/os-release", b"# a comment\n\n  \nID=debian\n")]
    )
    assert probe_rootfs_tar(tar).os_id == "debian"


# ----------------------------------------------------------- link resolution


def test_a_direct_symlink_to_systemd_is_bootable(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/sbin/init", "/lib/systemd/systemd"),
            ],
        )
    )
    assert info.systemd_bootable is True
    assert info.init_resolved_path == "/lib/systemd/systemd"
    assert info.systemd_path == "/lib/systemd/systemd"


def test_a_usr_merged_chain_through_a_symlinked_parent_resolves(tmp_path):
    """/sbin -> usr/sbin, then /usr/sbin/init -> ../lib/systemd/systemd."""
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/usr/sbin/init", "../lib/systemd/systemd"),
                ("symlink", "/sbin", "usr/sbin"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable is True


def test_a_relative_target_resolves_against_the_links_own_parent(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/sbin/init", "../usr/lib/systemd/systemd"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"


def test_an_absolute_target_is_absolute_against_the_guest_root(tmp_path):
    """Not the host's root. Nothing here touches a host path."""
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"


def test_a_multi_hop_chain_resolves(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/usr/lib/systemd/systemd-real", "systemd"),
                ("symlink", "/sbin/init", "/usr/bin/init"),
                ("symlink", "/usr/bin/init", "../lib/systemd/systemd-real"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable is True


def test_a_hard_linked_init_resolves_to_the_file_it_duplicates(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("hardlink", "/sbin/init", "/usr/lib/systemd/systemd"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable is True


def test_a_symlink_loop_is_refused(tmp_path):
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "/sbin/init", "/sbin/other"),
            ("symlink", "/sbin/other", "/sbin/init"),
        ],
    )
    with pytest.raises(SystemdBootError, match="link loop"):
        probe_rootfs_tar(tar)


def test_a_self_referencing_symlink_is_refused(tmp_path):
    tar = write_tar(tmp_path, [("symlink", "/sbin/init", "/sbin/init")])
    with pytest.raises(SystemdBootError, match="link loop"):
        probe_rootfs_tar(tar)


def test_a_link_resolving_above_the_guest_root_is_refused(tmp_path):
    """`..` past / would be a host path. It is refused, never clamped."""
    tar = write_tar(tmp_path, [("symlink", "/sbin/init", "../../../../etc/shadow")])
    with pytest.raises(SystemdBootError, match="leaves the guest root"):
        probe_rootfs_tar(tar)


def test_a_member_name_that_escapes_the_guest_root_is_refused(tmp_path):
    tar = write_tar(tmp_path, [("file", "../outside", b"x")])
    with pytest.raises(SystemdBootError, match="escapes the guest root"):
        probe_rootfs_tar(tar)


def test_a_dangling_symlink_is_absent_not_an_error(tmp_path):
    info = probe_rootfs_tar(
        write_tar(tmp_path, [("symlink", "/sbin/init", "/usr/lib/systemd/systemd")])
    )
    assert info.init_present is False
    assert info.init_resolved_path is None
    assert info.systemd_bootable is False


def test_one_path_declared_twice_as_different_objects_is_refused(tmp_path):
    """Extraction would pick by ordering; a probe must not pick the other."""
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("file", "/sbin/init", b"a real file"),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
        ],
    )
    with pytest.raises(SystemdBootError, match="more than once"):
        probe_rootfs_tar(tar)


def test_an_identical_duplicate_is_not_ambiguous(tmp_path):
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
        ],
    )
    assert probe_rootfs_tar(tar).systemd_bootable is True


def test_leading_dot_slash_member_names_are_normalized(tmp_path):
    tar = write_tar(
        tmp_path,
        [
            ("file", "./usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "./sbin/init", "/usr/lib/systemd/systemd"),
        ],
    )
    assert probe_rootfs_tar(tar).systemd_bootable is True


# -------------------------------------------------- what "bootable" means


def test_systemd_present_but_no_init_is_not_bootable(tmp_path):
    """The kernel needs a path, not a package."""
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/etc/os-release", DEBIAN_OS_RELEASE),
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ],
        )
    )
    assert info.systemd_path == "/usr/lib/systemd/systemd"
    assert info.init_present is False
    assert info.systemd_bootable is False


def test_an_init_that_is_some_other_init_is_recorded_not_overwritten(tmp_path):
    """systemd being installed nearby does not make sysvinit into systemd."""
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("file", "/usr/sbin/sysvinit", b"not systemd"),
                ("symlink", "/sbin/init", "/usr/sbin/sysvinit"),
            ],
        )
    )
    assert info.init_present is True
    assert info.init_resolved_path == "/usr/sbin/sysvinit"
    assert info.systemd_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable is False


def test_an_init_that_is_a_real_file_but_not_systemd_is_not_bootable(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("file", "/sbin/init", b"#!/bin/sh\nexec /bin/busybox init\n"),
            ],
        )
    )
    assert info.init_resolved_path == "/sbin/init"
    assert info.systemd_bootable is False


def test_a_systemd_path_that_is_a_directory_does_not_count(tmp_path):
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("dir", "/usr/lib/systemd/systemd", None),
                ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
            ],
        )
    )
    assert info.systemd_path is None
    assert info.systemd_bootable is False


def test_both_candidate_paths_resolving_to_one_file_still_matches(tmp_path):
    """A usr-merged guest where /lib -> usr/lib."""
    info = probe_rootfs_tar(
        write_tar(
            tmp_path,
            [
                ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
                ("symlink", "/lib", "usr/lib"),
                ("symlink", "/sbin/init", "/lib/systemd/systemd"),
            ],
        )
    )
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable is True


# ------------------------------------------------- nothing from the image runs


def test_probing_executes_nothing_at_all(tmp_path, monkeypatch):
    """No `podman run`, no /bin/sh from the task. Detection is a tar read."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"the probe executed something: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "check_output", refuse)

    info = probe_rootfs_tar(write_tar(tmp_path, bootable_spec()))
    assert info.systemd_bootable is True


def test_probing_extracts_nothing_onto_the_host(tmp_path):
    """A hostile member name stays a lookup key, never a host path."""
    tar = write_tar(
        tmp_path,
        [
            ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
            ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
            ("file", "/etc/passwd", b"root:x:0:0::/root:/bin/sh\n"),
        ],
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    probe_rootfs_tar(tar)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_archive_view_never_follows_a_host_symlink(tmp_path):
    """An absolute target names a guest path even when one exists on the host."""
    tar = write_tar(tmp_path, [("symlink", "/sbin/init", "/etc/hostname")])
    with RootfsArchive.open(tar) as archive:
        # /etc/hostname exists on the host; it does not exist in this guest.
        assert archive.resolve("/sbin/init") is None
        assert archive.read_file("/sbin/init") is None


# ------------------------------------------------------------ plan validation


def a_plan(**overrides) -> SystemdProvisionPlan:
    fields = {
        "strategy": "apt-systemd",
        "steps": (BuildRun(argv=("apt-get", "install", "-y", "systemd")),),
    }
    fields.update(overrides)
    return SystemdProvisionPlan(**fields)


def test_a_well_formed_plan_passes_through_unchanged():
    plan = a_plan()
    assert validate_provision_plan(plan) is plan


@pytest.mark.parametrize(
    ("plan", "error", "message"),
    [
        ("apt-systemd", TypeError, "must be a SystemdProvisionPlan"),
        (a_plan(strategy=b"apt"), TypeError, "strategy must be a str"),
        (a_plan(strategy=""), SystemdBootError, "cannot be empty"),
        (a_plan(strategy="   "), SystemdBootError, "cannot be empty"),
        (a_plan(strategy="apt\x00systemd"), SystemdBootError, "NUL byte"),
        (a_plan(steps=[BuildRun(argv=("x",))]), TypeError, "steps must be a tuple"),
        (a_plan(steps=()), SystemdBootError, "declares no steps"),
        (a_plan(steps=("apt-get install",)), TypeError, "must be a BuildRun"),
        (a_plan(steps=(BuildRun(argv=["x"]),)), TypeError, "argv must be a tuple"),
        (a_plan(steps=(BuildRun(argv=()),)), SystemdBootError, "argv is empty"),
        (a_plan(steps=(BuildRun(argv=(1,)),)), TypeError, "argv item must be a str"),
        (
            a_plan(steps=(BuildRun(argv=("apt\x00get",)),)),
            SystemdBootError,
            "NUL byte",
        ),
    ],
)
def test_a_malformed_plan_is_refused(plan, error, message):
    with pytest.raises(error, match=message):
        validate_provision_plan(plan)


def test_a_plan_may_ask_for_a_shell_but_must_say_so_out_loud():
    """Shell semantics are available; they are never implied."""
    plan = a_plan(
        steps=(
            BuildRun(
                argv=("/bin/sh", "-c", "apt-get update && apt-get -y dist-upgrade")
            ),
        )
    )
    assert validate_provision_plan(plan) is plan


# ------------------------------------------------------ the derived build file


def test_the_derived_recipe_is_from_user_zero_and_json_run():
    recipe = render_derived_build_file(
        source_tag="localhost/titanium-cella-build:abc", plan=a_plan()
    ).decode()
    lines = [line for line in recipe.splitlines() if not line.startswith("#")]
    assert lines[0] == "FROM localhost/titanium-cella-build:abc"
    assert lines[1] == "USER 0"
    assert lines[2] == 'RUN ["apt-get", "install", "-y", "systemd"]'


def test_the_derived_recipe_is_deterministic():
    first = render_derived_build_file(source_tag="localhost/t:1", plan=a_plan())
    second = render_derived_build_file(source_tag="localhost/t:1", plan=a_plan())
    assert first == second
    # No timestamp, no host detail.
    assert b"20" not in first.split(b"FROM")[0].replace(b"A2", b"")


def test_argv_is_never_joined_into_a_shell_string():
    """`&&` in an argument is an argument, not a command separator."""
    recipe = render_derived_build_file(
        source_tag="localhost/t:1",
        plan=a_plan(steps=(BuildRun(argv=("echo", "a && rm -rf /")),)),
    ).decode()
    assert 'RUN ["echo", "a && rm -rf /"]' in recipe
    assert "RUN echo a && rm -rf /" not in recipe


def test_the_derived_recipe_carries_no_task_runtime_semantics():
    recipe = render_derived_build_file(
        source_tag="localhost/t:1", plan=a_plan()
    ).decode()
    for directive in ("CMD", "ENTRYPOINT", "WORKDIR", "LABEL", "ENV", "EXPOSE"):
        assert directive not in recipe


def test_every_plan_step_becomes_one_run_in_order():
    plan = a_plan(
        steps=(
            BuildRun(argv=("apt-get", "update")),
            BuildRun(argv=("apt-get", "install", "-y", "systemd")),
            BuildRun(argv=("systemctl", "set-default", "multi-user.target")),
        )
    )
    runs = [
        line
        for line in render_derived_build_file(source_tag="localhost/t:1", plan=plan)
        .decode()
        .splitlines()
        if line.startswith("RUN ")
    ]
    assert runs == [
        'RUN ["apt-get", "update"]',
        'RUN ["apt-get", "install", "-y", "systemd"]',
        'RUN ["systemctl", "set-default", "multi-user.target"]',
    ]


# ------------------------------------------------------------- preparation


@pytest.fixture
def fake_derived_build(monkeypatch, tmp_path):
    """Replace the derived build with a recorder. Nothing reaches podman."""
    calls: dict[str, list] = {
        "build": [],
        "inspect": [],
        "export": [],
        "untag": [],
    }
    state = {"calls": calls, "produces": bootable_spec(), "image_id": "sha256:derived"}

    def fake_build(**kwargs):
        calls["build"].append(
            {
                "recipe": kwargs["build_file"].read_bytes(),
                "tag": kwargs["tag"],
                "pull": kwargs["pull"],
                "context_dir": kwargs["context_dir"],
            }
        )

    def fake_inspect(reference, **_kwargs):
        calls["inspect"].append(reference)
        return [{"Id": state["image_id"], "Config": {}}]

    def fake_export(*, image, dest_tar, timeout_sec=None):
        calls["export"].append(image)
        produced = write_tar(dest_tar.parent, state["produces"], name=dest_tar.name)
        assert produced == dest_tar

    monkeypatch.setattr(cella_systemd, "build_image", fake_build)
    monkeypatch.setattr(cella_systemd, "inspect_image", fake_inspect)
    monkeypatch.setattr(cella_systemd, "export_rootfs_tar", fake_export)
    monkeypatch.setattr(cella_systemd, "untag_image", calls["untag"].append)
    return state


def _prepare(tmp_path, source_tar, planner, **overrides):
    kwargs = {
        "source_tag": "localhost/titanium-cella-build:src",
        "source_image_id": "sha256:source",
        "source_rootfs_tar": source_tar,
        "work_dir": tmp_path,
        "plan_provisioning": planner,
    }
    kwargs.update(overrides)
    return prepare_systemd_rootfs(**kwargs)


def test_an_already_bootable_guest_needs_no_planner_and_no_build(
    fake_derived_build, tmp_path
):
    def refuse(_info):
        raise AssertionError("the planner was called for a bootable guest")

    source = write_tar(tmp_path, bootable_spec())
    prepared = _prepare(tmp_path, source, refuse)

    assert isinstance(prepared, PreparedSystemdRootfs)
    assert prepared.derived is False
    assert prepared.strategy == STRATEGY_ALREADY_SYSTEMD
    assert prepared.rootfs_tar == source
    assert prepared.recipe_bytes is None
    assert prepared.boot_image_id == "sha256:source"
    assert prepared.source_info == prepared.final_info
    assert fake_derived_build["calls"]["build"] == []
    assert fake_derived_build["calls"]["export"] == []


def test_a_non_bootable_guest_calls_the_planner_exactly_once(
    fake_derived_build, tmp_path
):
    seen = []

    def planner(info):
        seen.append(info)
        return a_plan()

    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    _prepare(tmp_path, source, planner)

    assert len(seen) == 1
    assert isinstance(seen[0], GuestOsInfo)
    assert seen[0].os_id == "debian"
    assert seen[0].systemd_bootable is False


def test_the_derived_build_is_local_explicit_and_cleaned_up(
    fake_derived_build, tmp_path
):
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    prepared = _prepare(tmp_path, source, lambda _info: a_plan())

    build = fake_derived_build["calls"]["build"][0]
    recipe = build["recipe"].decode()
    assert "FROM localhost/titanium-cella-build:src" in recipe
    assert "USER 0" in recipe
    assert 'RUN ["apt-get", "install", "-y", "systemd"]' in recipe
    # The parent is a local tag this conversion just built; there is nothing
    # to pull, and a registry round trip could resolve FROM to another image.
    assert build["pull"] == "never"
    assert build["tag"].startswith("localhost/titanium-cella-systemd:")
    # Untagged on the way out, never removed.
    assert fake_derived_build["calls"]["untag"] == [build["tag"]]
    assert prepared.recipe_bytes == build["recipe"]


def test_the_final_tar_is_the_derived_one_when_provisioning_ran(
    fake_derived_build, tmp_path
):
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    prepared = _prepare(tmp_path, source, lambda _info: a_plan())

    assert prepared.derived is True
    assert prepared.rootfs_tar != source
    assert prepared.rootfs_tar.name == "rootfs-systemd.tar"
    assert prepared.boot_image_id == "sha256:derived"
    assert prepared.strategy == "apt-systemd"
    # Source facts and final facts are both kept, and they differ.
    assert prepared.source_info.systemd_bootable is False
    assert prepared.final_info.systemd_bootable is True


def test_the_derived_tag_is_untagged_even_when_the_build_fails(
    fake_derived_build, tmp_path, monkeypatch
):
    def explode(**kwargs):
        raise RuntimeError("build blew up")

    monkeypatch.setattr(cella_systemd, "build_image", explode)
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    with pytest.raises(RuntimeError, match="build blew up"):
        _prepare(tmp_path, source, lambda _info: a_plan())
    assert len(fake_derived_build["calls"]["untag"]) == 1


def test_a_successful_build_that_did_not_work_is_still_a_failure(
    fake_derived_build, tmp_path
):
    """Exit zero is not evidence. The filesystem has to prove it."""
    fake_derived_build["produces"] = [
        ("file", "/etc/os-release", DEBIAN_OS_RELEASE),
        ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
        # Installed, but nothing repointed init.
    ]
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    with pytest.raises(SystemdBootError, match="still does not boot systemd"):
        _prepare(tmp_path, source, lambda _info: a_plan())


def test_a_build_that_left_another_init_in_place_is_a_failure(
    fake_derived_build, tmp_path
):
    fake_derived_build["produces"] = [
        ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
        ("file", "/usr/sbin/sysvinit", b"not systemd"),
        ("symlink", "/sbin/init", "/usr/sbin/sysvinit"),
    ]
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    with pytest.raises(SystemdBootError, match="/usr/sbin/sysvinit"):
        _prepare(tmp_path, source, lambda _info: a_plan())


def test_a_malformed_plan_stops_before_any_build(fake_derived_build, tmp_path):
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    with pytest.raises(SystemdBootError, match="declares no steps"):
        _prepare(tmp_path, source, lambda _info: a_plan(steps=()))
    assert fake_derived_build["calls"]["build"] == []


# ------------------------------------------------------- the provisioning policy

#: What the policy returns for every guest it supports, exactly.
DEBIAN_PLAN = SystemdProvisionPlan(
    strategy="debian-systemd",
    steps=(
        BuildRun(argv=("/usr/bin/apt-get", "update")),
        BuildRun(
            argv=(
                "/usr/bin/env",
                "DEBIAN_FRONTEND=noninteractive",
                "/usr/bin/apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "systemd",
                "systemd-sysv",
            )
        ),
        BuildRun(argv=("/bin/rm", "-f", "/etc/machine-id")),
        BuildRun(argv=("/usr/bin/touch", "/etc/machine-id")),
    ),
)


def guest(**overrides) -> GuestOsInfo:
    """A Debian 12 guest with no systemd and no init, unless overridden."""
    fields = {
        "os_id": "debian",
        "id_like": (),
        "version_id": "12",
        "pretty_name": "Debian GNU/Linux 12 (bookworm)",
        "init_present": False,
        "init_resolved_path": None,
        "systemd_path": None,
        "systemd_bootable": False,
    }
    fields.update(overrides)
    return GuestOsInfo(**fields)


def test_debian_12_with_no_systemd_and_no_init_gets_the_debian_plan():
    assert plan_systemd_provisioning(guest()) == DEBIAN_PLAN


def test_the_plan_the_policy_returns_passes_the_shape_check():
    plan = plan_systemd_provisioning(guest())
    assert validate_provision_plan(plan) is plan


@pytest.mark.parametrize(
    "identity",
    [
        {"os_id": "debian", "id_like": ()},
        {"os_id": "ubuntu", "id_like": ("debian",)},
        {"os_id": "linuxmint", "id_like": ("ubuntu",)},
        {"os_id": "pop", "id_like": ("ubuntu", "debian")},
        {"os_id": "raspbian", "id_like": ("debian",)},
        # ID absent but ID_LIKE still names the family.
        {"os_id": None, "id_like": ("debian",)},
    ],
)
def test_every_debian_family_guest_gets_the_same_plan(identity):
    """ID and ID_LIKE are one namespace; a match in either is a match."""
    assert plan_systemd_provisioning(guest(**identity)) == DEBIAN_PLAN


@pytest.mark.parametrize(
    "identity",
    [
        {"os_id": "Debian"},
        {"os_id": "UBUNTU"},
        {"os_id": "mint", "id_like": ("Ubuntu",)},
    ],
)
def test_family_matching_is_case_insensitive(identity):
    assert plan_systemd_provisioning(guest(**identity)) == DEBIAN_PLAN


def test_an_existing_non_systemd_init_is_refused_not_replaced():
    """Whatever is already PID 1 was chosen by someone. It is not overwritten."""
    with pytest.raises(SystemdBootError, match="Refusing to replace an existing"):
        plan_systemd_provisioning(
            guest(init_present=True, init_resolved_path="/usr/sbin/sysvinit")
        )


def test_the_existing_init_refusal_names_what_it_found():
    with pytest.raises(SystemdBootError, match="/usr/sbin/sysvinit"):
        plan_systemd_provisioning(
            guest(init_present=True, init_resolved_path="/usr/sbin/sysvinit")
        )


def test_a_supported_family_does_not_authorize_replacing_an_init():
    """Knowing how to install systemd on Debian is not permission to."""
    with pytest.raises(SystemdBootError, match="Refusing to replace"):
        plan_systemd_provisioning(
            guest(
                os_id="ubuntu",
                id_like=("debian",),
                init_present=True,
                init_resolved_path="/sbin/openrc-init",
            )
        )


@pytest.mark.parametrize(
    "identity",
    [
        {"os_id": "alpine", "id_like": ()},
        {"os_id": "fedora", "id_like": ()},
        {"os_id": "rhel", "id_like": ("fedora",)},
        {"os_id": "arch", "id_like": ()},
        {"os_id": "opensuse-leap", "id_like": ("suse", "opensuse")},
        # No os-release at all: FROM scratch, or an image that declares nothing.
        {"os_id": None, "id_like": ()},
    ],
)
def test_an_unsupported_guest_is_refused_with_no_fallback(identity):
    """Several of these ship systemd themselves.

    That is not enough. The policy supports the families whose provisioning it
    has actually validated, and refuses the rest rather than guessing at dnf,
    pacman or zypper.
    """
    with pytest.raises(SystemdBootError, match="No validated systemd provisioning"):
        plan_systemd_provisioning(guest(**identity))


def test_the_unsupported_refusal_reports_what_it_saw():
    with pytest.raises(SystemdBootError, match="'alpine'"):
        plan_systemd_provisioning(guest(os_id="alpine"))


def test_an_already_bootable_guest_is_refused_by_the_policy_itself():
    """Defence in depth. Preparation never calls the planner in this state."""
    with pytest.raises(SystemdBootError, match="already systemd-bootable"):
        plan_systemd_provisioning(
            guest(
                systemd_bootable=True,
                init_present=True,
                init_resolved_path="/usr/lib/systemd/systemd",
                systemd_path="/usr/lib/systemd/systemd",
            )
        )


def test_the_bootable_check_precedes_the_existing_init_check():
    """A bootable guest also has an init; the more specific message wins."""
    with pytest.raises(SystemdBootError, match="already systemd-bootable"):
        plan_systemd_provisioning(
            guest(
                systemd_bootable=True,
                init_present=True,
                init_resolved_path="/usr/lib/systemd/systemd",
            )
        )


def test_machine_id_is_cleared_before_it_is_recreated():
    argvs = [step.argv for step in plan_systemd_provisioning(guest()).steps]
    assert argvs.index(("/bin/rm", "-f", "/etc/machine-id")) < argvs.index(
        ("/usr/bin/touch", "/etc/machine-id")
    )


def test_every_policy_command_is_an_absolute_path():
    """No PATH lookup decides which binary provisioning runs."""
    for step in plan_systemd_provisioning(guest()).steps:
        assert step.argv[0].startswith("/"), step.argv


# ------------------------------------------- what the policy's recipe becomes

EXPECTED_DEBIAN_RUNS = [
    'RUN ["/usr/bin/apt-get", "update"]',
    (
        'RUN ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", '
        '"/usr/bin/apt-get", "install", "-y", "--no-install-recommends", '
        '"systemd", "systemd-sysv"]'
    ),
    'RUN ["/bin/rm", "-f", "/etc/machine-id"]',
    'RUN ["/usr/bin/touch", "/etc/machine-id"]',
]


def debian_recipe(tag: str = "localhost/titanium-cella-build:probe") -> str:
    return render_derived_build_file(
        source_tag=tag, plan=plan_systemd_provisioning(guest())
    ).decode()


def test_the_debian_recipe_is_exactly_these_directives():
    directives = [
        line for line in debian_recipe().splitlines() if not line.startswith("#")
    ]
    assert directives == [
        "FROM localhost/titanium-cella-build:probe",
        "USER 0",
        *EXPECTED_DEBIAN_RUNS,
    ]


def test_every_run_in_the_debian_recipe_is_json_exec_form():
    runs = [line for line in debian_recipe().splitlines() if line.startswith("RUN")]
    assert runs == EXPECTED_DEBIAN_RUNS
    for line in runs:
        assert line.startswith('RUN ["'), line


def test_the_debian_recipe_introduces_no_shell_interpolation():
    """Exec form throughout: no shell parses any of these argv items."""
    runs = "\n".join(
        line for line in debian_recipe().splitlines() if line.startswith("RUN")
    )
    for metacharacter in ("&&", "||", ";", "|", "$(", "${", "`", ">", "<", "*"):
        assert metacharacter not in runs, metacharacter


def test_the_environment_assignment_travels_as_an_argv_item():
    """DEBIAN_FRONTEND reaches apt through /usr/bin/env, visibly.

    Written as a shell prefix it would need a shell to mean anything, and the
    plan would have acquired an interpreter it never asked for.
    """
    runs = debian_recipe()
    assert '"/usr/bin/env", "DEBIAN_FRONTEND=noninteractive"' in runs
    assert "DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get" not in runs
    assert "RUN /usr/bin/env" not in runs


def test_the_debian_recipe_is_deterministic_across_calls():
    assert debian_recipe() == debian_recipe()


def test_the_debian_recipe_carries_no_task_runtime_semantics():
    recipe = debian_recipe()
    for directive in ("CMD", "ENTRYPOINT", "WORKDIR", "LABEL", "ENV", "EXPOSE"):
        assert directive not in recipe


# --------------------------------------------- the policy through preparation


def test_the_real_policy_is_not_invoked_for_an_already_bootable_guest(
    fake_derived_build, tmp_path
):
    """It would raise if it were called, so a clean result proves it was not."""
    source = write_tar(tmp_path, bootable_spec())
    prepared = _prepare(tmp_path, source, plan_systemd_provisioning)

    assert prepared.strategy == STRATEGY_ALREADY_SYSTEMD
    assert prepared.derived is False
    assert prepared.rootfs_tar == source
    assert fake_derived_build["calls"]["build"] == []
    assert fake_derived_build["calls"]["export"] == []


def test_the_real_policy_drives_a_whole_preparation(fake_derived_build, tmp_path):
    """Debian source in, provisioned filesystem out. No package manager runs."""
    source = write_tar(tmp_path, [("file", "/etc/os-release", DEBIAN_OS_RELEASE)])
    prepared = _prepare(tmp_path, source, plan_systemd_provisioning)

    assert prepared.strategy == "debian-systemd"
    assert prepared.derived is True
    assert prepared.source_info.systemd_bootable is False
    assert prepared.final_info.systemd_bootable is True

    recipe = fake_derived_build["calls"]["build"][0]["recipe"].decode()
    for expected in EXPECTED_DEBIAN_RUNS:
        assert expected in recipe
    assert prepared.recipe_bytes == recipe.encode()


def test_an_unsupported_guest_stops_preparation_before_any_build(
    fake_derived_build, tmp_path
):
    source = write_tar(
        tmp_path,
        [("file", "/etc/os-release", b"ID=alpine\nVERSION_ID=3.22\n")],
    )
    with pytest.raises(SystemdBootError, match="No validated systemd provisioning"):
        _prepare(tmp_path, source, plan_systemd_provisioning)
    assert fake_derived_build["calls"]["build"] == []


def test_a_guest_with_another_init_stops_preparation_before_any_build(
    fake_derived_build, tmp_path
):
    source = write_tar(
        tmp_path,
        [
            ("file", "/etc/os-release", DEBIAN_OS_RELEASE),
            ("file", "/usr/sbin/sysvinit", b"not systemd"),
            ("symlink", "/sbin/init", "/usr/sbin/sysvinit"),
        ],
    )
    with pytest.raises(SystemdBootError, match="Refusing to replace"):
        _prepare(tmp_path, source, plan_systemd_provisioning)
    assert fake_derived_build["calls"]["build"] == []


def test_the_seam_is_injected_never_imported_by_the_preparation():
    """prepare_systemd_rootfs takes the planner as an argument, so the module
    cannot acquire an opinion about package managers by accident."""
    import inspect

    signature = inspect.signature(prepare_systemd_rootfs)
    assert "plan_provisioning" in signature.parameters
    source = inspect.getsource(prepare_systemd_rootfs)
    assert "plan_systemd_provisioning(" not in source


# ------------------------------------------------- A2 boot proof scaffold


PROBE_UNIT_PATH = "/etc/systemd/system/titanium-a2-probe.service"
PROBE_WANTS_PATH = (
    "/etc/systemd/system/multi-user.target.wants/titanium-a2-probe.service"
)
PROBE_MARKER = "A2_SYSTEMD_PROBE"

PROBE_UNIT = b"""\
[Unit]
Description=Titanium A2 systemd boot proof
After=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo A2_SYSTEMD_PROBE; echo PID1=$(cat /proc/1/comm)'
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
"""


def probe_boot_layer(_inputs) -> BootLayer:
    """TEST-ONLY. A minimal unit that proves systemd reached multi-user.

    Deliberately not in production code: the production boot layer is empty
    until A3 puts the real controller in it, and a probe service shipped by
    default would be a boot policy invented by scaffolding.
    """
    return BootLayer(
        entries=(
            GuestFile(
                path=PROBE_UNIT_PATH, contents=PROBE_UNIT, mode=0o644, uid=0, gid=0
            ),
            # How systemd enablement is actually spelled: a relative link from
            # the target's .wants directory back to the unit.
            GuestSymlink(
                path=PROBE_WANTS_PATH,
                target="../titanium-a2-probe.service",
                uid=0,
                gid=0,
            ),
        )
    )


def test_the_probe_layer_is_a_valid_installable_boot_layer():
    """Runs always: the scaffold's own shape is checkable without a VM."""
    layer = probe_boot_layer(None)
    assert validate_boot_layer(layer) is layer
    assert PROBE_MARKER in PROBE_UNIT.decode()


def test_the_production_boot_layer_carries_no_probe():
    """The scaffold must never leak into what a real conversion ships."""
    from titanium.environments.cella.boot_layer import render_boot_layer

    assert render_boot_layer(None) == BootLayer(entries=())


UNKNOWN_GUEST = GuestOsInfo(
    os_id=None,
    id_like=(),
    version_id=None,
    pretty_name=None,
    init_present=False,
    init_resolved_path=None,
    systemd_path=None,
    systemd_bootable=False,
)

LIVE_ENV = "TITANIUM_CELLA_LIVE"

#: A Debian-family base that ships neither systemd nor /sbin/init, which is
#: what makes it a real subject for the provisioning policy. Small and
#: standard on purpose: the proof is of the architecture, not of a corpus task.
BASE_IMAGE = os.environ.get(
    "TITANIUM_A2_BASE_IMAGE", "docker.io/library/debian:12-slim"
)

FLAVOR = "titanium-a2-probe"
EXT4_BYTES = 2 * 1024 * 1024 * 1024
GUEST_MEM_MB = "1024"
BOOT_TIMEOUT_SEC = float(os.environ.get("TITANIUM_A2_BOOT_TIMEOUT", "240"))

#: The lab flavor is the whole reason this is runnable: a release machine gets
#: no console.log and no console.sock (cella machine.rs, "The console exists
#: only in the lab"), so there would be nothing to read.
DEFAULT_CELLA_BIN = (
    Path.home() / "workspace" / "omne" / "cella" / "target" / "smoke" / "cella"
)


def _cella_bin() -> Path:
    return Path(os.environ.get("CELLA_BIN", str(DEFAULT_CELLA_BIN)))


def _traversable_by_others(path: Path) -> bool:
    """Whether every directory above *path* grants o+x.

    The VMM's jail maps a sub-user per machine
    (security/profiles/cella-vmm/bwrap.txt), and bwrap binds the VMM binary
    *as that sub-user*. A home directory of 0710 therefore makes the bind fail
    with "Can't find source path ...: Permission denied" long before KVM is
    reached -- an ownership fact about the host, not a fault in the artifact.
    """
    for parent in list(path.parents)[:-1]:
        try:
            if not (parent.stat().st_mode & stat.S_IXOTH):
                return False
        except OSError:
            return False
    return True


def _staged_cella(work: Path) -> Path:
    """The cella CLI, relocated under /tmp when its own path is unreachable.

    Copies the whole persona set, because the shim resolves each verb to a
    sibling beside its own inode and `spawn` resolves the VMM the same way.
    Nothing about Cella is modified or reconfigured; only the directory the
    binaries are read from changes.
    """
    source = _cella_bin()
    if _traversable_by_others(source):
        return source
    staged = work / "bin"
    staged.mkdir(parents=True, exist_ok=True)
    staged.chmod(0o755)
    for entry in source.parent.iterdir():
        if (
            entry.is_file()
            and entry.name.startswith("cella")
            and os.access(entry, os.X_OK)
        ):
            shutil.copy2(entry, staged / entry.name)
    print(
        f"[a2] staged the cella persona set into {staged} ({source.parent} is not traversable by the jail's sub-user)"
    )
    return staged / source.name


def _cella(binary: Path, *args: str, home: Path, timeout: float = 120.0):
    env = {**os.environ, "CELLA_HOME": str(home)}
    return subprocess.run(
        [str(binary), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _planner_is_implemented() -> bool:
    """Whether a provisioning policy exists.

    A policy that declines this synthetic guest still counts: what is being
    asked is whether the seam is filled, not what it decides.
    """
    try:
        plan_systemd_provisioning(UNKNOWN_GUEST)
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001
        return True
    return True


def _live_boot_blockers() -> list[str]:
    """Every unmet precondition for the live boot proof, named explicitly."""
    from titanium.environments.cella.podman import podman_bin

    blockers = []
    if os.environ.get(LIVE_ENV) != "1":
        blockers.append(
            f"{LIVE_ENV}=1 is not set (this builds images and boots real VMs)"
        )
    if not _planner_is_implemented():
        blockers.append(
            "systemd_boot.plan_systemd_provisioning() is not implemented, so "
            "no non-systemd task image can be converted"
        )
    if shutil.which(podman_bin()) is None:
        blockers.append(f"{podman_bin()} is not on PATH")
    binary = _cella_bin()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        blockers.append(
            f"no cella CLI at {binary} -- build the lab flavor (make build) or "
            f"set CELLA_BIN. A release-flavor cella cannot be used: it writes "
            f"no console.log, so the guest's own output is unreadable"
        )
    return blockers


def _pid_of(home: Path, machine: str) -> int | None:
    try:
        return int((home / "machines" / machine / "pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_identity(pid: int) -> str | None:
    """A fingerprint that survives PID reuse, or None if the pid is gone.

    ``starttime`` (field 22 of ``/proc/<pid>/stat``) is stamped once when the
    process is created and never changes, so pid+starttime names one process
    for the life of the host boot. It is read rather than the exe link
    because ``/proc/<pid>/stat`` stays world-readable even when the process
    runs under the jail's delegated sub-user, where ``exe`` and ``cmdline``
    may not be.

    The comm field can contain spaces and parentheses, so the split is taken
    after the final ``)``: from there, index 0 is `state` (field 3), and
    starttime (field 22) is index 19.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        starttime = stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        cmdline = ""
    return f"{starttime}|{cmdline}"


def _is_same_process(pid: int, identity: str | None) -> bool:
    """True only when *pid* still names the process *identity* came from."""
    if identity is None:
        return _alive(pid)
    return _process_identity(pid) == identity


def _process_snapshot(pid: int | None) -> str:
    """What the VMM process is doing, read-only, from /proc.

    Nothing here signals or ptraces the VMM; a stalled guest must be observed
    exactly as it was found, or the observation is about the observer.
    """
    if pid is None:
        return "  (no pid recorded)"
    if not _alive(pid):
        return f"  pid {pid}: NOT ALIVE (exited or reaped)"

    lines = [f"  pid {pid}: alive"]
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        wanted = (
            "Name:",
            "State:",
            "Uid:",
            "Gid:",
            "Threads:",
            "VmRSS:",
            "voluntary_ctxt",
        )
        lines += [
            f"    {line}" for line in status.splitlines() if line.startswith(wanted)
        ]
    except OSError as exc:
        lines.append(f"    /proc/{pid}/status unreadable: {exc}")

    try:
        for task in sorted(Path(f"/proc/{pid}/task").iterdir()):
            comm = (task / "comm").read_text().strip()
            state = (task / "stat").read_text().split(") ", 1)[1].split(" ", 1)[0]
            try:
                wchan = (task / "wchan").read_text().strip() or "-"
            except OSError:
                wchan = "?"
            lines.append(f"    thread {task.name} {comm!r} state={state} wchan={wchan}")
    except OSError as exc:
        lines.append(f"    threads unreadable: {exc}")
    return "\n".join(lines)


def _tail(path: Path, limit: int) -> str:
    if not path.is_file():
        return "(absent)"
    return path.read_text(errors="replace")[-limit:]


def _boot_once(binary: Path, home: Path, machine: str, label: str = "boot") -> str:
    """Create, start, and read the guest's own console until it proves itself.

    Returns the console text. Raises AssertionError with the console, the
    vmm.log, and a read-only snapshot of the VMM process attached when either
    marker fails to appear inside the timeout.
    """
    machine_dir = home / "machines" / machine
    console = machine_dir / "console.log"
    vmm_log = machine_dir / "vmm.log"
    pid: int | None = None
    try:
        created = _cella(
            binary,
            "create",
            machine,
            "--kernel",
            "canonical",
            "--rootfs",
            FLAVOR,
            "--mem-mb",
            GUEST_MEM_MB,
            "--net",
            "none",
            "--root",
            "rw",
            home=home,
        )
        assert created.returncode == 0, f"cella create failed:\n{created.stderr}"
        print(f"[a2] {label}: machine created ({machine})")

        # The jail binds the machine directory as a per-machine sub-user. A
        # restrictive operator umask would leave it 0700 and unreachable, so
        # the harness does not depend on what umask happened to be set.
        for path in (home / "machines", machine_dir):
            if path.is_dir():
                path.chmod(0o755)

        started = _cella(binary, "start", machine, home=home)
        assert started.returncode == 0, f"cella start failed:\n{started.stderr}"

        # Read before anything can clear it: `cella stop` deletes the pid file
        # as a transient, so this is the only point at which the VMM can still
        # be named.
        pid = _pid_of(home, machine)
        identity = _process_identity(pid) if pid is not None else None
        print(f"[a2] {label}: machine started, vmm pid={pid}")
        print(f"[a2] {label}: waiting for probe (timeout {BOOT_TIMEOUT_SEC}s)")

        started_at = time.monotonic()
        deadline = started_at + BOOT_TIMEOUT_SEC
        text = ""
        console_bytes = 0
        last_growth = started_at
        while time.monotonic() < deadline:
            if console.is_file():
                text = console.read_text(errors="replace")
                if len(text) != console_bytes:
                    console_bytes = len(text)
                    last_growth = time.monotonic()
                if PROBE_MARKER in text and "PID1=systemd" in text:
                    elapsed = time.monotonic() - started_at
                    print(
                        f"[a2] {label}: proved in {elapsed:.1f}s "
                        f"({console_bytes} console bytes)"
                    )
                    return text
            if pid is not None and not _alive(pid):
                elapsed = time.monotonic() - started_at
                raise AssertionError(
                    f"the VMM exited after {elapsed:.1f}s without the guest "
                    f"proving itself\n"
                    f"--- console.log ({console_bytes} bytes, tail) ---\n"
                    f"{text[-4000:]}\n"
                    f"--- vmm.log (tail) ---\n{_tail(vmm_log, 2000)}"
                )
            time.sleep(0.25)

        # Timed out with the VMM still up: the interesting case. Snapshot it
        # before the finally block kills it.
        now = time.monotonic()
        info = _cella(binary, "info", machine, home=home, timeout=30.0)
        raise AssertionError(
            f"the guest never proved itself within {BOOT_TIMEOUT_SEC}s\n"
            f"  {PROBE_MARKER} seen: {PROBE_MARKER in text}\n"
            f"  PID1=systemd seen: {'PID1=systemd' in text}\n"
            f"  console bytes: {console_bytes}, "
            f"last grew {now - last_growth:.1f}s ago "
            f"({last_growth - started_at:.1f}s after start)\n"
            f"--- vmm process ---\n{_process_snapshot(pid)}\n"
            f"--- cella info ---\n{info.stdout.strip() or info.stderr.strip()}\n"
            f"--- console.log (tail) ---\n{text[-4000:]}\n"
            f"--- vmm.log (tail) ---\n{_tail(vmm_log, 2000)}"
        )
    finally:
        # The ordinary path first, so that whether it actually reaps the VMM
        # is measured rather than masked. `cella stop` clears the pid file as
        # a transient, which is why the pid and its fingerprint were captured
        # at start: afterwards there is nothing left in the machine directory
        # to name the process by.
        _cella(binary, "stop", machine, home=home, timeout=60.0)

        reaped = True
        if pid is not None:
            if not _is_same_process(pid, identity):
                print(f"[a2] {label}: VMM pid={pid} was gone after cella stop")
            else:
                print(f"[a2] {label}: WARNING VMM pid={pid} survived cella stop")
                # Snapshot before disturbing it: the state of a VMM that
                # outlived its own stop is the evidence.
                print(_process_snapshot(pid))
                try:
                    os.kill(pid, signal.SIGKILL)
                except PermissionError:
                    print(
                        f"[a2] {label}: WARNING cannot signal pid={pid} "
                        f"(EPERM -- it runs as the jail's delegated sub-user, "
                        f"which is also what `cella stop` would have hit)"
                    )
                except (OSError, ProcessLookupError):
                    pass
                for _ in range(200):
                    if not _is_same_process(pid, identity):
                        break
                    time.sleep(0.05)
                if _is_same_process(pid, identity):
                    reaped = False
                    print(
                        f"[a2] {label}: WARNING VMM pid={pid} survived the "
                        f"fallback SIGKILL and is STILL RUNNING; leaving "
                        f"{machine_dir} in place rather than destroying a "
                        f"machine directory out from under a live VMM"
                    )
                else:
                    print(f"[a2] {label}: VMM pid={pid} reaped by fallback SIGKILL")

        if reaped:
            _cella(binary, "destroy", machine, home=home, timeout=60.0)


def test_a2_systemd_boot_proof(tmp_path):
    """The whole A2 claim, against a real Debian image and a real Cella guest.

    task filesystem -> Titanium systemd adaptation -> ext4 -> Cella boot ->
    systemd as PID 1 -> an ordinary systemd service runs.

    Gated, never silently: a skip names the exact precondition that is
    missing. The probe unit is TEST-ONLY; production render_boot_layer stays
    empty, which a separate test asserts.
    """
    blockers = _live_boot_blockers()
    if blockers:
        pytest.skip("A2 live boot proof not runnable: " + "; ".join(blockers))

    # /tmp, never under $HOME: the jail's sub-user has to reach both the
    # machine directory and the binaries, and a 0710 home defeats it.
    work = Path(tempfile.mkdtemp(prefix="titanium-a2-"))
    # mkdtemp makes 0700, which the jail's sub-user cannot traverse either.
    work.chmod(0o755)
    home = work / "cella-home"
    home.mkdir()
    home.chmod(0o755)
    binary = _staged_cella(work)

    try:
        # Gated against the operator's real home, which is where the goldens
        # live; the temporary one is still empty at this point.
        real_home = Path(os.environ.get("CELLA_HOME", Path.home() / ".cella"))
        gate = _cella(
            binary,
            "doctor",
            "gate",
            "kvm",
            "bwrap",
            "golden:kernel:canonical",
            home=real_home,
        )
        if gate.returncode != 0 or "SKIP" in gate.stdout:
            pytest.skip(
                f"cella doctor gate: {gate.stdout.strip() or gate.stderr.strip()}"
            )

        # Copied in rather than referenced, so the proof cannot disturb the
        # operator's own goldens.
        kernel_dir = home / "kernel" / "canonical"
        kernel_dir.mkdir(parents=True)
        for name in ("bzImage", "golden.json"):
            source = real_home / "kernel" / "canonical" / name
            if source.is_file():
                shutil.copy2(source, kernel_dir / name)

        environment = work / "task" / "environment"
        environment.mkdir(parents=True)
        (environment / "Containerfile").write_text(f"FROM {BASE_IMAGE}\n")

        facts = {}

        def identity(seen):
            facts["build"] = seen
            return FlavorIdentity(flavor_name=FLAVOR, manifest_fields={})

        # The REAL policy and the TEST-ONLY probe layer. The planner is not
        # monkeypatched: this executes the real derived podman build, and the
        # real package manager inside it.
        result = convert_task_to_rootfs_flavor(
            environment_dir=environment,
            ext4_size_bytes=EXT4_BYTES,
            render_boot_layer=probe_boot_layer,
            compute_flavor_identity=identity,
            plan_systemd_provisioning=plan_systemd_provisioning,
            home=home,
            build_timeout_sec=1800.0,
            built_epoch=1700000000,
        )

        built: BuildFacts = facts["build"]
        # The subject really was a guest that could not boot, and the
        # conversion really did fix it -- both measured off exported tars.
        assert built.systemd_source_os.systemd_bootable is False
        assert built.systemd_source_os.init_present is False
        assert built.systemd_strategy == "debian-systemd"
        assert built.systemd_final_os.systemd_bootable is True, (
            "the conversion published a filesystem it did not prove bootable"
        )
        assert result.artifact_path.is_file()
        print(
            f"[a2] conversion complete: strategy={built.systemd_strategy} "
            f"derived={built.systemd_final_os is not built.systemd_source_os} "
            f"boot_image={built.boot_image_id}"
        )

        # Two boots, fresh machines, one artifact. Nothing is rebuilt or
        # reprovisioned between them: `cella create` copies the published
        # rootfs.ext4 into each machine's disk.img, so both boots read the
        # same bytes and "it booted once" cannot pass for "it boots".
        digest_before = result.sha3_256
        for attempt in (1, 2):
            console = _boot_once(
                binary, home, f"{FLAVOR}-{attempt}", label=f"boot {attempt}"
            )
            assert PROBE_MARKER in console
            assert "PID1=systemd" in console
            print(f"[a2] boot {attempt}: {PROBE_MARKER} and PID1=systemd observed")

        assert sha3_256_file(result.artifact_path) == digest_before, (
            "the rootfs artifact changed between the two boots"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
