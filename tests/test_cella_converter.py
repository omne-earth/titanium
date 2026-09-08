"""The conversion pipeline: ordering, refusals, cleanup, and the cache rule.

Most of these stub the podman steps. What is under test is the frame -- what
happens when a step fails, what reaches the two decision functions, and what
the cache does with a flavor that does not verify -- not podman itself, which
tests/test_cella_podman_build.py and tests/test_cella_rootfs.py exercise for
real.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from titanium.environments.cella import converter as cella_converter
from titanium.environments.cella import systemd_boot as cella_systemd
from titanium.environments.cella.boot_layer import (
    BootLayer,
    BootLayerError,
    GuestFile,
    GuestSymlink,
    boot_layer_digest,
)
from titanium.environments.cella.converter import (
    CONVERTER_VERSION,
    BuildFacts,
    ConversionError,
    FlavorIdentity,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.flavor import (
    FlavorIntegrityError,
    ManifestFieldError,
    manifest_field,
    verify_flavor_dir,
)
from titanium.environments.cella.podman import PodmanError, podman_bin
from titanium.environments.cella.rootfs import RootfsBuildError
from titanium.environments.cella.systemd_boot import (
    STRATEGY_ALREADY_SYSTEMD,
    BuildRun,
    SystemdBootError,
    SystemdProvisionPlan,
)
from titanium.models.agent.install import AgentInstallSpec, InstallStep

CAPACITY = 1024 * 1024
LAYER = BootLayer(
    entries=(
        GuestFile(
            path="/etc/systemd/system/titanium.service",
            contents=b"[Unit]\n",
            mode=0o644,
            uid=0,
            gid=0,
        ),
        GuestSymlink(
            path="/etc/systemd/system/default.target",
            target="multi-user.target",
            uid=0,
            gid=0,
        ),
    )
)
EXT4 = b"pretend ext4 image"

SYSTEMD_BYTES = b"\x7fELF pretend systemd\n"
OS_RELEASE = (
    b'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'
)


def write_rootfs_tar(dest: Path, spec) -> None:
    """A synthetic `podman export`. (kind, guest path, payload-or-target)."""
    with tarfile.open(dest, "w") as archive:
        for kind, path, extra in spec:
            info = tarfile.TarInfo(path.lstrip("/"))
            info.mode = 0o755
            if kind == "file":
                info.type = tarfile.REGTYPE
                info.size = len(extra)
                archive.addfile(info, io.BytesIO(extra))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = extra
                archive.addfile(info)


BOOTABLE_TAR = [
    ("file", "/etc/os-release", OS_RELEASE),
    ("file", "/usr/lib/systemd/systemd", SYSTEMD_BYTES),
    ("symlink", "/sbin/init", "/usr/lib/systemd/systemd"),
]

UNSUPPORTED_TAR = [
    ("file", "/etc/os-release", b'ID=alpine\nVERSION_ID="3.22"\n'),
    ("file", "/bin/busybox", b"busybox"),
]

NON_BOOTABLE_TAR = [
    ("file", "/etc/os-release", OS_RELEASE),
    ("file", "/app/run.sh", b"#!/bin/sh\nexit 0\n"),
]


def refuse_to_plan(_info):
    raise AssertionError("the systemd planner was called for an already-bootable guest")


INSPECT = [
    {
        "Id": "sha256:deadbeef",
        "Digest": "sha256:cafebabe",
        "RepoDigests": [],
        "Config": {"Entrypoint": ["/app/run.sh"], "WorkingDir": "/app"},
        "Healthcheck": {"Test": ["CMD-SHELL", "true"]},
    }
]


BUILDER_ID = "sha256:builder-under-test"


def boot_layer_of(_inputs) -> BootLayer:
    return LAYER


def identity_of(_facts) -> FlavorIdentity:
    return FlavorIdentity(flavor_name="probe", manifest_fields={})


@pytest.fixture
def environment_dir(tmp_path):
    env = tmp_path / "task" / "environment"
    env.mkdir(parents=True)
    (env / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /app\n")
    return env


@pytest.fixture
def stub_podman(monkeypatch):
    """Replace every podman touchpoint. Records which steps ran."""
    ran: dict[str, int] = {}

    def counted(name, result=None, side_effect=None):
        def call(*_args, **kwargs):
            ran[name] = ran.get(name, 0) + 1
            if side_effect is not None:
                raise side_effect
            if name == "export":
                write_rootfs_tar(kwargs["dest_tar"], state["exports"])
            if name == "mkfs":
                kwargs["dest"].write_bytes(EXT4)
            return result

        return call

    state = {"ran": ran, "fail": {}, "exports": BOOTABLE_TAR}

    def install():
        for name, target in [
            ("build", "build_image"),
            ("inspect", "inspect_image"),
            ("builder", "rootfs_builder_image_id"),
            ("export", "export_rootfs_tar"),
            ("mkfs", "build_ext4"),
            ("untag", "untag_image"),
        ]:
            result = None
            if name == "inspect":
                result = INSPECT
            elif name == "builder":
                result = BUILDER_ID
            monkeypatch.setattr(
                cella_converter,
                target,
                counted(name, result=result, side_effect=state["fail"].get(name)),
            )

    state["install"] = install
    install()
    return state


def _convert(environment_dir, tmp_path, **overrides):
    kwargs = {
        "environment_dir": environment_dir,
        "ext4_size_bytes": CAPACITY,
        "render_boot_layer": boot_layer_of,
        "compute_flavor_identity": identity_of,
        # The default fixture exports an already-bootable filesystem, so a
        # planner call would mean the no-op path stopped being taken.
        "plan_systemd_provisioning": refuse_to_plan,
        "home": tmp_path / "cella",
        "built_epoch": 1700000000,
    }
    kwargs.update(overrides)
    return convert_task_to_rootfs_flavor(**kwargs)


def _published(tmp_path) -> list[Path]:
    rootfs = tmp_path / "cella" / "rootfs"
    if not rootfs.is_dir():
        return []
    return sorted(rootfs.iterdir())


# ------------------------------------------------------------------ happy path


def test_a_conversion_publishes_a_verified_flavor(
    stub_podman, environment_dir, tmp_path
):
    result = _convert(environment_dir, tmp_path)

    assert result.flavor_name == "probe"
    assert result.reused is False
    assert result.flavor_dir == tmp_path / "cella" / "rootfs" / "probe"
    assert result.artifact_path.read_bytes() == EXT4
    assert result.sha3_256 == hashlib.sha3_256(EXT4).hexdigest()
    assert result.size_bytes == len(EXT4)
    assert verify_flavor_dir(result.flavor_dir) == result.sha3_256


def test_the_manifest_states_what_cella_expects(stub_podman, environment_dir, tmp_path):
    result = _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=lambda _f: FlavorIdentity(
            flavor_name="probe",
            manifest_fields={"input_dockerfile": "a" * 64},
        ),
    )
    text = (result.flavor_dir / "golden.json").read_text()
    assert manifest_field(text, "axis") == "rootfs"
    assert manifest_field(text, "flavor") == "probe"
    assert manifest_field(text, "artifact") == "rootfs.ext4"
    assert manifest_field(text, "built_epoch") == "1700000000"
    assert manifest_field(text, "input_dockerfile") == "a" * 64
    assert (result.flavor_dir / "golden.json").stat().st_mode & 0o777 == 0o444


def test_the_build_tag_is_dropped_on_success(stub_podman, environment_dir, tmp_path):
    _convert(environment_dir, tmp_path)
    assert stub_podman["ran"]["untag"] == 1


# ------------------------------------------------------------ what the human sees


def test_the_boot_layer_decision_receives_the_whole_image_record(
    stub_podman, environment_dir, tmp_path
):
    seen = {}

    def capture(inputs):
        seen["inputs"] = inputs
        return LAYER

    _convert(environment_dir, tmp_path, render_boot_layer=capture)
    record = seen["inputs"].image
    assert record.image_id == "sha256:deadbeef"
    assert record.config["Entrypoint"] == ["/app/run.sh"]
    # Not in Config; reachable anyway.
    assert record.inspect["Healthcheck"]["Test"] == ["CMD-SHELL", "true"]


def test_the_boot_layer_decision_receives_titaniums_explicit_runtime_user(
    stub_podman, environment_dir, tmp_path
):
    """Config.User must never become the runtime-user decision by default.

    dockerfile_install_commands emits a USER per install step and restores
    nothing, so after a bake the image's Config.User is install plumbing. The
    explicit decision travels separately, and the contamination is flagged.
    """
    seen = {}

    def capture(inputs):
        seen.setdefault("all", []).append(inputs)
        return LAYER

    _convert(environment_dir, tmp_path, render_boot_layer=capture, agent_user="app")
    plain = seen["all"][-1]
    assert plain.agent_user == "app"
    assert plain.agent_install_applied is False

    spec = AgentInstallSpec(
        agent_name="probe", steps=[InstallStep(run="true", user="root")]
    )
    _convert(
        environment_dir,
        tmp_path,
        render_boot_layer=capture,
        agent_install_spec=spec,
        agent_user=None,
        compute_flavor_identity=lambda _f: FlavorIdentity("probe-baked", {}),
    )
    baked = seen["all"][-1]
    # None is Titanium's own meaning -- "the image's declared USER" -- and it
    # is passed through, not resolved here.
    assert baked.agent_user is None
    assert baked.agent_install_applied is True


def test_the_identity_decision_receives_every_shaping_input(
    stub_podman, environment_dir, tmp_path
):
    seen = {}

    def capture(facts: BuildFacts) -> FlavorIdentity:
        seen["facts"] = facts
        return FlavorIdentity(flavor_name="probe", manifest_fields={})

    spec = AgentInstallSpec(
        agent_name="probe", steps=[InstallStep(run="echo hi", user="root")]
    )
    _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=capture,
        agent_install_spec=spec,
    )
    facts: BuildFacts = seen["facts"]
    assert facts.source_build_file_name == "Dockerfile"
    assert facts.source_build_file_bytes.startswith(b"FROM ubuntu:24.04")
    # What was actually built differs from what the task shipped.
    assert b"docker.io/library/ubuntu:24.04" in facts.staged_build_file_bytes
    assert b"echo hi" in facts.staged_build_file_bytes
    assert facts.image.digest == "sha256:cafebabe"
    assert facts.boot_layer == LAYER
    assert facts.agent_install_fingerprint == spec.fingerprint()
    assert facts.ext4_size_bytes == CAPACITY
    assert facts.converter_version == CONVERTER_VERSION
    assert facts.agent_install_applied is True
    assert facts.rootfs_builder_image_id == BUILDER_ID
    assert "e2fsprogs" in facts.rootfs_builder_recipe
    # Systemd provenance: what the guest was, what it is, and how it got there.
    assert facts.systemd_strategy == STRATEGY_ALREADY_SYSTEMD
    assert facts.systemd_source_os.os_id == "debian"
    assert facts.systemd_source_os.systemd_bootable is True
    assert facts.systemd_final_os == facts.systemd_source_os
    assert facts.systemd_recipe_bytes is None
    assert facts.boot_image_id == "sha256:deadbeef"


def test_no_agent_means_no_fingerprint(stub_podman, environment_dir, tmp_path):
    seen = {}
    _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=lambda f: (
            seen.setdefault("facts", f),
            FlavorIdentity(flavor_name="probe", manifest_fields={}),
        )[1],
    )
    assert seen["facts"].agent_install_fingerprint is None


# --------------------------------------------------------------- decision guards


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        # The old contract was bytes. It is not a boot layer, and the seam
        # says so rather than treating it as one file.
        (b"#!/bin/sh\nexec /bin/true\n", TypeError),
        ("a string", TypeError),
        (None, TypeError),
        (0, TypeError),
        (BootLayer(entries=[]), TypeError),
        (
            BootLayer(
                entries=(
                    GuestFile(
                        path="sbin/init", contents=b"x", mode=0o755, uid=0, gid=0
                    ),
                )
            ),
            BootLayerError,
        ),
        (
            BootLayer(
                entries=(
                    GuestFile(
                        path="/sbin//init", contents=b"x", mode=0o755, uid=0, gid=0
                    ),
                )
            ),
            BootLayerError,
        ),
    ],
)
def test_a_boot_layer_that_cannot_be_installed_is_refused(
    stub_podman, environment_dir, tmp_path, bad, error
):
    """The refusal is the boot layer's own check, not a second one here."""
    with pytest.raises(error):
        _convert(environment_dir, tmp_path, render_boot_layer=lambda _r: bad)
    assert _published(tmp_path) == []


