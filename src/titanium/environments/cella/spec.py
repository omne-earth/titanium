"""Task/trial -> Cella machine spec: the identity boundary.

This module answers one question and nothing else: *given a trial, which Cella
machine is it?* It is a pure projection -- no I/O, no subprocess, no clock, no
randomness -- from already-validated Titanium configuration onto the arguments
``cella create`` takes.


Ownership boundary (read this before adding a check here)
---------------------------------------------------------
Refusing task shapes Cella cannot honour is **not** this module's job. It
belongs to ``CellaEnvironment._validate_definition`` and the sibling validators
``BaseEnvironment.__init__`` already runs (``_validate_gpu_support``,
``_validate_internet_config``, ``_validate_windows_support``,
``_validate_agent_setup_options``) -- the same seam
``GVisorEnvironment._validate_definition`` uses to refuse compose tasks and
non-Linux hosts.

So ``gpus``, ``os``, ``docker_image``, ``storage_mb`` and ``allow_internet``
are *validation's* concern and must not be re-checked here. By the time this
function runs they have already been accepted.

The consequence is worth stating positively: **``build_machine_spec`` is total
over already-validated inputs.** It has exactly one failure mode -- an input
from which no valid Cella machine name can be derived at all -- and that is a
projection invariant, not a judgment about the task.


Why the name rules are what they are
------------------------------------
Cella's ``machine::valid_name`` accepts ``[a-z0-9-]``, length 1..=64, and
refuses a leading ``-`` (a machine name is a path component under
``~/.cella/machines/``, so it must not escape or confuse a shell).

Titanium must hold a *tighter* bound. ``cella inspect <vm>`` derives a
throwaway appliance named ``<vm>-inspector`` -- ten more characters -- and that
appliance is the post-mortem path a graded trial depends on. A name Cella
accepts but cannot inspect is a trial that runs and cannot be read. Hence 54,
and hence the bound lives here: this module is the only place that knows the
suffix will be appended.


Status
------
``build_machine_spec`` is implemented. Its behavioural contract is documented
below; the tests that pin it live in ``tests/test_cella_machine_spec.py``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from titanium.environments.cella.definition import CellaEnvironmentDefinition
from titanium.models.task.config import EnvironmentConfig
from titanium.models.trial.paths import TrialPaths

# Cella's own rule: `machine::valid_name` in crates/cella-libs/src/machine.rs.
CELLA_MAX_NAME_LEN = 64

# `cella inspect <vm>` boots an appliance named `<vm>-inspector`. Every machine
# Titanium creates must stay inspectable, so the effective bound is shorter.
CELLA_INSPECTOR_SUFFIX = "-inspector"
MAX_MACHINE_NAME_LEN = CELLA_MAX_NAME_LEN - len(CELLA_INSPECTOR_SUFFIX)


@dataclass(frozen=True)
class CellaMachineSpec:
    """The identity and shape of one trial's Cella machine.

    Frozen: once projected, a trial's machine identity does not change. Every
    field maps to something ``cella create`` or the surrounding CLI wrapper
    consumes; nothing here is Titanium bookkeeping.
    """

    name: str
    """The Cella machine name. ``cella create <name>`` / ``start`` / ``stop`` /
    ``destroy`` / ``inspect`` all key on it. Must satisfy
    ``^[a-z0-9][a-z0-9-]*$`` and ``len <= MAX_MACHINE_NAME_LEN``."""

    kernel_flavor: str
    """``cella create --kernel <flavor>``. Comes from the task's declaration."""

    rootfs_flavor: str
    """``cella create --rootfs <flavor>``. Comes from the task's declaration."""

    mem_mb: int | None
    """``cella create --mem-mb <N>``, or ``None``.

    ``None`` means *the task declared no memory*, and the caller must then omit
    the flag entirely so Cella applies its own default (256 MiB at
    ``machine::defaults``). Titanium does not restate that number: Cella stays
    authoritative over its own defaults, and a Titanium-side constant would
    silently pin a value that is not Titanium's to pin."""

    net: str
    """``cella create --net <value>``. Cella's vocabulary is a TAP device name,
    ``auto``, or ``none``. Deliberately a plain string, not an enum: when the
    membrane lands this becomes a tap name, which is not enumerable."""

    cella_home: Path
    """The value exported as ``CELLA_HOME`` for every ``cella`` invocation of
    this trial.

    ``CELLA_HOME`` relocates Cella's entire registry (``machine::home``), which
    is how every Cella test isolates itself. Trials run concurrently, so they
    must not share one registry."""


