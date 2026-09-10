"""Unit tests for the cella rootfs conversion modules.

Everything runs offline: no podman, no cella, no network. The podman
module is exercised against a fake recording binary; the converter
pipeline against monkeypatched seams. The systemd probe is exercised
against tars built in the test, because the module's whole claim is
that it reads archives instead of running task content.
"""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from titanium.environments.cella.boot_layer import (
    BootLayer,
    BootLayerError,
    GuestFile,
    GuestSymlink,
    boot_layer_digest,
    render_boot_layer,
    validate_boot_layer,
)
from titanium.environments.cella.buildfile import (
    BuildFileError,
    discover_build_file,
    prepare_build_context,
)
from titanium.environments.cella.converter import (
    BOOT_LAYER_FIELD,
    ConversionError,
    FlavorIdentity,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.flavor import (
    FlavorIntegrityError,
    ManifestFieldError,
    cella_home,
    manifest_field,
    publish_flavor,
    render_golden_json,
    staging_flavor_dir,
    validate_flavor_name,
    verify_flavor_dir,
    write_manifest,
)
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    PodmanError,
    build_image,
    export_rootfs_tar,
    image_exists,
    new_build_tag,
    run_podman,
)
from titanium.environments.cella.rootfs import (
    RootfsBuildError,
    build_ext4,
    sha3_256_file,
)
from titanium.environments.cella.systemd_boot import (
    STRATEGY_ALREADY_SYSTEMD,
    BuildRun,
    GuestOsInfo,
    PreparedSystemdRootfs,
    RootfsArchive,
    SystemdBootError,
    SystemdProvisionPlan,
    plan_systemd_provisioning,
    probe_rootfs_tar,
    render_derived_build_file,
    validate_provision_plan,
)

# ---------------------------------------------------------------------------
# boot_layer
# ---------------------------------------------------------------------------


def _file(path="/etc/one", mode=0o644, uid=0, gid=0, contents=b"x"):
    return GuestFile(path=path, contents=contents, mode=mode, uid=uid, gid=gid)


def _symlink(path="/etc/two", target="../one", uid=0, gid=0):
    return GuestSymlink(path=path, target=target, uid=uid, gid=gid)


def test_a_valid_layer_returns_unchanged():
    layer = BootLayer(entries=(_file(), _symlink()))
    assert validate_boot_layer(layer) is layer


def test_the_production_boot_layer_is_empty():
    assert render_boot_layer(object()) == BootLayer(entries=())


@pytest.mark.parametrize(
    "entry",
    [
        _file(path="relative/path"),
        _file(path="/has/../dotdot"),
        _file(path="/doubled//slash"),
        _file(path="/trailing/"),
        _file(path="/has/./dot"),
        _file(path="/"),
        _file(path=""),
        _file(path="/nul\x00byte"),
        _file(mode=0o10000),
        _file(mode=-1),
        _file(uid=-1),
        _file(uid=True),
        _symlink(target=""),
        _symlink(target="a\x00b"),
    ],
)
def test_uninstallable_entries_are_refused(entry):
    with pytest.raises(BootLayerError):
        validate_boot_layer(BootLayer(entries=(entry,)))


@pytest.mark.parametrize(
    "layer",
    [
        "not a layer",
        BootLayer(entries=[_file()]),  # list, not tuple
        BootLayer(entries=("not an entry",)),
        BootLayer(entries=(GuestFile("/a", "str not bytes", 0o644, 0, 0),)),
    ],
)
def test_structurally_wrong_layers_raise_type_error(layer):
    with pytest.raises(TypeError):
        validate_boot_layer(layer)


def test_two_entries_claiming_one_destination_are_refused():
    with pytest.raises(BootLayerError, match="claim"):
        validate_boot_layer(BootLayer(entries=(_file(), _file(contents=b"y"))))


def test_the_digest_is_deterministic_and_sensitive():
    layer = BootLayer(entries=(_file(), _symlink()))
    assert boot_layer_digest(layer) == boot_layer_digest(layer)
    reordered = BootLayer(entries=(_symlink(), _file()))
    assert boot_layer_digest(layer) != boot_layer_digest(reordered)
    changed = BootLayer(entries=(_file(contents=b"y"), _symlink()))
    assert boot_layer_digest(layer) != boot_layer_digest(changed)


# ---------------------------------------------------------------------------
# image_config
# ---------------------------------------------------------------------------