def test_nothing_falls_back_to_the_images_own_init(
    stub_podman, environment_dir, tmp_path
):
    """A refused decision fails the conversion; it never boots what was there."""
    with pytest.raises(TypeError, match="must be a BootLayer"):
        _convert(environment_dir, tmp_path, render_boot_layer=lambda _r: None)
    assert _published(tmp_path) == []
    assert stub_podman["ran"].get("mkfs") is None


def test_the_boot_layer_reaches_the_filesystem_builder(
    stub_podman, environment_dir, tmp_path
):
    """What the decision returned is what placement is asked to install."""
    seen = {}

    def capture_mkfs(*_args, **kwargs):
        seen["boot_layer"] = kwargs["boot_layer"]
        kwargs["dest"].write_bytes(EXT4)

    original = cella_converter.build_ext4
    cella_converter.build_ext4 = capture_mkfs
    try:
        _convert(environment_dir, tmp_path)
    finally:
        cella_converter.build_ext4 = original

    assert seen["boot_layer"] is LAYER


def test_an_identity_of_the_wrong_type_is_refused(
    stub_podman, environment_dir, tmp_path
):
    with pytest.raises(ConversionError, match="FlavorIdentity"):
        _convert(environment_dir, tmp_path, compute_flavor_identity=lambda _f: "probe")
    assert _published(tmp_path) == []


