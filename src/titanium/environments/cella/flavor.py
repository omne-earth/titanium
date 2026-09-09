"""Where a converted rootfs lands, and what proves it is intact.

Cella resolves a flavor as ``$CELLA_HOME/rootfs/<flavor>/rootfs.ext4`` with
``golden.json`` beside it (``cella_libs::machine::rootfs_path``,
``cella_libs::golden``). This module mirrors that layout exactly and adds
nothing to it -- Cella boots flavors, and the converter writes two files.

Two facts about the current Cella tree shape everything here.

**Nothing on the boot path checks the digest.** ``machine::create`` verifies
only that the artifact file exists; it never reads ``golden.json``. And
``doctor::verify`` walks a hardcoded list of Cella's own flavors, so
``cella doctor verify rootfs <a-titanium-flavor>`` matches nothing, prints
"nothing to verify", and exits 0. A zero exit from that verb is not integrity
evidence for a flavor Titanium produced. Titanium therefore recomputes the
digest itself, here. This is a defensive gate against a gap on the Cella side,
not a claim that the gap is closed.

**The manifest reader is a substring scan.** ``cella_libs::golden::field``
finds ``"<key>":`` anywhere in the text and takes the next quoted run, and the
writer escapes nothing. A value carrying a quote could therefore close its own
string, and a value carrying ``"sha3_256": "..."`` could be found *before* the
real field. Every key and value written here passes a character allowlist for
that reason.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from titanium.environments.cella.rootfs import sha3_256_file

# Cella's names, not Titanium's.
ROOTFS_AXIS = "rootfs"
ROOTFS_ARTIFACT_NAME = "rootfs.ext4"
MANIFEST_NAME = "golden.json"

# Cella writes its manifests read-only: "the manifest states what was built,
# and nothing edits that statement" (cella_libs::golden).
MANIFEST_MODE = 0o444

# Staging directories share the flavor store's filesystem so publication is a
# rename. The leading dot keeps a half-built tree from reading as a flavor.
_STAGING_PREFIX = ".tmp-"

# A flavor name is a path component and a manifest value at once. This is the
# floor both roles require -- it constrains the name, it does not choose one.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_FIELD_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_FIELD_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]*$")

# NAME_MAX. Not a policy, the filesystem's own limit.
_NAME_MAX = 255


class ManifestFieldError(ValueError):
    """A manifest key or value cannot be written safely."""


class FlavorIntegrityError(RuntimeError):
    """A flavor's artifact does not match the manifest beside it."""