def _inspect_record(**overrides):
    record = {
        "Id": "sha256:abc",
        "Digest": "sha256:def",
        "RepoDigests": ["example.com/img@sha256:def"],
        "Config": {"User": "1234:5678", "WorkingDir": "/app"},
    }
    record.update(overrides)
    return record


def test_parse_accepts_the_list_podman_emits_and_the_bare_object():
    for raw in ([_inspect_record()], _inspect_record()):
        record = parse_image_record(raw)
        assert record.image_id == "sha256:abc"
        assert record.digest == "sha256:def"
        assert record.repo_digests == ("example.com/img@sha256:def",)
        assert record.config["User"] == "1234:5678"
        assert "User" in record.config_keys()
        assert "Config" in record.record_keys()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        [_inspect_record(), _inspect_record()],
        "not a record",
        _inspect_record(Id=None),
        _inspect_record(Id=""),
        _inspect_record(Digest=7),
        _inspect_record(RepoDigests="not a list"),
        _inspect_record(Config=None),
        {"Id": "sha256:abc"},  # Config absent entirely
    ],
)
def test_unusable_records_are_refused(raw):
    with pytest.raises(ValueError):
        parse_image_record(raw)


# ---------------------------------------------------------------------------
# flavor
# ---------------------------------------------------------------------------


def test_cella_home_honors_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CELLA_HOME", str(tmp_path / "elsewhere"))
    assert cella_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("CELLA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cella_home() == tmp_path / ".cella"


@pytest.mark.parametrize("name", ["task-1", "a", "Task.v2_x"])
def test_safe_flavor_names_pass(name):
    assert validate_flavor_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "-leading", ".tmp-abc", "..", "has space", "a/b", "a" * 256]
)
def test_unsafe_flavor_names_are_refused(name):
    with pytest.raises(ManifestFieldError):
        validate_flavor_name(name)


_DIGEST = "0" * 64


def test_golden_json_carries_the_cella_shape_and_extras():
    text = render_golden_json(
        flavor="task-1",
        sha3_256=_DIGEST,
        size_bytes=42,
        built_epoch=7,
        extra_fields={"input_image": "sha256:abc"},
    )
    parsed = json.loads(text)
    assert parsed == {
        "axis": "rootfs",
        "flavor": "task-1",
        "artifact": "rootfs.ext4",
        "sha3_256": _DIGEST,
        "bytes": 42,
        "built_epoch": 7,
        "input_image": "sha256:abc",
    }
    assert manifest_field(text, "flavor") == "task-1"
    assert manifest_field(text, "bytes") == "42"
    assert manifest_field(text, "absent") is None


@pytest.mark.parametrize(
    "extras",
    [
        {"sha3_256": "x"},  # shadows a stated field
        {"bad key": "v"},
        {'quote"key': "v"},
        {"key": 'value"with"quotes'},
        {"key": "value with spaces"},
        {"key": 7},
    ],
)
def test_unsafe_manifest_fields_are_refused(extras):
    with pytest.raises(ManifestFieldError):
        render_golden_json(
            flavor="task-1",
            sha3_256=_DIGEST,
            size_bytes=1,
            built_epoch=1,
            extra_fields=extras,
        )


def _publish_fixture(tmp_path, flavor="task-1", artifact=b"the-ext4"):
    flavor_dir = tmp_path / flavor
    flavor_dir.mkdir()
    (flavor_dir / "rootfs.ext4").write_bytes(artifact)
    digest = hashlib.sha3_256(artifact).hexdigest()
    write_manifest(
        flavor_dir,
        render_golden_json(
            flavor=flavor,
            sha3_256=digest,
            size_bytes=len(artifact),
            built_epoch=1,
            extra_fields={},
        ),
    )
    return flavor_dir, digest


def test_an_intact_flavor_verifies(tmp_path):
    flavor_dir, digest = _publish_fixture(tmp_path)
    assert verify_flavor_dir(flavor_dir, expected_flavor="task-1") == digest


def test_verification_refuses_the_wrong_flavor_and_wrong_bytes(tmp_path):
    flavor_dir, _ = _publish_fixture(tmp_path)
    with pytest.raises(FlavorIntegrityError, match="describes flavor"):
        verify_flavor_dir(flavor_dir, expected_flavor="other")
    (flavor_dir / "rootfs.ext4").chmod(0o644)
    (flavor_dir / "rootfs.ext4").write_bytes(b"tampered!")
    with pytest.raises(FlavorIntegrityError):
        verify_flavor_dir(flavor_dir, expected_flavor="task-1")