def test_an_unsafe_flavor_name_is_refused_before_anything_is_written(
    stub_podman, environment_dir, tmp_path
):
    with pytest.raises(ManifestFieldError):
        _convert(
            environment_dir,
            tmp_path,
            compute_flavor_identity=lambda _f: FlavorIdentity(
                flavor_name="../escape", manifest_fields={}
            ),
        )
    assert _published(tmp_path) == []
    # The export now runs *before* the identity decision, because the systemd
    # preparation it feeds produces a shaping fact -- the boot image id -- that
    # the decision has to be able to see. What still holds, and is what this
    # test is about, is that nothing reached the flavor store and no filesystem
    # was built.
    assert stub_podman["ran"].get("mkfs") is None


def test_an_unsafe_manifest_value_is_refused(stub_podman, environment_dir, tmp_path):
    with pytest.raises(ManifestFieldError):
        _convert(
            environment_dir,
            tmp_path,
            compute_flavor_identity=lambda _f: FlavorIdentity(
                flavor_name="probe",
                manifest_fields={"input_x": 'closes", "sha3_256": "' + "f" * 64},
            ),
        )
    assert _published(tmp_path) == []


# ----------------------------------------------------------------- input guards


def test_a_non_positive_capacity_is_refused(environment_dir, tmp_path):
    with pytest.raises(ConversionError, match="positive"):
        _convert(environment_dir, tmp_path, ext4_size_bytes=0)