def build_machine_spec(
    *,
    session_id: str,
    environment_name: str,
    task_env_config: EnvironmentConfig,
    definition: CellaEnvironmentDefinition,
    trial_paths: TrialPaths,
) -> CellaMachineSpec:
    """Project a trial onto the Cella machine that will run it.

    Pure. Deterministic. No I/O.

    Args:
        session_id: The trial's session identifier, typically
            ``<task_name>__<trial_id>``. **Not name-safe**: it carries ``__``,
            dots, and case, and is unbounded in length.
        environment_name: The task name, as the factory passes it. Available
            for readability in the derived name; using it is optional.
        task_env_config: The task's ``[environment]`` table. Projection reads
            **only** ``memory_mb``. Every other field on it belongs to
            validation (see the ownership boundary above) and must not be
            re-checked here. The whole config is passed rather than the single
            field to match every other environment seam in this repo; narrowing
            the parameter later is a valid simplification.
        definition: The task's ``environment/cella.toml``. The source of both
            flavor names.
        trial_paths: The trial's output directory, which roots ``cella_home``.

    Returns:
        The machine spec. Callers treat it as the single source of truth for
        this trial's Cella identity.

    Raises:
        ValueError: only when no valid machine name can be derived from
            ``session_id`` at all (for example, an input with no usable
            characters). This is the one failure mode; task-shape refusals
            belong to ``_validate_definition``.

    Contract:
        A1  ``name`` matches ``^[a-z0-9][a-z0-9-]*$`` and
            ``len(name) <= MAX_MACHINE_NAME_LEN``, for every input --
            including inputs carrying uppercase, dots, ``__``, unicode, or
            hundreds of characters.

        A2  Deterministic. The same inputs yield the same ``name`` in every
            process, on every host, on every run. No ``uuid4``, no clock, no
            PID, no iteration order of an unordered set.

        A3  Collision-resistant. Two ``session_id`` values that differ only
            past the truncation point must still produce different names.
            ``Trial._separate_verifier_session_id`` (``trial/trial.py:464``) is
            the in-repo precedent: normalize, and when truncation is needed
            append a short digest of the **full pre-truncation** string.

        A4  ``mem_mb`` is ``task_env_config.memory_mb`` unchanged, ``None``
            included. Do not substitute a default. See the field docstring.

        A5  ``net`` is ``"none"`` in v1. Cella's membrane is not landed, so a
            sealed trial has no legitimate egress yet.

        A6  ``cella_home`` is under ``trial_paths.trial_dir`` and is never the
            operator's ``~/.cella``.

        A8  ``kernel_flavor`` and ``rootfs_flavor`` come from ``definition``
            and are never invented or defaulted here. Cella owns the supply
            chain; Titanium names a flavor, it does not choose one.

        A9  The ``MAX_MACHINE_NAME_LEN`` bound is enforced here and nowhere
            else. This module is the only place that knows ``-inspector`` will
            be appended.

        (A7 -- refusing unsupported task settings -- deliberately does not
        appear. It moved to ``CellaEnvironment._validate_definition``.)
    """

    # Part 1 - A1, A2, A4, A5, A6, A8
    candidate = session_id.lower()
    candidate = re.sub(r"[^a-z0-9-]+", "-", candidate)
    candidate = candidate.strip("-")
    if not candidate:
        raise ValueError("No valid machine name can be derived from session_id")

    # Part 2  - A3, A9
    if len(candidate) > MAX_MACHINE_NAME_LEN:
        digest = hashlib.sha1(candidate.encode()).hexdigest()[:8]
        suffix = f"-{digest}"
        prefix_len = MAX_MACHINE_NAME_LEN - len(suffix)
        candidate = f"{candidate[:prefix_len].rstrip('-')}{suffix}"

    # Part 3 - A2, A4, A5, A6, A8
    return CellaMachineSpec(
        name=candidate,
        kernel_flavor=definition.kernel,
        rootfs_flavor=definition.rootfs,
        mem_mb=task_env_config.memory_mb,
        net="none",
        cella_home=trial_paths.trial_dir / ".cella",
    )