def test_verification_requires_both_files(tmp_path):
    flavor_dir, _ = _publish_fixture(tmp_path)
    (flavor_dir / "rootfs.ext4").unlink()
    with pytest.raises(FlavorIntegrityError, match="No artifact"):
        verify_flavor_dir(flavor_dir)


def test_staging_is_cleaned_on_failure_and_publish_is_a_rename(tmp_path):
    home = tmp_path / "home"
    with (
        pytest.raises(RuntimeError, match="boom"),
        staging_flavor_dir(home=home) as staging,
    ):
        leaked = staging
        raise RuntimeError("boom")
    assert not leaked.exists()

    with staging_flavor_dir(home=home) as staging:
        (staging / "rootfs.ext4").write_bytes(b"x")
        destination = home / "rootfs" / "task-1"
        publish_flavor(staging, destination)
    assert (destination / "rootfs.ext4").read_bytes() == b"x"

    # A competing publication fails without touching the winner.
    with staging_flavor_dir(home=home) as staging:
        (staging / "rootfs.ext4").write_bytes(b"y")
        with pytest.raises(FlavorIntegrityError, match="left untouched"):
            publish_flavor(staging, destination)
    assert (destination / "rootfs.ext4").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# systemd_boot: the archive probe
# ---------------------------------------------------------------------------


def _tar(tmp_path, entries) -> Path:
    """Build a rootfs tar from (name, kind, payload) triples."""
    path = tmp_path / "rootfs.tar"
    with tarfile.open(path, "w") as archive:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            data = None
            if kind == "file":
                data = payload if isinstance(payload, bytes) else payload.encode()
                info.size = len(data)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


_OS_RELEASE = 'ID=debian\nID_LIKE=""\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n'


def _bootable_entries():
    # A usr-merged guest: /lib is a symlink into /usr/lib, and both
    # systemd candidates resolve to the one real file.
    return [
        ("etc/os-release", "file", _OS_RELEASE),
        ("usr/lib/systemd/systemd", "file", b"ELF"),
        ("lib", "symlink", "usr/lib"),
        ("sbin/init", "symlink", "/lib/systemd/systemd"),
    ]


def test_a_bootable_filesystem_probes_bootable(tmp_path):
    info = probe_rootfs_tar(_tar(tmp_path, _bootable_entries()))
    assert info.os_id == "debian"
    assert info.version_id == "12"
    assert info.pretty_name == "Debian 12"
    assert info.init_present
    assert info.init_resolved_path == "/usr/lib/systemd/systemd"
    assert info.systemd_bootable


def test_an_initless_filesystem_probes_not_bootable(tmp_path):
    info = probe_rootfs_tar(_tar(tmp_path, [("etc/os-release", "file", "ID=debian\n")]))
    assert not info.init_present
    assert not info.systemd_bootable


def test_a_foreign_init_is_recorded_not_bootable(tmp_path):
    info = probe_rootfs_tar(
        _tar(
            tmp_path,
            [
                ("usr/lib/systemd/systemd", "file", b"ELF"),
                ("sbin/init", "symlink", "/bin/busybox"),
                ("bin/busybox", "file", b"ELF"),
            ],
        )
    )
    assert info.init_present
    assert info.init_resolved_path == "/bin/busybox"
    assert not info.systemd_bootable


def test_a_hardlinked_init_is_the_same_file(tmp_path):
    info = probe_rootfs_tar(
        _tar(
            tmp_path,
            [
                ("usr/lib/systemd/systemd", "file", b"ELF"),
                ("sbin/init", "hardlink", "usr/lib/systemd/systemd"),
            ],
        )
    )
    assert info.systemd_bootable


def test_a_link_loop_is_refused(tmp_path):
    tar = _tar(
        tmp_path,
        [("a", "symlink", "/b"), ("b", "symlink", "/a"), ("sbin", "dir", None)],
    )
    with (
        tarfile.open(tar) as archive,
        pytest.raises(SystemdBootError, match="link loop"),
    ):
        RootfsArchive(archive).resolve("/a")


def test_a_link_above_the_root_is_refused(tmp_path):
    tar = _tar(tmp_path, [("escape", "symlink", "/../outside")])
    with (
        tarfile.open(tar) as archive,
        pytest.raises(SystemdBootError, match="leaves the guest root"),
    ):
        RootfsArchive(archive).resolve("/escape")