def test_a_missing_environment_directory_is_refused(tmp_path):
    with pytest.raises(ConversionError, match="No environment directory"):
        _convert(tmp_path / "absent", tmp_path)


# --------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ("build", PodmanError("build failed")),
        ("export", PodmanError("export failed")),
        ("mkfs", RootfsBuildError("mkfs failed")),
    ],
)
def test_a_failed_step_publishes_nothing_and_leaves_no_staging(
    stub_podman, environment_dir, tmp_path, step, error
):
    stub_podman["fail"][step] = error
    stub_podman["install"]()

    with pytest.raises(type(error)):
        _convert(environment_dir, tmp_path)

    assert _published(tmp_path) == []
    # Not even a half-built staging tree survives.
    rootfs = tmp_path / "cella" / "rootfs"
    assert not rootfs.exists() or list(rootfs.iterdir()) == []
    # The tag this conversion created is dropped on the failure path too.
    assert stub_podman["ran"]["untag"] == 1


def test_a_failed_verification_publishes_nothing(
    monkeypatch, stub_podman, environment_dir, tmp_path
):
    def corrupt(*_args, **kwargs):
        # Write the artifact, then change it behind the manifest's back.
        kwargs["dest"].write_bytes(EXT4)
        monkeypatch.setattr(
            cella_converter,
            "sha3_256_file",
            lambda _p: hashlib.sha3_256(b"a different image").hexdigest(),
        )

    monkeypatch.setattr(cella_converter, "build_ext4", corrupt)
    with pytest.raises(FlavorIntegrityError):
        _convert(environment_dir, tmp_path)

    assert _published(tmp_path) == []


def test_the_conversion_leaves_no_temporary_build_context(
    stub_podman, environment_dir, tmp_path
):
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("titanium-cella-*"))
    _convert(environment_dir, tmp_path)
    after = set(Path(tempfile.gettempdir()).glob("titanium-cella-*"))
    assert after == before


# ------------------------------------------------------------------- cache rule


def test_an_intact_flavor_is_reused_without_rebuilding(
    stub_podman, environment_dir, tmp_path
):
    first = _convert(environment_dir, tmp_path)
    exports_before = stub_podman["ran"]["export"]

    second = _convert(environment_dir, tmp_path)

    assert second.reused is True
    assert second.sha3_256 == first.sha3_256
    assert second.flavor_dir == first.flavor_dir
    # No second mkfs: the published artifact is the answer.
    assert stub_podman["ran"]["mkfs"] == 1
    # The export does run again -- see the test below, which is where that
    # cost is stated on purpose rather than discovered here.
    assert stub_podman["ran"]["export"] > exports_before


