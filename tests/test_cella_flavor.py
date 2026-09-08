"""Flavor placement, the golden.json contract, integrity, and atomic publish.

Three things are being pinned here:

* Titanium lands artifacts exactly where Cella looks for them.
* The manifest survives Cella's substring-scan reader, which escapes nothing.
* Verification is recomputation, and a mismatch refuses instead of repairing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from titanium.environments.cella.flavor import (
    MANIFEST_MODE,
    FlavorIntegrityError,
    ManifestFieldError,
    cella_home,
    manifest_field,
    publish_flavor,
    render_golden_json,
    rootfs_artifact_path,
    rootfs_flavor_dir,
    staging_flavor_dir,
    validate_flavor_name,
    verify_flavor_dir,
    write_manifest,
)

DIGEST = "0" * 64


def _flavor(
    directory: Path, payload: bytes = b"ext4 bytes", flavor: str = "probe"
) -> str:
    """Write a well-formed flavor into *directory* and return its digest."""
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "rootfs.ext4"
    artifact.write_bytes(payload)
    digest = hashlib.sha3_256(payload).hexdigest()
    write_manifest(
        directory,
        render_golden_json(
            flavor=flavor,
            sha3_256=digest,
            size_bytes=len(payload),
            built_epoch=1,
            extra_fields={},
        ),
    )
    return digest


def _rewrite_manifest(directory: Path, replacements: dict[str, str]) -> None:
    """Edit a published manifest's fields, leaving the artifact untouched."""
    manifest = directory / "golden.json"
    text = manifest.read_text()
    for old, new in replacements.items():
        text = text.replace(old, new)
    manifest.chmod(0o644)
    manifest.write_text(text)
    manifest.chmod(0o444)


# ------------------------------------------------------------------ placement


def test_cella_home_mirrors_cellas_own_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("CELLA_HOME", str(tmp_path / "explicit"))
    assert cella_home() == tmp_path / "explicit"

    monkeypatch.delenv("CELLA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cella_home() == tmp_path / "home" / ".cella"


def test_flavor_paths_match_cellas_layout(tmp_path):
    assert rootfs_flavor_dir("probe", home=tmp_path) == tmp_path / "rootfs" / "probe"
    assert (
        rootfs_artifact_path("probe", home=tmp_path)
        == tmp_path / "rootfs" / "probe" / "rootfs.ext4"
    )


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../escape", "a/b", ".hidden", "-leading", "a b", "x" * 256],
)
def test_unsafe_flavor_names_are_refused(name):
    with pytest.raises(ManifestFieldError):
        validate_flavor_name(name)


# ------------------------------------------------------------------- manifest


def test_manifest_carries_cellas_field_set_in_order():
    text = render_golden_json(
        flavor="probe",
        sha3_256=DIGEST,
        size_bytes=4096,
        built_epoch=1700000000,
        extra_fields={"input_dockerfile": DIGEST, "source_converter": "1"},
    )
    for key, expected in [
        ("axis", "rootfs"),
        ("flavor", "probe"),
        ("artifact", "rootfs.ext4"),
        ("sha3_256", DIGEST),
        ("bytes", "4096"),
        ("built_epoch", "1700000000"),
        ("input_dockerfile", DIGEST),
        ("source_converter", "1"),
    ]:
        assert manifest_field(text, key) == expected
    # Trailing comma discipline: the hand-rolled object must still parse.
    import json

    assert json.loads(text)["flavor"] == "probe"


@pytest.mark.parametrize(
    "value",
    [
        'quote"inside',
        'closes", "sha3_256": "' + "f" * 64,
        "has space",
        "brace}",
        "back\\slash",
        "new\nline",
    ],
)
def test_manifest_values_that_could_fool_cellas_reader_are_refused(value):
    with pytest.raises(ManifestFieldError):
        render_golden_json(
            flavor="probe",
            sha3_256=DIGEST,
            size_bytes=1,
            built_epoch=1,
            extra_fields={"input_x": value},
        )


def test_manifest_keys_cannot_shadow_a_stated_field():
    with pytest.raises(ManifestFieldError, match="shadow"):
        render_golden_json(
            flavor="probe",
            sha3_256=DIGEST,
            size_bytes=1,
            built_epoch=1,
            extra_fields={"sha3_256": "f" * 64},
        )


def test_manifest_refuses_a_digest_that_is_not_sha3_256_hex():
    for bad in ["", "abc", "G" * 64, "A" * 64]:
        with pytest.raises(ManifestFieldError):
            render_golden_json(
                flavor="probe",
                sha3_256=bad,
                size_bytes=1,
                built_epoch=1,
                extra_fields={},
            )


def test_manifest_lands_read_only_like_every_cella_golden(tmp_path):
    _flavor(tmp_path / "f")
    mode = (tmp_path / "f" / "golden.json").stat().st_mode & 0o777
    assert mode == MANIFEST_MODE


# ------------------------------------------------------------------ integrity


def test_verify_accepts_a_flavor_whose_artifact_matches(tmp_path):
    digest = _flavor(tmp_path / "f")
    assert verify_flavor_dir(tmp_path / "f") == digest


def test_verify_refuses_a_single_flipped_byte(tmp_path):
    _flavor(tmp_path / "f", payload=b"ext4 bytes")
    artifact = tmp_path / "f" / "rootfs.ext4"
    corrupted = bytearray(artifact.read_bytes())
    corrupted[0] ^= 0x01
    artifact.write_bytes(bytes(corrupted))
    with pytest.raises(FlavorIntegrityError, match="sha3-256"):
        verify_flavor_dir(tmp_path / "f")


