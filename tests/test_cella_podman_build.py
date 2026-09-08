"""Podman plumbing: how the commands are built, and what export preserves.

The end-to-end half builds ``FROM scratch`` images, so it needs no registry
and no network: it exercises the real build/inspect/create/export path against
a fixture whose ownership and modes are known exactly.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from titanium.environments.cella import podman as cella_podman
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    IMAGE_FORMAT,
    PodmanError,
    build_image,
    export_rootfs_tar,
    image_exists,
    inspect_image,
    new_build_tag,
    podman_bin,
    run_podman,
    untag_image,
)

requires_podman = pytest.mark.skipif(
    shutil.which(podman_bin()) is None,
    reason=f"{podman_bin()} is not on PATH",
)


class _Recorder:
    """Stands in for subprocess.run and records the argv it was handed."""

    def __init__(self, returncode: int = 0, stdout: bytes = b""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, b"")


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(cella_podman, "require_podman", lambda: "/usr/bin/podman")
    monkeypatch.setattr(cella_podman.subprocess, "run", rec)
    return rec


# --------------------------------------------------------- command construction


def test_build_names_podman_never_docker_and_never_sudo(recorder, tmp_path):
    build_image(context_dir=tmp_path, build_file=tmp_path / "Dockerfile", tag="t:1")
    argv = recorder.calls[0]
    # `--format docker` names a manifest schema, not an engine; what must not
    # appear is the docker *binary*, its socket, or any elevation.
    assert Path(argv[0]).name == "podman"
    assert not any(token.endswith("docker.sock") for token in argv)
    assert "sudo" not in argv
    assert "--privileged" not in argv


def test_build_pins_the_docker_manifest_format(recorder, tmp_path):
    # OCI format drops HEALTHCHECK from the inspect record entirely; a field
    # the shim's classifier never sees is a field silently ignored.
    build_image(context_dir=tmp_path, build_file=tmp_path / "Dockerfile", tag="t:1")
    argv = recorder.calls[0]
    assert argv[argv.index("--format") + 1] == IMAGE_FORMAT == "docker"


def test_build_omits_pull_policy_unless_asked(recorder, tmp_path):
    build_image(context_dir=tmp_path, build_file=tmp_path / "Dockerfile", tag="t:1")
    assert not any(a.startswith("--pull") for a in recorder.calls[0])

    recorder.calls.clear()
    build_image(
        context_dir=tmp_path,
        build_file=tmp_path / "Dockerfile",
        tag="t:1",
        pull="never",
    )
    # Joined form: podman parses a separated value as a positional argument.
    assert "--pull=never" in recorder.calls[0]


def test_a_nonzero_exit_raises_rather_than_returning(monkeypatch, tmp_path):
    monkeypatch.setattr(cella_podman, "require_podman", lambda: "/usr/bin/podman")
    monkeypatch.setattr(
        cella_podman.subprocess,
        "run",
        _Recorder(returncode=125, stdout=b"boom"),
    )
    with pytest.raises(PodmanError, match="125"):
        run_podman(["build", "."])


def test_cleanup_untags_and_never_removes_an_image(recorder):
    untag_image("localhost/titanium-cella-build:abc")
    argv = recorder.calls[0]
    assert argv[1:] == ["image", "untag", "localhost/titanium-cella-build:abc"]
    assert "rm" not in argv


def test_build_tags_are_unique_per_conversion():
    assert new_build_tag() != new_build_tag()
    assert new_build_tag().startswith("localhost/")


def test_missing_binary_is_named_not_worked_around(monkeypatch):
    monkeypatch.setattr(cella_podman.shutil, "which", lambda _binary: None)
    with pytest.raises(PodmanError, match="not on PATH"):
        run_podman(["info"])


# ------------------------------------------------------------------ end to end


@pytest.fixture
def scratch_image(tmp_path):
    """A FROM-scratch image with known ownership and modes. No registry."""
    context = tmp_path / "ctx"
    (context / "app").mkdir(parents=True)
    (context / "app" / "data.txt").write_text("payload")
    script = context / "app" / "run.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    (context / "Dockerfile").write_text(
        "FROM scratch\n"
        "COPY --chown=1234:5678 app /app\n"
        "ENV FOO=bar\n"
        "WORKDIR /app\n"
        "USER 1234:5678\n"
        'ENTRYPOINT ["/app/run.sh"]\n'
        "STOPSIGNAL SIGTERM\n"
        "HEALTHCHECK --interval=5s CMD /app/run.sh\n"
    )
    tag = new_build_tag("titanium-cella-test")
    build_image(
        context_dir=context,
        build_file=context / "Dockerfile",
        tag=tag,
        pull="never",
        timeout_sec=300,
    )
    try:
        yield tag
    finally:
        untag_image(tag)


@requires_podman
def test_inspect_returns_one_parsable_record(scratch_image):
    record = parse_image_record(inspect_image(scratch_image, timeout_sec=60))
    assert record.image_id
    assert record.config["User"] == "1234:5678"
    assert record.config["WorkingDir"] == "/app"
    assert record.config["Entrypoint"] == ["/app/run.sh"]
    assert "FOO=bar" in record.config["Env"]


@requires_podman
def test_healthcheck_survives_because_the_build_format_is_docker(scratch_image):
    record = parse_image_record(inspect_image(scratch_image, timeout_sec=60))
    # Top level, not Config -- the reason image_config.py carries the whole
    # record instead of a chosen subset.
    assert "Healthcheck" in record.record_keys()
    assert record.inspect["Healthcheck"]["Test"] == ["CMD-SHELL", "/app/run.sh"]


@requires_podman
def test_export_preserves_numeric_ownership_and_modes(scratch_image, tmp_path):
    tar_path = tmp_path / "rootfs.tar"
    export_rootfs_tar(image=scratch_image, dest_tar=tar_path, timeout_sec=300)

    with tarfile.open(tar_path) as archive:
        members = {member.name.lstrip("./"): member for member in archive}

    data = members["app/data.txt"]
    script = members["app/run.sh"]
    # The image's numbers, not the invoking user's.
    assert (data.uid, data.gid) == (1234, 5678)
    assert (script.uid, script.gid) == (1234, 5678)
    assert script.mode & 0o111, "executable bit must survive the export"
    assert not data.mode & 0o111


def _containers_of(image: str) -> set[str]:
    """Containers created from *image*.

    Scoped to the fixture's own image rather than a global `podman ps -aq`
    snapshot: this host runs Cella's own toolbox container, and any concurrent
    podman activity would make a global diff report a leak that is not ours.
    """
    return set(
        subprocess.run(
            [podman_bin(), "ps", "-a", "--filter", f"ancestor={image}", "-q"],
            capture_output=True,
            text=True,
        ).stdout.split()
    )


@requires_podman
def test_export_removes_its_temporary_container(scratch_image, tmp_path):
    before = _containers_of(scratch_image)
    export_rootfs_tar(
        image=scratch_image, dest_tar=tmp_path / "rootfs.tar", timeout_sec=300
    )
    assert _containers_of(scratch_image) == before == set()


@requires_podman
def test_export_removes_its_container_even_when_export_fails(
    monkeypatch, scratch_image, tmp_path
):
    before = _containers_of(scratch_image)

    real_run = cella_podman.run_podman

    def fail_on_export(args, **kwargs):
        if args and args[0] == "export":
            raise PodmanError("export failed")
        return real_run(args, **kwargs)

    monkeypatch.setattr(cella_podman, "run_podman", fail_on_export)
    with pytest.raises(PodmanError, match="export failed"):
        export_rootfs_tar(
            image=scratch_image,
            dest_tar=tmp_path / "rootfs.tar",
            timeout_sec=300,
        )

    assert _containers_of(scratch_image) == before == set()


@requires_podman
def test_untag_leaves_the_image_reachable_by_id(scratch_image):
    record = parse_image_record(inspect_image(scratch_image, timeout_sec=60))
    untag_image(scratch_image)
    assert not image_exists(scratch_image)
    # The layers other builds may cache on are not collateral.
    assert image_exists(record.image_id)