def test_a_cache_hit_still_pays_for_the_export_and_the_systemd_probe(
    stub_podman, environment_dir, tmp_path
):
    """Deliberate, and the price of an honest cache key.

    A provisioning build resolves packages against a moving index, so recipe
    text does not determine the filesystem it produces -- only the derived
    image's id does. An identity decision that could not see that id would be
    keying the cache on something that does not pin the artifact. So the
    export and the probe happen above the cache check, and a hit costs one
    export. What a hit still avoids is the expensive half: mkfs, the manifest,
    and the publish.
    """
    _convert(environment_dir, tmp_path)
    assert stub_podman["ran"]["export"] == 1
    assert stub_podman["ran"]["mkfs"] == 1

    result = _convert(environment_dir, tmp_path)

    assert result.reused is True
    assert stub_podman["ran"]["export"] == 2
    assert stub_podman["ran"]["mkfs"] == 1


def test_a_cached_flavor_that_fails_verification_is_refused_not_replaced(
    stub_podman, environment_dir, tmp_path
):
    first = _convert(environment_dir, tmp_path)
    artifact = first.artifact_path
    tampered = b"tampered image bytes"
    artifact.write_bytes(tampered)

    with pytest.raises(FlavorIntegrityError):
        _convert(environment_dir, tmp_path)

    # Refused, and left exactly as found: the mismatch is the evidence.
    assert artifact.read_bytes() == tampered
    assert (first.flavor_dir / "golden.json").is_file()


# ---------------------------------------------------- boot-layer provenance


def _other_layer() -> BootLayer:
    """The canonical layer with one byte of one file changed."""
    first = LAYER.entries[0]
    return BootLayer(
        entries=(
            GuestFile(
                path=first.path,
                contents=first.contents + b"#",
                mode=first.mode,
                uid=first.uid,
                gid=first.gid,
            ),
            *LAYER.entries[1:],
        )
    )


def test_the_manifest_records_the_layer_that_was_placed(
    stub_podman, environment_dir, tmp_path
):
    result = _convert(environment_dir, tmp_path)
    text = (result.flavor_dir / "golden.json").read_text()
    assert manifest_field(text, "input_boot_layer") == boot_layer_digest(LAYER)


def test_an_unchanged_layer_still_hits_the_cache(
    stub_podman, environment_dir, tmp_path
):
    first = _convert(environment_dir, tmp_path)
    again = _convert(environment_dir, tmp_path)
    assert again.reused is True
    assert again.flavor_dir == first.flavor_dir


def test_a_changed_layer_cannot_be_answered_by_the_cached_artifact(
    stub_podman, environment_dir, tmp_path
):
    """The hole this closes: a new controller binary served from an old build.

    The identity function here deliberately ignores the boot layer, which is
    exactly the case that used to reuse a stale flavor silently.
    """
    first = _convert(environment_dir, tmp_path)
    published = first.artifact_path.read_bytes()

    with pytest.raises(ConversionError, match="input_boot_layer|boot layer"):
        _convert(environment_dir, tmp_path, render_boot_layer=lambda _i: _other_layer())

    # Refused, and the published artifact was left exactly as it was.
    assert first.artifact_path.read_bytes() == published


def test_an_identity_that_names_the_layer_gets_a_clean_second_flavor(
    stub_podman, environment_dir, tmp_path
):
    """An identity function that includes the digest never collides at all."""

    def identity(facts: BuildFacts) -> FlavorIdentity:
        return FlavorIdentity(
            flavor_name=f"probe-{boot_layer_digest(facts.boot_layer)[:12]}",
            manifest_fields={},
        )

    first = _convert(environment_dir, tmp_path, compute_flavor_identity=identity)
    second = _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=identity,
        render_boot_layer=lambda _i: _other_layer(),
    )
    assert first.flavor_name != second.flavor_name
    assert second.reused is False
    assert len(_published(tmp_path)) == 2


def test_an_identity_may_not_write_the_reserved_provenance_field(
    stub_podman, environment_dir, tmp_path
):
    """A supplied value could disagree with the layer actually placed."""
    with pytest.raises(ConversionError, match="input_boot_layer"):
        _convert(
            environment_dir,
            tmp_path,
            compute_flavor_identity=lambda _f: FlavorIdentity(
                flavor_name="probe", manifest_fields={"input_boot_layer": "0" * 64}
            ),
        )
    assert _published(tmp_path) == []