def test_verify_refuses_a_truncated_artifact(tmp_path):
    _flavor(tmp_path / "f", payload=b"ext4 bytes")
    (tmp_path / "f" / "rootfs.ext4").write_bytes(b"ext4")
    with pytest.raises(FlavorIntegrityError, match="bytes"):
        verify_flavor_dir(tmp_path / "f")


def test_verify_refuses_a_missing_artifact_or_manifest(tmp_path):
    _flavor(tmp_path / "a")
    (tmp_path / "a" / "rootfs.ext4").unlink()
    with pytest.raises(FlavorIntegrityError, match="No artifact"):
        verify_flavor_dir(tmp_path / "a")

    _flavor(tmp_path / "b")
    (tmp_path / "b" / "golden.json").chmod(0o644)
    (tmp_path / "b" / "golden.json").unlink()
    with pytest.raises(FlavorIntegrityError, match="No manifest"):
        verify_flavor_dir(tmp_path / "b")


def test_verify_does_not_repair_or_delete(tmp_path):
    _flavor(tmp_path / "f", payload=b"ext4 bytes")
    artifact = tmp_path / "f" / "rootfs.ext4"
    artifact.write_bytes(b"tampered!!")
    before = artifact.read_bytes()
    with pytest.raises(FlavorIntegrityError):
        verify_flavor_dir(tmp_path / "f")
    assert artifact.read_bytes() == before
    assert (tmp_path / "f" / "golden.json").is_file()


# -------------------------------------------------------------------- publish


def test_staging_is_on_the_flavor_stores_filesystem_and_is_not_a_flavor(tmp_path):
    with staging_flavor_dir(home=tmp_path) as staging:
        assert staging.parent == tmp_path / "rootfs"
        assert staging.name.startswith(".tmp-")
        # A half-built tree must not be nameable as a flavor.
        with pytest.raises(ManifestFieldError):
            validate_flavor_name(staging.name)
        marker = staging
    assert not marker.exists()


def test_staging_is_removed_even_when_the_body_raises(tmp_path):
    captured = {}
    with pytest.raises(RuntimeError), staging_flavor_dir(home=tmp_path) as staging:
        captured["path"] = staging
        (staging / "rootfs.ext4").write_bytes(b"partial")
        raise RuntimeError("mkfs blew up")
    assert not captured["path"].exists()
    assert list((tmp_path / "rootfs").iterdir()) == []


def test_publish_moves_the_finished_tree_into_place(tmp_path):
    destination = tmp_path / "rootfs" / "probe"
    with staging_flavor_dir(home=tmp_path) as staging:
        _flavor(staging)
        publish_flavor(staging, destination)
    assert verify_flavor_dir(destination)


def test_a_second_publish_of_the_same_identity_cannot_mix_output(tmp_path):
    destination = tmp_path / "rootfs" / "probe"
    with staging_flavor_dir(home=tmp_path) as first:
        _flavor(first, payload=b"winner")
        publish_flavor(first, destination)

    winner = (destination / "rootfs.ext4").read_bytes()
    with staging_flavor_dir(home=tmp_path) as second:
        _flavor(second, payload=b"loser-with-different-length")
        with pytest.raises(FlavorIntegrityError):
            publish_flavor(second, destination)

    # The published flavor is one build's output, whole and untouched.
    assert (destination / "rootfs.ext4").read_bytes() == winner
    assert verify_flavor_dir(destination)


# ---------------------------------------------------------- flavor identity


def test_a_wrong_axis_is_refused_even_when_the_bytes_match(tmp_path):
    _flavor(tmp_path / "f")
    _rewrite_manifest(tmp_path / "f", {'"axis": "rootfs"': '"axis": "kernel"'})
    with pytest.raises(FlavorIntegrityError, match="axis"):
        verify_flavor_dir(tmp_path / "f")


def test_a_wrong_artifact_name_is_refused_even_when_the_bytes_match(tmp_path):
    _flavor(tmp_path / "f")
    _rewrite_manifest(
        tmp_path / "f", {'"artifact": "rootfs.ext4"': '"artifact": "bzImage"'}
    )
    with pytest.raises(FlavorIntegrityError, match="artifact"):
        verify_flavor_dir(tmp_path / "f")


def test_a_wrong_flavor_is_refused_when_the_expected_name_is_supplied(tmp_path):
    _flavor(tmp_path / "f", flavor="flavor-b")
    # Without the expectation, an internally consistent directory passes.
    assert verify_flavor_dir(tmp_path / "f")
    with pytest.raises(FlavorIntegrityError, match="flavor-a"):
        verify_flavor_dir(tmp_path / "f", expected_flavor="flavor-a")


def test_the_expectation_is_not_taken_from_the_directory_name(tmp_path):
    """Staging is `.tmp-<random>` while the manifest carries the final name."""
    with staging_flavor_dir(home=tmp_path) as staging:
        _flavor(staging, flavor="probe")
        assert staging.name != "probe"
        assert verify_flavor_dir(staging, expected_flavor="probe")


def test_a_correctly_named_flavor_still_verifies(tmp_path):
    digest = _flavor(tmp_path / "f", flavor="probe")
    assert verify_flavor_dir(tmp_path / "f", expected_flavor="probe") == digest