def cella_home() -> Path:
    """Cella's artifact home, resolved exactly as Cella resolves it.

    Mirrors ``cella_libs::machine::home``: ``$CELLA_HOME`` when set, else
    ``$HOME/.cella``, else ``./.cella``. Titanium must land artifacts where
    Cella will look for them, so this is the contract rather than a knob --
    there is no Titanium-specific override.
    """
    override = os.environ.get("CELLA_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("HOME", ".")) / ".cella"


def validate_flavor_name(flavor: str) -> str:
    """Reject a flavor name that cannot be a directory or a manifest value."""
    if not flavor or len(flavor.encode()) > _NAME_MAX:
        raise ManifestFieldError(
            f"Flavor name must be 1..{_NAME_MAX} bytes, got {len(flavor.encode())}."
        )
    if not _SAFE_NAME.match(flavor):
        raise ManifestFieldError(
            f"Flavor name {flavor!r} must start alphanumeric and hold only "
            "letters, digits, '.', '_', and '-'."
        )
    if flavor in (".", "..") or flavor.startswith(_STAGING_PREFIX):
        raise ManifestFieldError(f"Flavor name {flavor!r} is reserved.")
    return flavor


def rootfs_flavor_dir(flavor: str, *, home: Path | None = None) -> Path:
    return (home or cella_home()) / ROOTFS_AXIS / validate_flavor_name(flavor)


def rootfs_artifact_path(flavor: str, *, home: Path | None = None) -> Path:
    return rootfs_flavor_dir(flavor, home=home) / ROOTFS_ARTIFACT_NAME


def manifest_path(flavor_dir: Path) -> Path:
    return flavor_dir / MANIFEST_NAME


def render_golden_json(
    *,
    flavor: str,
    sha3_256: str,
    size_bytes: int,
    built_epoch: int,
    extra_fields: Mapping[str, str],
) -> str:
    """A ``golden.json`` in Cella's own shape.

    The first six fields, in order, are what every Cella golden carries.
    ``extra_fields`` are the ``source_*`` / ``input_*`` keys that record what
    shaped the artifact; which inputs those are is a flavor-identity decision
    and is not made here.

    Raises:
        ManifestFieldError: on any key or value the Cella reader could not
            recover unambiguously. See the module docstring.
    """
    validate_flavor_name(flavor)
    if not re.fullmatch(r"[0-9a-f]{64}", sha3_256):
        raise ManifestFieldError("sha3_256 must be 64 lowercase hex characters.")
    if size_bytes < 0 or built_epoch < 0:
        raise ManifestFieldError("Manifest byte count and epoch must not be negative.")

    lines = [
        "{",
        f'  "axis": "{ROOTFS_AXIS}",',
        f'  "flavor": "{flavor}",',
        f'  "artifact": "{ROOTFS_ARTIFACT_NAME}",',
        f'  "sha3_256": "{sha3_256}",',
        f'  "bytes": {size_bytes},',
        f'  "built_epoch": {built_epoch},',
    ]
    for key, value in extra_fields.items():
        if not _SAFE_FIELD_KEY.match(key):
            raise ManifestFieldError(f"Manifest key {key!r} is not safe to write.")
        if key in ("axis", "flavor", "artifact", "sha3_256", "bytes", "built_epoch"):
            raise ManifestFieldError(
                f"Manifest key {key!r} would shadow a field the manifest already "
                "states."
            )
        if not isinstance(value, str) or not _SAFE_FIELD_VALUE.match(value):
            raise ManifestFieldError(
                f"Manifest value for {key!r} is not safe to write: Cella's reader "
                "scans for the key and takes the next quoted run, so a value "
                "carrying a quote or a brace could be misread as another field."
            )
        lines.append(f'  "{key}": "{value}",')

    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines) + "\n"


def manifest_field(text: str, key: str) -> str | None:
    """Read one field the way Cella reads it.

    Deliberately the same substring scan as ``cella_libs::golden::field``, so
    Titanium verifies against the value Cella would actually recover rather
    than against a stricter parse of its own.
    """
    marker = f'"{key}":'
    index = text.find(marker)
    if index < 0:
        return None
    rest = text[index + len(marker) :].lstrip()
    if rest.startswith('"'):
        remainder = rest[1:]
        end = remainder.find('"')
        return remainder if end < 0 else remainder[:end]
    return re.split(r"[,}\s]", rest, maxsplit=1)[0] or None


def write_manifest(flavor_dir: Path, text: str) -> Path:
    """Write ``golden.json`` read-only beside the artifact."""
    path = manifest_path(flavor_dir)
    path.write_text(text)
    path.chmod(MANIFEST_MODE)
    return path