# ------------------------------------------------- systemd preparation wiring


@pytest.fixture
def fake_systemd_build(monkeypatch):
    """Fake the derived build. No package manager, no network, no registry."""
    calls: dict[str, list] = {"build": [], "untag": []}

    def fake_build(**kwargs):
        calls["build"].append(kwargs["build_file"].read_bytes())

    def fake_inspect(reference, **_kwargs):
        return [{"Id": "sha256:derived-boot-image", "Config": {"User": "0"}}]

    def fake_export(*, image, dest_tar, timeout_sec=None):
        write_rootfs_tar(dest_tar, BOOTABLE_TAR)

    monkeypatch.setattr(cella_systemd, "build_image", fake_build)
    monkeypatch.setattr(cella_systemd, "inspect_image", fake_inspect)
    monkeypatch.setattr(cella_systemd, "export_rootfs_tar", fake_export)
    monkeypatch.setattr(cella_systemd, "untag_image", calls["untag"].append)
    return calls


def a_plan(_info) -> SystemdProvisionPlan:
    return SystemdProvisionPlan(
        strategy="probe-systemd",
        steps=(BuildRun(argv=("apt-get", "install", "-y", "systemd")),),
    )


def test_a_non_systemd_task_image_is_provisioned(
    stub_podman, fake_systemd_build, environment_dir, tmp_path
):
    stub_podman["exports"] = NON_BOOTABLE_TAR
    seen = {}

    def planner(info):
        seen.setdefault("infos", []).append(info)
        return a_plan(info)

    facts = {}
    _convert(
        environment_dir,
        tmp_path,
        plan_systemd_provisioning=planner,
        compute_flavor_identity=lambda f: (
            facts.setdefault("f", f),
            FlavorIdentity("probe", {}),
        )[1],
    )

    assert len(seen["infos"]) == 1
    assert seen["infos"][0].systemd_bootable is False
    assert seen["infos"][0].os_id == "debian"

    recorded: BuildFacts = facts["f"]
    assert recorded.systemd_strategy == "probe-systemd"
    assert recorded.systemd_source_os.systemd_bootable is False
    assert recorded.systemd_final_os.systemd_bootable is True
    assert (
        b'RUN ["apt-get", "install", "-y", "systemd"]' in recorded.systemd_recipe_bytes
    )
    # The derived image's id, not the source's -- recipe text does not pin a
    # filesystem that a package index helped produce.
    assert recorded.boot_image_id == "sha256:derived-boot-image"
    assert recorded.image.image_id == "sha256:deadbeef"
    assert len(fake_systemd_build["untag"]) == 1


def test_the_task_image_record_is_never_replaced_by_the_derived_one(
    stub_podman, fake_systemd_build, environment_dir, tmp_path
):
    """A4/A5/A7 read what the *task* declared, not Titanium's plumbing.

    The derived image's Config.User is Titanium's own `USER 0`. If it reached
    the boot-layer decision, an install-plumbing artifact would become the
    guest's identity -- the same failure agent_user exists to prevent.
    """
    stub_podman["exports"] = NON_BOOTABLE_TAR
    seen = {}

    def capture(inputs):
        seen["inputs"] = inputs
        return LAYER

    _convert(
        environment_dir,
        tmp_path,
        plan_systemd_provisioning=a_plan,
        render_boot_layer=capture,
    )

    assert seen["inputs"].image.image_id == "sha256:deadbeef"
    assert seen["inputs"].image.config["Entrypoint"] == ["/app/run.sh"]
    assert seen["inputs"].image.config.get("User") is None


def test_the_filesystem_is_built_from_the_provisioned_tar(
    stub_podman, fake_systemd_build, environment_dir, tmp_path
):
    stub_podman["exports"] = NON_BOOTABLE_TAR
    seen = {}

    def capture_mkfs(*_args, **kwargs):
        seen["rootfs_tar"] = kwargs["rootfs_tar"]
        kwargs["dest"].write_bytes(EXT4)

    original = cella_converter.build_ext4
    cella_converter.build_ext4 = capture_mkfs
    try:
        _convert(environment_dir, tmp_path, plan_systemd_provisioning=a_plan)
    finally:
        cella_converter.build_ext4 = original

    # The derived export, not the task's own.
    assert seen["rootfs_tar"].name == "rootfs-systemd.tar"


