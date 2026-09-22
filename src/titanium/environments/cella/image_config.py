"""The built image's own description of how it expects to run.

This module records and does not interpret. It answers "what did the image
declare?" and never "what should that mean at boot?" -- the second question is
the guest's OCI execution contract, and it is classified elsewhere.

What is recorded here stays the *task's* declaration. Titanium may build a
derived image on the way to a bootable filesystem (see
:mod:`titanium.environments.cella.systemd_boot`); that derived image's own
config describes Titanium's provisioning, not the task, and is never read as
runtime semantics.

The whole ``podman image inspect`` record is carried, not a chosen subset,
because the field set is not knowable in advance:

* ``HEALTHCHECK`` lands at the record's **top level** (``Healthcheck``), not
  inside ``Config``, and only for a docker-format image -- an OCI-format build
  drops it outright.
* ``SHELL`` appears in neither place under either format.
* An unfamiliar field from a newer Podman would be dropped entirely by a
  fixed-field parser.

A field that never reaches the classifier is a silently ignored runtime
semantic. Keeping the record whole, and exposing the key sets actually
present, is what makes an exhaustive classification possible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ImageRecord:
    """One ``podman image inspect`` record, verbatim.

    Attributes:
        image_id: The image's content id (``.Id``).
        digest: The manifest digest (``.Digest``), or ``None`` when absent.
        repo_digests: ``.RepoDigests``, in the order Podman reported them.
        inspect: The entire record. Read-only at the top level; nested values
            are the parsed JSON as-is, deliberately unwrapped so nothing about
            their shape is asserted here.
    """

    image_id: str
    digest: str | None
    repo_digests: tuple[str, ...]
    inspect: Mapping[str, Any]

    @property
    def config(self) -> Mapping[str, Any]:
        """The ``Config`` sub-object, validated present at parse time.

        Never a substituted empty mapping. An image whose record carries no
        ``Config`` -- or a non-object one -- is refused by
        :func:`parse_image_record`, because an empty stand-in reads exactly
        like an image that declared nothing, and would let a classification
        pass over an image nobody could actually classify.

        An empty *real* ``Config`` is a different thing and is valid: it means
        the image declared nothing.

        Not every declared runtime semantic lives here; see the module
        docstring. Read ``inspect`` for the rest.
        """
        return self.inspect["Config"]

    def config_keys(self) -> tuple[str, ...]:
        """Sorted keys actually present in ``Config``."""
        return tuple(sorted(self.config))

    def record_keys(self) -> tuple[str, ...]:
        """Sorted keys actually present at the record's top level."""
        return tuple(sorted(self.inspect))


def parse_image_record(raw: Any) -> ImageRecord:
    """Wrap ``podman image inspect`` output in an ``ImageRecord``.

    Accepts the single-element list Podman emits, or the bare object.

    Raises:
        ValueError: when the payload is not one inspect record, or when that
            record carries no object ``Config``. Guessing which of several
            records describes the image just built would attach a task's
            identity to the wrong artifact; accepting a record with no
            ``Config`` would hand the classifier an image whose declared
            semantics it cannot see.
    """
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError(
                f"Expected exactly one image inspect record, got {len(raw)}."
            )
        record = raw[0]
    else:
        record = raw

    if not isinstance(record, Mapping):
        raise TypeError(
            f"Image inspect record is {type(record).__name__}, not an object."
        )

    image_id = record.get("Id")
    if not isinstance(image_id, str) or not image_id:
        raise ValueError("Image inspect record carries no 'Id'.")

    digest = record.get("Digest")
    if digest is not None and not isinstance(digest, str):
        raise ValueError("Image inspect record's 'Digest' is not a string.")

    raw_repo_digests = record.get("RepoDigests") or []
    if not isinstance(raw_repo_digests, list) or not all(
        isinstance(item, str) for item in raw_repo_digests
    ):
        raise ValueError("Image inspect record's 'RepoDigests' is not a string list.")

    config = record.get("Config")
    if not isinstance(config, Mapping):
        found = type(config).__name__ if "Config" in record else "absent"
        raise TypeError(
            f"Image inspect record's 'Config' is {found}, not an object. The "
            "image's declared runtime semantics are unreadable; refusing "
            "rather than treating it as an image that declared nothing."
        )

    return ImageRecord(
        image_id=image_id,
        digest=digest,
        repo_digests=tuple(raw_repo_digests),
        inspect=MappingProxyType(dict(record)),
    )