def test_a_member_escaping_the_root_is_refused(tmp_path):
    tar = _tar(tmp_path, [("../evil", "file", b"x")])
    with pytest.raises(SystemdBootError, match="escapes"):
        probe_rootfs_tar(tar)


def test_an_ambiguous_member_is_refused_at_the_question(tmp_path):
    tar = _tar(
        tmp_path,
        [
            ("sbin/init", "file", b"x"),
            ("sbin/init", "symlink", "/bin/busybox"),
        ],
    )
    with (
        tarfile.open(tar) as archive,
        pytest.raises(SystemdBootError, match="more than once"),
    ):
        RootfsArchive(archive).resolve("/sbin/init")


@pytest.mark.parametrize(
    "text",
    [
        'ID="debian\n',  # unterminated quote
        "ID=deb\\ian\n",  # unquoted with a backslash
        'ID="deb\\qian"\n',  # undefined escape
        "ID=$(reboot)\n",  # unquoted shell
    ],
)
def test_malformed_os_release_is_refused(tmp_path, text):
    tar = _tar(tmp_path, [("etc/os-release", "file", text)])
    with pytest.raises(SystemdBootError):
        probe_rootfs_tar(tar)


# ---------------------------------------------------------------------------
# systemd_boot: the provisioning policy and plan handling
# ---------------------------------------------------------------------------


def _info(**overrides) -> GuestOsInfo:
    fields = {
        "os_id": None,
        "id_like": (),
        "version_id": None,
        "pretty_name": None,
        "init_present": False,
        "init_resolved_path": None,
        "systemd_path": None,
        "systemd_bootable": False,
    }
    fields.update(overrides)
    return GuestOsInfo(**fields)


def test_debian_and_ubuntu_families_get_the_debian_strategy():
    for identity in (
        {"os_id": "debian"},
        {"os_id": "ubuntu"},
        {"os_id": "linuxmint", "id_like": ("ubuntu", "debian")},
    ):
        plan = plan_systemd_provisioning(_info(**identity))
        assert plan.strategy == "debian-systemd"
        assert validate_provision_plan(plan) is plan


def test_the_planner_refuses_what_it_must_not_touch():
    with pytest.raises(SystemdBootError, match="already"):
        plan_systemd_provisioning(_info(systemd_bootable=True))
    with pytest.raises(SystemdBootError, match="existing non-systemd"):
        plan_systemd_provisioning(
            _info(init_present=True, init_resolved_path="/bin/busybox")
        )
    with pytest.raises(SystemdBootError, match="No validated"):
        plan_systemd_provisioning(_info(os_id="alpine"))


@pytest.mark.parametrize(
    "plan,error",
    [
        ("not a plan", TypeError),
        (
            SystemdProvisionPlan(strategy="", steps=(BuildRun(argv=("x",)),)),
            SystemdBootError,
        ),
        (SystemdProvisionPlan(strategy="s", steps=()), SystemdBootError),
        (
            SystemdProvisionPlan(strategy="s", steps=(BuildRun(argv=()),)),
            SystemdBootError,
        ),
        (SystemdProvisionPlan(strategy="s", steps=("run",)), TypeError),
        (
            SystemdProvisionPlan(strategy="s", steps=(BuildRun(argv=("a", 1)),)),
            TypeError,
        ),
        (
            SystemdProvisionPlan(strategy="s", steps=(BuildRun(argv=("a\x00b",)),)),
            SystemdBootError,
        ),
    ],
)
def test_malformed_plans_are_refused(plan, error):
    with pytest.raises(error):
        validate_provision_plan(plan)


def test_the_derived_recipe_is_deterministic_exec_form():
    plan = SystemdProvisionPlan(
        strategy="s",
        steps=(BuildRun(argv=("/bin/x", "a && b")),),
    )
    recipe = render_derived_build_file(source_tag="localhost/t:1", plan=plan)
    assert recipe == render_derived_build_file(source_tag="localhost/t:1", plan=plan)
    text = recipe.decode()
    assert "FROM localhost/t:1" in text
    assert "USER 0" in text
    # JSON exec form: the metacharacter stays data.
    assert 'RUN ["/bin/x", "a && b"]' in text


# ---------------------------------------------------------------------------
# buildfile
# ---------------------------------------------------------------------------