def verify_flavor_dir(flavor_dir: Path, *, expected_flavor: str | None = None) -> str:
    """Check that this directory holds the flavor it claims, intact.

    Digest agreement alone is not enough. A manifest and an artifact can match
    each other perfectly and still describe a *different* flavor -- a directory
    copied from elsewhere verifies against itself, and would otherwise be
    served as a cache hit for whatever name it was placed under.

    Args:
        flavor_dir: The directory to check.
        expected_flavor: When given, the flavor the manifest must name.
            Deliberately a parameter rather than ``flavor_dir.name``: during
            staging the directory is ``.tmp-<random>`` while the manifest
            already carries the final name, so the directory name is not the
            answer at the site that most needs the check.

    Returns the verified digest.

    Raises:
        FlavorIntegrityError: when the artifact, the manifest, or any field
            they must agree on is missing or wrong. Nothing here repairs,
            rewrites, or deletes: a flavor that fails this check is evidence
            of a problem, and quietly replacing it would destroy the evidence
            and hide the cause.
    """
    artifact = flavor_dir / ROOTFS_ARTIFACT_NAME
    manifest = manifest_path(flavor_dir)
    if not artifact.is_file():
        raise FlavorIntegrityError(f"No artifact at {artifact}.")
    if not manifest.is_file():
        raise FlavorIntegrityError(f"No manifest at {manifest}.")

    text = manifest.read_text()

    # What the manifest claims to be, before what it claims about bytes. These
    # are cheap, and a mismatch here means the digest check below would be
    # answering the wrong question.
    for key, expected in (
        ("axis", ROOTFS_AXIS),
        ("artifact", ROOTFS_ARTIFACT_NAME),
    ):
        recorded = manifest_field(text, key)
        if recorded != expected:
            raise FlavorIntegrityError(
                f"{manifest} records {key}={recorded!r}; this store holds "
                f"{key}={expected!r}."
            )
    if expected_flavor is not None:
        recorded_flavor = manifest_field(text, "flavor")
        if recorded_flavor != expected_flavor:
            raise FlavorIntegrityError(
                f"{manifest} describes flavor {recorded_flavor!r}, not "
                f"{expected_flavor!r}. The artifact may be intact and still be "
                "the wrong artifact."
            )

    recorded_digest = manifest_field(text, "sha3_256")
    if not recorded_digest:
        raise FlavorIntegrityError(f"{manifest} records no sha3_256.")
    recorded_bytes = manifest_field(text, "bytes")
    if recorded_bytes is None:
        raise FlavorIntegrityError(f"{manifest} records no byte count.")

    actual_size = artifact.stat().st_size
    if recorded_bytes != str(actual_size):
        raise FlavorIntegrityError(
            f"{artifact} is {actual_size} bytes; {manifest} records {recorded_bytes}."
        )

    actual_digest = sha3_256_file(artifact)
    if actual_digest != recorded_digest:
        raise FlavorIntegrityError(
            f"{artifact} has sha3-256 {actual_digest[:16]}..; {manifest} records "
            f"{recorded_digest[:16]}.."
        )
    return actual_digest


@contextmanager
def staging_flavor_dir(*, home: Path | None = None) -> Iterator[Path]:
    """A private directory on the flavor store's own filesystem.

    Same filesystem so publication is a rename rather than a copy; dot-prefixed
    and randomly named so a half-built tree is not mistakeable for a flavor.
    Removed on every exit path, so a failed conversion publishes nothing.
    """
    import shutil

    root = (home or cella_home()) / ROOTFS_AXIS
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        yield staging
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def publish_flavor(staging: Path, destination: Path) -> None:
    """Move a finished staging directory into place, atomically.

    ``rename`` is the whole concurrency story, and it is worth being exact
    about what that buys. Three things, and only these three:

    1. Each conversion builds in its own private directory, so two builds
       cannot merge into one published directory.
    2. One completed directory wins publication, atomically.
    3. A competing publication fails without modifying the winner -- ``rename``
       onto a non-empty directory is an error, not a merge.

    What it does **not** buy: any claim that two builds of the same identity
    produce the same bytes. They do not. ``mkfs.ext4`` generates a fresh
    filesystem UUID and timestamps on every run, so identical converter inputs
    yield different ``rootfs.ext4`` digests -- measured, two runs of one tar at
    one capacity giving two different sha3-256 values. Until the filesystem
    build is reproducible, "same identity" means "same inputs", never "same
    artifact".

    A lock would add a file and a failure mode and would change none of the
    three guarantees above.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(staging, destination)
    except OSError as exc:
        raise FlavorIntegrityError(
            f"Could not publish {destination}: {exc}. A flavor already at that "
            "path is left untouched."
        ) from exc


__all__ = [
    "MANIFEST_MODE",
    "MANIFEST_NAME",
    "ROOTFS_ARTIFACT_NAME",
    "ROOTFS_AXIS",
    "FlavorIntegrityError",
    "ManifestFieldError",
    "cella_home",
    "manifest_field",
    "manifest_path",
    "publish_flavor",
    "render_golden_json",
    "rootfs_artifact_path",
    "rootfs_flavor_dir",
    "staging_flavor_dir",
    "validate_flavor_name",
    "verify_flavor_dir",
    "write_manifest",
]
