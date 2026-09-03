"""The task-side Cella environment declaration.

A task opts into Cella with an ``environment/cella.toml`` beside the
``Dockerfile`` a task would carry for an OCI environment -- the same directory
``BaseEnvironment._validate_definition`` already receives as
``environment_dir``::

    [cella]
    kernel = "canonical"
    rootfs = "titanium-swe-basic"
    rootfs_digest = "sha3-256:9f2c..."   # optional

The flavors are *names Titanium consumes and Cella owns*: ``cella build
kernel|rootfs <flavor>`` produces them, ``cella doctor verify`` pins them, and
Titanium never builds one. That is the whole point of the ownership split --
Titanium's TCB stays narrow because it names a string and checks a digest.

Scope note: this module is deliberately only the model. Reading and validating
``environment/cella.toml`` belongs to ``CellaEnvironment._validate_definition``
and is not scaffolded yet; neither is flavor-name shape validation (a flavor is
a single path component under ``~/.cella/<axis>/``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CellaEnvironmentDefinition(BaseModel):
    """What a task declares about the Cella machine it needs.

    Both flavors are required and neither has a default. Cella's own defaults
    (``kernel = "canonical"``, ``rootfs = "cella"``, see ``cella-libs``
    ``machine::defaults``) are Cella's to change; a Titanium-side default would
    silently pin a value Titanium does not own.
    """

    kernel: str = Field(
        description=(
            "Golden kernel flavor, e.g. 'canonical'. Resolved by Cella at "
            "'~/.cella/kernel/<flavor>/bzImage'."
        ),
    )
    rootfs: str = Field(
        description=(
            "Golden rootfs flavor carrying this task's content. Resolved by "
            "Cella at '~/.cella/rootfs/<flavor>/rootfs.ext4'."
        ),
    )
    rootfs_digest: str | None = Field(
        default=None,
        description=(
            "Optional sha3-256 pin for the rootfs flavor, checked against "
            "'cella doctor verify rootfs <flavor>'. None means unpinned."
        ),
    )