def test_discovery_wants_exactly_one_build_file(tmp_path):
    with pytest.raises(BuildFileError, match="No build file"):
        discover_build_file(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    assert discover_build_file(tmp_path).name == "Dockerfile"
    (tmp_path / "Containerfile").write_text("FROM alpine\n")
    with pytest.raises(BuildFileError, match="More than one"):
        discover_build_file(tmp_path)


def test_staging_normalizes_and_qualifies(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "Containerfile").write_text("FROM alpine:3.20\n")
    (environment / "asset.txt").write_text("payload")

    context = prepare_build_context(
        environment_dir=environment, context_dir=tmp_path / "context"
    )
    assert context.build_file.name == "Dockerfile"
    assert not (context.context_dir / "Containerfile").exists()
    assert (context.context_dir / "asset.txt").read_text() == "payload"
    staged = context.staged_build_file_bytes.decode()
    assert "docker.io/library/alpine:3.20" in staged
    assert context.source_build_file_name == "Containerfile"
    assert not context.agent_install_applied


def test_staging_refuses_a_leftover_context(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM alpine\n")
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    with pytest.raises(BuildFileError, match="already exists"):
        prepare_build_context(environment_dir=environment, context_dir=context_dir)


# ---------------------------------------------------------------------------
# podman, against a fake recording binary
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_podman(tmp_path, monkeypatch):
    """A stand-in podman that logs argv and follows a small script."""
    log = tmp_path / "calls.log"
    binary = tmp_path / "podman"
    binary.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {log}\n'
        'case "$1" in\n'
        "  fail) echo 'the reason' >&2; exit 7 ;;\n"
        "  create) echo container-id-123 ;;\n"
        "  export) echo -n tar-bytes ;;\n"
        "  *) echo ok ;;\n"
        "esac\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("TITANIUM_PODMAN_BIN", str(binary))
    return log


def test_run_podman_returns_stdout_and_raises_on_failure(fake_podman):
    assert run_podman(["version"]).strip() == "ok"
    with pytest.raises(PodmanError, match="the reason"):
        run_podman(["fail"])


def test_run_podman_streams_stdout_to_a_file(fake_podman, tmp_path):
    sink = tmp_path / "out.tar"
    run_podman(["export", "c1"], stdout_path=sink)
    assert sink.read_bytes() == b"tar-bytes"


def test_missing_podman_is_named(monkeypatch):
    monkeypatch.setenv("TITANIUM_PODMAN_BIN", "/no/such/podman")
    with pytest.raises(PodmanError, match="not installed"):
        run_podman(["version"])


def test_image_exists_reads_the_exit_code(fake_podman):
    assert image_exists("anything")


def test_build_image_passes_pull_joined(fake_podman, tmp_path):
    build_image(
        context_dir=tmp_path,
        build_file=tmp_path / "Dockerfile",
        tag="localhost/t:1",
        pull="never",
    )
    call = fake_podman.read_text().strip().splitlines()[-1]
    assert "--pull=never" in call.split()
    assert "-t localhost/t:1" in call


def test_export_rootfs_tar_creates_exports_and_removes(fake_podman, tmp_path):
    dest = tmp_path / "rootfs.tar"
    export_rootfs_tar(image="localhost/t:1", dest_tar=dest)
    assert dest.read_bytes() == b"tar-bytes"
    calls = fake_podman.read_text().strip().splitlines()
    assert calls[0].startswith("create localhost/t:1")
    assert calls[1].startswith("export container-id-123")
    assert calls[2].startswith("rm -f container-id-123")


def test_new_build_tags_do_not_collide():
    a, b = new_build_tag(), new_build_tag()
    assert a != b
    assert a.startswith("localhost/titanium-cella-build:")


# ---------------------------------------------------------------------------
# rootfs
# ---------------------------------------------------------------------------


def test_sha3_256_file_matches_hashlib(tmp_path):
    payload = b"some artifact bytes"
    path = tmp_path / "artifact"
    path.write_bytes(payload)
    assert sha3_256_file(path) == hashlib.sha3_256(payload).hexdigest()


def test_build_ext4_refuses_bad_inputs(tmp_path):
    tar = tmp_path / "rootfs.tar"
    tar.write_bytes(b"tar")
    dest = tmp_path / "out" / "rootfs.ext4"
    dest.parent.mkdir()

    with pytest.raises(RootfsBuildError, match="positive"):
        build_ext4(rootfs_tar=tar, boot_layer=None, size_bytes=0, dest=dest)
    with pytest.raises(RootfsBuildError, match="No exported"):
        build_ext4(
            rootfs_tar=tmp_path / "absent.tar",
            boot_layer=None,
            size_bytes=1,
            dest=dest,
        )
    dest.write_bytes(b"already")
    with pytest.raises(RootfsBuildError, match="refusing to overwrite"):
        build_ext4(rootfs_tar=tar, boot_layer=None, size_bytes=1, dest=dest)


# ---------------------------------------------------------------------------
# converter, with the podman seams monkeypatched
# ---------------------------------------------------------------------------


@pytest.fixture
def conversion(tmp_path, monkeypatch):
    """A convert_task_to_rootfs_flavor call with every podman seam faked."""
    import titanium.environments.cella.converter as converter_module

    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM debian:12-slim\n")
    home = tmp_path / "cella-home"

    monkeypatch.setattr(converter_module, "build_image", lambda **kwargs: None)
    monkeypatch.setattr(
        converter_module,
        "inspect_image",
        lambda tag, timeout_sec=None: [_inspect_record()],
    )
    monkeypatch.setattr(
        converter_module,
        "rootfs_builder_image_id",
        lambda timeout_sec=None: "sha256:builder",
    )

    def fake_export(*, image, dest_tar, timeout_sec=None):
        dest_tar.write_bytes(b"the exported tar")

    monkeypatch.setattr(converter_module, "export_rootfs_tar", fake_export)

    def fake_prepare(**kwargs):
        info = _info(os_id="debian", systemd_bootable=True, init_present=True)
        return PreparedSystemdRootfs(
            rootfs_tar=kwargs["source_rootfs_tar"],
            source_info=info,
            final_info=info,
            strategy=STRATEGY_ALREADY_SYSTEMD,
            derived=False,
            recipe_bytes=None,
            boot_image_id="sha256:abc",
        )

    monkeypatch.setattr(converter_module, "prepare_systemd_rootfs", fake_prepare)

    def fake_build_ext4(*, rootfs_tar, boot_layer, size_bytes, dest, **kwargs):
        dest.write_bytes(b"ext4 " + rootfs_tar.read_bytes())

    monkeypatch.setattr(converter_module, "build_ext4", fake_build_ext4)

    def convert(**overrides):
        arguments = {
            "environment_dir": environment,
            "ext4_size_bytes": 1 << 20,
            "render_boot_layer": lambda inputs: BootLayer(entries=()),
            "compute_flavor_identity": lambda facts: FlavorIdentity(
                flavor_name="task-1", manifest_fields={"input_image": "sha256:abc"}
            ),
            "plan_systemd_provisioning": plan_systemd_provisioning,
            "home": home,
            "built_epoch": 7,
        }
        arguments.update(overrides)
        return convert_task_to_rootfs_flavor(**arguments)

    convert.home = home
    return convert


def test_a_conversion_publishes_a_verified_flavor(conversion):
    result = conversion()
    assert not result.reused
    assert result.flavor_name == "task-1"
    assert result.artifact_path.read_bytes().startswith(b"ext4 ")
    manifest = (result.flavor_dir / "golden.json").read_text()
    assert manifest_field(manifest, "input_image") == "sha256:abc"
    assert manifest_field(manifest, BOOT_LAYER_FIELD) is not None
    assert verify_flavor_dir(result.flavor_dir, expected_flavor="task-1")


def test_an_intact_flavor_is_reused(conversion):
    first = conversion()
    second = conversion()
    assert second.reused
    assert second.sha3_256 == first.sha3_256


def test_a_cached_flavor_from_another_boot_layer_is_refused(conversion):
    conversion()
    other_layer = BootLayer(entries=(GuestFile("/etc/x", b"y", 0o644, 0, 0),))
    with pytest.raises(ConversionError, match="cannot answer for this layer"):
        conversion(render_boot_layer=lambda inputs: other_layer)


def test_identity_misbehavior_is_refused(conversion):
    with pytest.raises(ConversionError, match="must return a FlavorIdentity"):
        conversion(compute_flavor_identity=lambda facts: "task-1")
    with pytest.raises(ConversionError, match="must not set"):
        conversion(
            compute_flavor_identity=lambda facts: FlavorIdentity(
                flavor_name="task-1",
                manifest_fields={BOOT_LAYER_FIELD: "forged"},
            )
        )


def test_bad_conversion_inputs_are_refused(conversion, tmp_path):
    with pytest.raises(ConversionError, match="positive"):
        conversion(ext4_size_bytes=0)
    with pytest.raises(ConversionError, match="No environment directory"):
        conversion(environment_dir=tmp_path / "absent")
