"""Which file describes the task image, and how its build context is staged.

Two responsibilities, both mechanical:

1. Decide which of ``Dockerfile`` / ``Containerfile`` the task ships. Exactly
   one is a task; zero or two is a refusal, never a silent pick.
2. Stage a build context whose ``FROM`` lines are fully qualified.

On (2): ``docs/environments/PODMAN.md`` section 2.1 records a residual -- a task
built directly from its own build file with no agent install bypasses
``qualify_dockerfile_froms``, so a short name still resolves through
host-global registry configuration. The Cella converter closes that residual
by always staging a prepared build file, agent install or not.

Staging also normalizes the file name to ``Dockerfile``. That is not cosmetic:
``agent_setup.write_agent_dockerfile`` reads ``source_environment_dir /
"Dockerfile"``, and normalizing here lets a ``Containerfile`` task reuse that
function verbatim instead of forking the agent-bake machinery.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from titanium.environments.agent_setup import (
    qualify_dockerfile_froms,
    write_agent_dockerfile,
)
from titanium.environments.cella.constants import (
    BUILD_FILE_NAMES,
    STAGED_BUILD_FILE_NAME,
)
from titanium.models.agent.install import AgentInstallSpec


class BuildFileError(ValueError):
    """The task's build file is missing, or there is more than one."""


@dataclass(frozen=True)
class PreparedContext:
    """A staged build context, and the bytes that shaped it.

    ``source_build_file_bytes`` and ``staged_build_file_bytes`` differ whenever
    qualification or an agent install rewrote something. Both are carried
    because which one pins the artifact is a flavor-identity decision, and that
    decision is not made here.
    """

    context_dir: Path
    build_file: Path
    source_build_file_name: str
    source_build_file_bytes: bytes
    staged_build_file_bytes: bytes
    agent_install_applied: bool
    """Whether agent install steps were appended to the staged build file.

    Load-bearing for the runtime user. ``dockerfile_install_commands`` emits a
    ``USER`` directive per install step and restores nothing afterwards, so the
    last one wins and becomes the built image's ``Config.User``. Measured on a
    real build: a task declaring ``USER 1234:5678`` inspects as ``1234:5678``
    with no agent, and as ``root`` once an install ran. When this is true, the
    image's ``Config.User`` describes install plumbing, not what the task
    declared."""


def discover_build_file(environment_dir: Path) -> Path:
    """The task's single build file.

    Raises:
        BuildFileError: when neither name is present, or both are. Both is a
            refusal rather than a precedence rule: a task carrying two build
            files has two possible meanings, and guessing one of them would
            build an image the author did not describe.
    """
    present = [
        environment_dir / name
        for name in BUILD_FILE_NAMES
        if (environment_dir / name).is_file()
    ]
    if not present:
        expected = " or ".join(BUILD_FILE_NAMES)
        raise BuildFileError(
            f"No build file in {environment_dir}: expected {expected}."
        )
    if len(present) > 1:
        found = ", ".join(str(path) for path in present)
        raise BuildFileError(
            f"More than one build file in {environment_dir}: {found}. "
            "A task must ship exactly one; refusing to guess which describes "
            "the image."
        )
    return present[0]


def prepare_build_context(
    *,
    environment_dir: Path,
    context_dir: Path,
    agent_install_spec: AgentInstallSpec | None = None,
    agent_user: str | int | None = None,
) -> PreparedContext:
    """Copy the task's environment into *context_dir* and prepare its build file.

    *context_dir* must not already exist: a leftover context could carry files
    from an earlier, different task.

    Args:
        environment_dir: The task's ``environment/`` directory.
        context_dir: Where to stage. Created by this function.
        agent_install_spec: When set, the agent's install steps are appended to
            the staged build file by ``agent_setup.write_agent_dockerfile`` --
            the same machinery every other environment bakes with.
        agent_user: The user those install steps run as, passed through
            unchanged.
    """
    if context_dir.exists():
        raise BuildFileError(
            f"Build context {context_dir} already exists; refusing to reuse a "
            "directory that may carry another task's files."
        )

    source_build_file = discover_build_file(environment_dir)
    source_bytes = source_build_file.read_bytes()

    # symlinks=True: copytree's default dereferences, which would resolve a
    # task's symlink at staging time and copy the target's *contents* into the
    # context -- including the contents of a link pointing outside
    # environment/. Preserving the link keeps the staged context a faithful
    # copy of what the task ships, and leaves resolution to the build, where a
    # link out of the context simply does not resolve.
    shutil.copytree(environment_dir, context_dir, symlinks=True)

    # Whichever name the task used, the staged context carries exactly one
    # build file, named Dockerfile.
    for name in BUILD_FILE_NAMES:
        staged_original = context_dir / name
        # is_symlink() as well as is_file(): the task's build file may itself
        # be a link, which now survives staging and would otherwise be written
        # *through* when the prepared file is emitted.
        if staged_original.is_symlink() or staged_original.is_file():
            staged_original.unlink()

    staged_build_file = context_dir / STAGED_BUILD_FILE_NAME
    staged_build_file.write_text(qualify_dockerfile_froms(source_bytes.decode("utf-8")))

    if agent_install_spec is not None:
        # write_agent_dockerfile re-reads the staged file and rewrites it in
        # place. qualify_dockerfile_froms is idempotent, so the second pass it
        # performs changes nothing that the first pass already qualified.
        write_agent_dockerfile(
            build_dir=context_dir,
            source_environment_dir=context_dir,
            prebuilt_image_name=None,
            install=agent_install_spec,
            user=agent_user,
        )

    return PreparedContext(
        context_dir=context_dir,
        build_file=staged_build_file,
        source_build_file_name=source_build_file.name,
        source_build_file_bytes=source_bytes,
        staged_build_file_bytes=staged_build_file.read_bytes(),
        agent_install_applied=agent_install_spec is not None,
    )