def test_a_guest_that_cannot_be_made_bootable_publishes_nothing(
    stub_podman, fake_systemd_build, environment_dir, tmp_path, monkeypatch
):
    """No fallback: not to the image's own init, and not to no init at all."""
    stub_podman["exports"] = NON_BOOTABLE_TAR

    def useless_export(*, image, dest_tar, timeout_sec=None):
        write_rootfs_tar(dest_tar, NON_BOOTABLE_TAR)

    monkeypatch.setattr(cella_systemd, "export_rootfs_tar", useless_export)

    with pytest.raises(SystemdBootError, match="still does not boot systemd"):
        _convert(environment_dir, tmp_path, plan_systemd_provisioning=a_plan)

    assert _published(tmp_path) == []
    assert stub_podman["ran"].get("mkfs") is None


def test_a_policy_refusal_stops_the_conversion_before_any_build(
    stub_podman, fake_systemd_build, environment_dir, tmp_path
):
    """The real policy, refusing a guest it has no validated strategy for.

    The refusal has to stop the conversion rather than degrade it: there is no
    filesystem to publish if nothing can make the guest boot.
    """
    from titanium.environments.cella.systemd_boot import plan_systemd_provisioning

    stub_podman["exports"] = UNSUPPORTED_TAR
    with pytest.raises(SystemdBootError, match="No validated systemd provisioning"):
        _convert(
            environment_dir,
            tmp_path,
            plan_systemd_provisioning=plan_systemd_provisioning,
        )
    assert _published(tmp_path) == []
    assert fake_systemd_build["build"] == []
    assert stub_podman["ran"].get("mkfs") is None


# ------------------------------------------------------------------ end to end


@pytest.mark.skipif(
    shutil.which(podman_bin()) is None, reason=f"{podman_bin()} is not on PATH"
)
def test_a_real_conversion_end_to_end(tmp_path):
    """A FROM-scratch task, converted for real: no registry, no network.

    The image ships a stand-in systemd and a real ``/sbin/init`` symlink, so
    the guest is already bootable and the no-op preparation path is what runs.
    That also puts the offline probe against a genuine ``podman export`` tar
    -- directory members, ``./`` prefixes and all -- rather than a synthetic
    one. No provisioning build happens here, and none should: running a
    package manager is not something an offline test gets to do.
    """
    env = tmp_path / "task" / "environment"
    (env / "app").mkdir(parents=True)
    (env / "app" / "report.json").write_text('{"ok": true}')
    boot = env / "boot"
    (boot / "usr" / "lib" / "systemd").mkdir(parents=True)
    (boot / "usr" / "lib" / "systemd" / "systemd").write_bytes(
        b"\x7fELF pretend systemd\n"
    )
    (boot / "etc").mkdir()
    (boot / "etc" / "os-release").write_bytes(
        b'PRETTY_NAME="Probe Linux"\nID=probe\nVERSION_ID="1"\n'
    )
    (boot / "sbin").mkdir()
    (boot / "sbin" / "init").symlink_to("/usr/lib/systemd/systemd")
    (env / "Containerfile").write_text(
        "FROM scratch\n"
        "COPY --chown=4242:4242 app /app\n"
        "COPY boot/usr /usr\n"
        "COPY boot/etc /etc\n"
        "COPY boot/sbin /sbin\n"
        "WORKDIR /app\n"
    )

    facts_seen = {}

    def identity(facts: BuildFacts) -> FlavorIdentity:
        facts_seen["facts"] = facts
        return FlavorIdentity(
            flavor_name="titanium-e2e-probe",
            manifest_fields={"input_converter": facts.converter_version},
        )

    result = convert_task_to_rootfs_flavor(
        environment_dir=env,
        ext4_size_bytes=32 * 1024 * 1024,
        render_boot_layer=lambda _r: LAYER,
        compute_flavor_identity=identity,
        plan_systemd_provisioning=refuse_to_plan,
        home=tmp_path / "cella",
        pull="never",
        build_timeout_sec=600,
        built_epoch=1700000000,
    )

    assert result.reused is False
    assert result.artifact_path.stat().st_size == 32 * 1024 * 1024
    assert verify_flavor_dir(result.flavor_dir) == result.sha3_256
    assert facts_seen["facts"].source_build_file_name == "Containerfile"

    # A second run is a verified cache hit, not a rebuild.
    again = convert_task_to_rootfs_flavor(
        environment_dir=env,
        ext4_size_bytes=32 * 1024 * 1024,
        render_boot_layer=lambda _r: LAYER,
        compute_flavor_identity=identity,
        plan_systemd_provisioning=refuse_to_plan,
        home=tmp_path / "cella",
        pull="never",
        build_timeout_sec=600,
        built_epoch=1700000000,
    )
    assert again.reused is True
    assert again.sha3_256 == result.sha3_256

    # The probe read a real export tar and found the guest already bootable.
    facts: BuildFacts = facts_seen["facts"]
    assert facts.systemd_strategy == STRATEGY_ALREADY_SYSTEMD
    assert facts.systemd_source_os.os_id == "probe"
    assert facts.systemd_source_os.init_resolved_path == "/usr/lib/systemd/systemd"
    assert facts.systemd_source_os.systemd_bootable is True
    assert facts.systemd_recipe_bytes is None


