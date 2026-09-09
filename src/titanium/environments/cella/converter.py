"""One task's build file becomes one Cella rootfs flavor.

The whole conversion, in order:

    discover the build file        Dockerfile xor Containerfile
    stage the build context        FROM lines qualified, agent steps baked
    podman build                   build time, and only build time
    podman image inspect           the image's declared runtime semantics
    podman export                  numeric ownership preserved
    systemd preparation            probe offline; provision only if needed
  * render the boot layer          <- decided elsewhere (RenderBootLayer)
  * compute the flavor identity    <- decided elsewhere (ComputeFlavorIdentity)
    cache check                    a published flavor is reused only if intact
    mkfs.ext4                      in a container: no root, no loop device
    write golden.json              Cella's shape, read-only
    verify independently           re-read the manifest, re-hash the artifact
    publish                        atomic rename into the flavor store

The export and the systemd preparation sit *above* the cache check, and that
ordering is deliberate. A provisioning build resolves packages against a moving
index, so the recipe text does not determine the filesystem it produces; only
the derived image's own id does. An identity decision that could not see that
id would be keying a cache on something that does not pin the artifact. Paying
for an export on a cache hit is the cost of the identity decision seeing every
fact that shaped the thing being cached. This ordering can be optimized once
identity semantics are explicit; it cannot be optimized by hiding the fact.

The two starred steps are the load-bearing decisions and arrive as callables.
Passing them in rather than importing them keeps the seam impossible to close
by accident: this module cannot acquire an opinion about what boots or about
what a cache key contains, because it never names one.

Nothing here starts a machine. Cella's runtime verbs -- create, start, freeze,
inspect -- are not called, imported, or referenced.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from titanium.environments.cella.boot_layer import (
    BootLayer,
    BootLayerInputs,
    boot_layer_digest,
    validate_boot_layer,
)
from titanium.environments.cella.buildfile import (
    PreparedContext,
    prepare_build_context,
)
from titanium.environments.cella.flavor import (
    MANIFEST_NAME,
    ROOTFS_ARTIFACT_NAME,
    manifest_field,
    publish_flavor,
    render_golden_json,
    rootfs_flavor_dir,
    staging_flavor_dir,
    validate_flavor_name,
    verify_flavor_dir,
    write_manifest,
)
from titanium.environments.cella.image_config import ImageRecord, parse_image_record
from titanium.environments.cella.podman import (
    build_image,
    export_rootfs_tar,
    inspect_image,
    new_build_tag,
    untag_image,
)
from titanium.environments.cella.rootfs import (
    ROOTFS_BUILDER_CONTAINERFILE,
    build_ext4,
    rootfs_builder_image_id,
    sha3_256_file,
)
from titanium.environments.cella.systemd_boot import (
    GuestOsInfo,
    PlanSystemdProvisioning,
    PreparedSystemdRootfs,
    prepare_systemd_rootfs,
)
from titanium.models.agent.install import AgentInstallSpec

# Bumped when a change to this pipeline could produce a different filesystem
# from identical task inputs. Offered to the identity function as a fact;
# whether it belongs in the cache key is that function's decision.
CONVERTER_VERSION = "1"

#: The manifest field recording which boot layer produced an artifact.
#:
#: Written on every publish and checked on every cache hit. The flavor name is
#: the identity decision's to choose, and a decision that ignored the boot
#: layer would let a changed controller binary answer from a directory built
#: before it existed. This field closes that hole without taking the naming
#: decision away: a hit whose layer does not match is refused rather than
#: served.
BOOT_LAYER_FIELD = "input_boot_layer"


class ConversionError(RuntimeError):
    """The conversion could not complete."""


@dataclass(frozen=True)
class BuildFacts:
    """Everything the conversion knows that could have shaped the artifact.

    This is a record of what is *available*, not a claim about what belongs in
    a cache key. Selecting from these -- and deciding what a changed value
    should do -- is the flavor-identity decision.

    Attributes:
        source_build_file_name: ``Dockerfile`` or ``Containerfile``, as the
            task shipped it.
        source_build_file_bytes: The task's build file, byte for byte.
        staged_build_file_bytes: What was actually built: the source with
            ``FROM`` lines qualified, plus any agent install steps. Differs
            from the source whenever either rewrote something.
        image: The whole ``podman image inspect`` record, carrying the image
            id, the manifest digest, and every declared field.
        boot_layer: The files and symlinks Titanium contributes to the
            guest filesystem.
        systemd_strategy: How the filesystem came to boot systemd --
            ``"already-systemd"`` when the task's own image did, otherwise the
            strategy label the provisioning policy returned.
        systemd_source_os: What the task's exported filesystem said about
            itself, before anything was done to it.
        systemd_final_os: What the filesystem actually being shipped says. A
            measurement of the final tar, never a prediction.
        systemd_recipe_bytes: The derived build file, byte for byte, or
            ``None`` when no provisioning ran. It explains the artifact but
            does not pin it -- see ``boot_image_id``.
        boot_image_id: The content id of the image the final filesystem came
            from. The source image's id when nothing was provisioned, the
            derived image's otherwise. This is the fact that distinguishes two
            runs of one recipe: ``apt-get install systemd`` resolves against a
            moving index, so identical recipe text can produce different
            filesystems on different days.
        agent_install_fingerprint: ``AgentInstallSpec.fingerprint()`` when an
            agent was baked in, else ``None``. Note what a baked agent means:
            anything in the image is readable by the guest, so a credential
            that entered at build time is visible to whatever runs there.
        agent_user: Titanium's explicit runtime-user decision. See
            :class:`~titanium.environments.cella.boot_layer.BootLayerInputs`.
        agent_install_applied: Whether install steps rewrote the image, and so
            whether ``image.config["User"]`` is install plumbing.
        ext4_size_bytes: The filesystem capacity the caller asked for.
        converter_version: ``CONVERTER_VERSION``.
        rootfs_builder_image_id: The content id of the builder image that
            actually produces the filesystem. The tag and the recipe do not
            pin it -- ``apk add e2fsprogs`` resolves against a moving index --
            so this is the fact that distinguishes two hosts' builders.
        rootfs_builder_recipe: The builder's Containerfile text, so a builder
            can be explained and rebuilt from what the manifest records.

    Note what these last two do **not** establish: ``mkfs.ext4`` writes a fresh
    filesystem UUID and timestamps every run, so even one builder and one tar
    produce different bytes each time. Same builder means same tool, never same
    artifact.
    """

    source_build_file_name: str
    source_build_file_bytes: bytes
    staged_build_file_bytes: bytes
    image: ImageRecord
    boot_layer: BootLayer
    systemd_strategy: str
    systemd_source_os: GuestOsInfo
    systemd_final_os: GuestOsInfo
    systemd_recipe_bytes: bytes | None
    boot_image_id: str
    agent_install_fingerprint: str | None
    agent_user: str | int | None
    agent_install_applied: bool
    ext4_size_bytes: int
    converter_version: str
    rootfs_builder_image_id: str
    rootfs_builder_recipe: str


@dataclass(frozen=True)
class FlavorIdentity:
    """What the identity decision returns.

    Attributes:
        flavor_name: The directory under ``$CELLA_HOME/rootfs/``, and the
            cache key by construction -- an existing directory of this name is
            a hit.
        manifest_fields: Extra ``golden.json`` fields recording what shaped the
            artifact, conventionally ``input_*`` and ``source_*``. Written
            verbatim after a safety check; they are a record, not an input to
            anything the converter does.
    """

    flavor_name: str
    manifest_fields: Mapping[str, str]


@dataclass(frozen=True)
class ConversionResult:
    flavor_name: str
    flavor_dir: Path
    artifact_path: Path
    sha3_256: str
    size_bytes: int
    reused: bool


#: Given the built image's declared semantics and Titanium's explicit
#: runtime-user decision, return the boot layer Titanium contributes. Every
#: field the image declares is reachable through the record; the contract is
#: that each one is honored, refused by name, or recorded as inapplicable, and
#: that none is silently dropped.
RenderBootLayer = Callable[[BootLayerInputs], BootLayer]

#: Given everything that could have shaped the artifact, return the flavor's
#: identity. Pure: same facts, same identity, in every process and on every
#: host.
ComputeFlavorIdentity = Callable[[BuildFacts], FlavorIdentity]


def convert_task_to_rootfs_flavor(
    *,
    environment_dir: Path,
    ext4_size_bytes: int,
    render_boot_layer: RenderBootLayer,
    compute_flavor_identity: ComputeFlavorIdentity,
    plan_systemd_provisioning: PlanSystemdProvisioning,
    agent_install_spec: AgentInstallSpec | None = None,
    agent_user: str | int | None = None,
    home: Path | None = None,
    build_timeout_sec: float | None = 600.0,
    pull: str | None = None,
    built_epoch: int | None = None,
) -> ConversionResult:
    """Convert ``environment_dir`` into a published Cella rootfs flavor.

    Args:
        environment_dir: The task's ``environment/`` directory.
        ext4_size_bytes: The filesystem's capacity. The task's declared storage
            contract, passed through; this function invents no sizing rule.
        render_boot_layer: See :data:`RenderBootLayer`.
        compute_flavor_identity: See :data:`ComputeFlavorIdentity`.
        plan_systemd_provisioning: How to make a guest that does not boot
            systemd boot it. Passed in rather than imported, for the same
            reason the other two are: this module must not acquire an opinion
            about package managers. Called at most once, and only when the
            task's own filesystem is not already bootable.
        agent_install_spec: Baked into the image at build time when set.
        agent_user: Titanium's runtime-user decision -- ``[agent].user`` from
            task.toml, the same value ``BaseEnvironment.default_user`` carries.
            Used for two different things, deliberately: it is the user the
            agent's install steps run as at build time, and it is the explicit
            runtime identity handed to the boot-layer decision, which must not
            read that identity off the built image's ``Config.User`` because
            install steps overwrite it.
        home: Cella's artifact home. Defaults to Cella's own resolution.
        build_timeout_sec: Applied to each podman invocation.
        pull: Podman's ``--pull`` policy for the task build, passed through
            only when given.
        built_epoch: The manifest's ``built_epoch``. Defaults to now.

    Returns:
        The published flavor, with ``reused=True`` when an intact flavor of the
        same identity was already present.

    Raises:
        ConversionError: on a bad input, or on an identity decision that
            returned the wrong type.
        TypeError, BootLayerError: when the boot-layer decision returned
            something that cannot be installed. Raised from the boot layer's
            own validation rather than reworded here, so the reason survives.
        SystemdBootError: when the guest cannot be made to boot systemd, or
            when a provisioning build succeeded and the resulting filesystem
            still does not. Never downgraded into a weaker outcome: there is no
            fallback to whatever init the image already carried.
        BuildFileError, PodmanError, RootfsBuildError, FlavorIntegrityError,
        ManifestFieldError: from the step that failed. None of them is caught
            and turned into a weaker outcome: a conversion either publishes a
            verified flavor or raises.
    """
    if ext4_size_bytes <= 0:
        raise ConversionError(f"ext4 capacity must be positive, got {ext4_size_bytes}.")
    if not environment_dir.is_dir():
        raise ConversionError(f"No environment directory at {environment_dir}.")

    tag = new_build_tag()
    with tempfile.TemporaryDirectory(prefix="titanium-cella-convert-") as work:
        work_dir = Path(work)
        try:
            context = prepare_build_context(
                environment_dir=environment_dir,
                context_dir=work_dir / "context",
                agent_install_spec=agent_install_spec,
                agent_user=agent_user,
            )
            build_image(
                context_dir=context.context_dir,
                build_file=context.build_file,
                tag=tag,
                timeout_sec=build_timeout_sec,
                pull=pull,
            )
            record = parse_image_record(
                inspect_image(tag, timeout_sec=build_timeout_sec)
            )
            # Resolved before the identity decision, because it is one of that
            # decision's inputs -- and passed to build_ext4 below, so the id
            # reported is the builder that ran.
            builder_id = rootfs_builder_image_id(timeout_sec=build_timeout_sec)

            source_tar = work_dir / "rootfs.tar"
            export_rootfs_tar(
                image=tag, dest_tar=source_tar, timeout_sec=build_timeout_sec
            )
            # Reads the tar with Python; runs nothing the task shipped. May
            # build a second image, which is Titanium's own artifact and never
            # a restatement of what the task declared.
            prepared = prepare_systemd_rootfs(
                source_tag=tag,
                source_image_id=record.image_id,
                source_rootfs_tar=source_tar,
                work_dir=work_dir,
                plan_provisioning=plan_systemd_provisioning,
                timeout_sec=build_timeout_sec,
            )

            # `record`, not the derived image's. Provisioning changed the
            # filesystem, not the task's declaration, and the derived image's
            # Config.User is Titanium's own `USER 0` -- reading runtime
            # semantics off it would substitute build plumbing for what the
            # task declared.
            boot_layer_inputs = BootLayerInputs(
                image=record,
                agent_user=agent_user,
                agent_install_applied=context.agent_install_applied,
            )
            layer = _require_boot_layer(render_boot_layer, boot_layer_inputs)
            layer_digest = boot_layer_digest(layer)
            facts = _build_facts(
                context=context,
                record=record,
                boot_layer=layer,
                prepared=prepared,
                agent_install_spec=agent_install_spec,
                agent_user=agent_user,
                ext4_size_bytes=ext4_size_bytes,
                builder_image_id=builder_id,
            )
            identity = _require_identity(compute_flavor_identity, facts)
            destination = rootfs_flavor_dir(identity.flavor_name, home=home)

            if destination.exists():
                # An intact flavor is a hit. A flavor that fails verification
                # is refused, not rebuilt over: something produced a mismatch
                # between an artifact and its manifest, and quietly replacing
                # it would destroy the only evidence of what.
                digest = verify_flavor_dir(
                    destination, expected_flavor=identity.flavor_name
                )
                _require_matching_boot_layer(destination, layer_digest)
                artifact = destination / ROOTFS_ARTIFACT_NAME
                return ConversionResult(
                    flavor_name=identity.flavor_name,
                    flavor_dir=destination,
                    artifact_path=artifact,
                    sha3_256=digest,
                    size_bytes=artifact.stat().st_size,
                    reused=True,
                )

            with staging_flavor_dir(home=home) as staging:
                artifact = staging / ROOTFS_ARTIFACT_NAME
                build_ext4(
                    # The prepared filesystem, which is the derived one
                    # whenever provisioning ran.
                    rootfs_tar=prepared.rootfs_tar,
                    boot_layer=facts.boot_layer,
                    size_bytes=ext4_size_bytes,
                    dest=artifact,
                    builder_image=builder_id,
                    timeout_sec=build_timeout_sec,
                )
                write_manifest(
                    staging,
                    render_golden_json(
                        flavor=identity.flavor_name,
                        sha3_256=sha3_256_file(artifact),
                        size_bytes=artifact.stat().st_size,
                        built_epoch=(
                            int(time.time()) if built_epoch is None else built_epoch
                        ),
                        extra_fields={
                            **identity.manifest_fields,
                            BOOT_LAYER_FIELD: layer_digest,
                        },
                    ),
                )
                # Independent of everything above: re-read the manifest from
                # disk and re-hash the artifact. `cella doctor verify` cannot
                # stand in for this -- it walks a hardcoded list of Cella's own
                # flavors and exits 0 having checked nothing for a foreign one.
                # expected_flavor, not staging.name: the directory here is
                # `.tmp-<random>` while the manifest already carries the final
                # name, so the directory cannot answer this question.
                digest = verify_flavor_dir(
                    staging, expected_flavor=identity.flavor_name
                )
                size_bytes = artifact.stat().st_size
                publish_flavor(staging, destination)

            return ConversionResult(
                flavor_name=identity.flavor_name,
                flavor_dir=destination,
                artifact_path=destination / ROOTFS_ARTIFACT_NAME,
                sha3_256=digest,
                size_bytes=size_bytes,
                reused=False,
            )
        finally:
            # Drops only the tag this conversion created; the image and its
            # layers, which may be another build's cache, are left alone.
            untag_image(tag)


def _require_boot_layer(render: RenderBootLayer, inputs: BootLayerInputs) -> BootLayer:
    """Render the boot layer and refuse anything that cannot be installed.

    The check is the boot layer's own, not a second one written here. Falling
    back to whatever the image already carried would boot an entrypoint whose
    semantics nothing classified.
    """
    return validate_boot_layer(render(inputs))


def _require_matching_boot_layer(destination: Path, layer_digest: str) -> None:
    """Refuse a cached flavor that was built from a different boot layer.

    Refusal rather than a silent rebuild, for the same reason a digest
    mismatch is refused: the published directory and the layer now being asked
    for disagree, and overwriting one of them would destroy the evidence of
    which was wrong. An identity function that includes the layer in its
    flavor name never reaches this, because a changed layer is simply a
    different directory.
    """
    text = (destination / MANIFEST_NAME).read_text()
    recorded = manifest_field(text, BOOT_LAYER_FIELD)
    if recorded != layer_digest:
        raise ConversionError(
            f"{destination} was built from boot layer {recorded!r}, but this "
            f"conversion places {layer_digest!r}. The flavor name does not "
            f"distinguish them, so the cached artifact cannot answer for this "
            f"layer."
        )


def _require_identity(
    compute: ComputeFlavorIdentity, facts: BuildFacts
) -> FlavorIdentity:
    identity = compute(facts)
    if not isinstance(identity, FlavorIdentity):
        raise ConversionError(
            f"compute_flavor_identity must return a FlavorIdentity, got "
            f"{type(identity).__name__}."
        )
    if BOOT_LAYER_FIELD in identity.manifest_fields:
        raise ConversionError(
            f"compute_flavor_identity must not set {BOOT_LAYER_FIELD!r}: the "
            f"converter writes it from the layer it actually placed, and a "
            f"supplied value could disagree with the artifact."
        )
    validate_flavor_name(identity.flavor_name)
    return identity


def _build_facts(
    *,
    context: PreparedContext,
    record: ImageRecord,
    boot_layer: BootLayer,
    prepared: PreparedSystemdRootfs,
    agent_install_spec: AgentInstallSpec | None,
    agent_user: str | int | None,
    ext4_size_bytes: int,
    builder_image_id: str,
) -> BuildFacts:
    return BuildFacts(
        source_build_file_name=context.source_build_file_name,
        source_build_file_bytes=context.source_build_file_bytes,
        staged_build_file_bytes=context.staged_build_file_bytes,
        image=record,
        boot_layer=boot_layer,
        systemd_strategy=prepared.strategy,
        systemd_source_os=prepared.source_info,
        systemd_final_os=prepared.final_info,
        systemd_recipe_bytes=prepared.recipe_bytes,
        boot_image_id=prepared.boot_image_id,
        agent_install_fingerprint=(
            None if agent_install_spec is None else agent_install_spec.fingerprint()
        ),
        agent_user=agent_user,
        agent_install_applied=context.agent_install_applied,
        ext4_size_bytes=ext4_size_bytes,
        converter_version=CONVERTER_VERSION,
        rootfs_builder_image_id=builder_image_id,
        rootfs_builder_recipe=ROOTFS_BUILDER_CONTAINERFILE,
    )


def empty_manifest_fields() -> Mapping[str, str]:
    """An immutable empty mapping, for an identity that pins nothing extra."""
    return MappingProxyType({})


__all__ = [
    "CONVERTER_VERSION",
    "BuildFacts",
    "ComputeFlavorIdentity",
    "ConversionError",
    "ConversionResult",
    "FlavorIdentity",
    "RenderBootLayer",
    "convert_task_to_rootfs_flavor",
    "empty_manifest_fields",
]