# ------------------------------------------------------- builder identity fact


def test_the_builder_id_reported_is_the_builder_that_ran(
    stub_podman, environment_dir, tmp_path
):
    """The exposed fact and the container that built the ext4 cannot drift."""
    seen = {}

    def capture_mkfs(*_args, **kwargs):
        seen["builder_image"] = kwargs["builder_image"]
        kwargs["dest"].write_bytes(EXT4)

    import titanium.environments.cella.converter as mod

    monkeypatched = mod.build_ext4
    mod.build_ext4 = capture_mkfs
    try:
        facts = {}
        _convert(
            environment_dir,
            tmp_path,
            compute_flavor_identity=lambda f: (
                facts.setdefault("f", f),
                FlavorIdentity("probe", {}),
            )[1],
        )
    finally:
        mod.build_ext4 = monkeypatched

    assert seen["builder_image"] == BUILDER_ID
    assert facts["f"].rootfs_builder_image_id == BUILDER_ID


def test_different_builder_ids_are_distinguishable_facts(
    monkeypatch, stub_podman, environment_dir, tmp_path
):
    ids = []

    def capture(f):
        ids.append(f.rootfs_builder_image_id)
        return FlavorIdentity(f"probe-{len(ids)}", {})

    _convert(environment_dir, tmp_path, compute_flavor_identity=capture)
    monkeypatch.setattr(
        cella_converter, "rootfs_builder_image_id", lambda **_k: "sha256:another"
    )
    _convert(environment_dir, tmp_path, compute_flavor_identity=capture)

    assert ids == [BUILDER_ID, "sha256:another"]


def test_this_pass_does_not_decide_how_the_builder_id_is_hashed(
    stub_podman, environment_dir, tmp_path
):
    """The converter records what the identity decision asks it to, and no more.

    A builder id in the manifest, or in the flavor name, is Slice B's call.
    """
    result = _convert(environment_dir, tmp_path)
    text = (result.flavor_dir / "golden.json").read_text()
    assert BUILDER_ID not in text
    assert "builder" not in text
    assert BUILDER_ID not in result.flavor_name


@pytest.mark.parametrize("field", ["rootfs_builder_image_id", "rootfs_builder_recipe"])
def test_the_builder_facts_are_plain_strings_on_build_facts(
    stub_podman, environment_dir, tmp_path, field
):
    seen = {}
    _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=lambda f: (
            seen.setdefault("f", f),
            FlavorIdentity("probe", {}),
        )[1],
    )
    assert isinstance(getattr(seen["f"], field), str)


# ------------------------------------------------- cache identity, not just bytes


def test_another_flavors_directory_at_this_destination_is_refused(
    stub_podman, environment_dir, tmp_path
):
    """An internally consistent directory is not automatically *this* flavor.

    A flavor-B tree copied to flavor-A's path verifies against itself. Only
    checking the name it claims catches it.
    """
    import shutil as _shutil

    flavor_b = _convert(
        environment_dir,
        tmp_path,
        compute_flavor_identity=lambda _f: FlavorIdentity("flavor-b", {}),
    )
    destination = tmp_path / "cella" / "rootfs" / "flavor-a"
    _shutil.copytree(flavor_b.flavor_dir, destination)

    # Bytes and digest agree; the name does not.
    assert verify_flavor_dir(destination)

    with pytest.raises(FlavorIntegrityError, match="flavor-a"):
        _convert(
            environment_dir,
            tmp_path,
            compute_flavor_identity=lambda _f: FlavorIdentity("flavor-a", {}),
        )
    # Refused, not replaced.
    assert (destination / "rootfs.ext4").read_bytes() == EXT4
