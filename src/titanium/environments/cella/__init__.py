"""Build-time conversion of a Titanium task into a Cella rootfs flavor.

Titanium owns the Dockerfile-to-rootfs path; Cella owns the runtime. This
package is the Titanium half, and only its build-time part: it turns one
task's ``environment/Dockerfile`` (or ``Containerfile``) into

    $CELLA_HOME/rootfs/<flavor>/rootfs.ext4
    $CELLA_HOME/rootfs/<flavor>/golden.json

Podman appears here and nowhere near the runtime. Nothing in this package
creates, starts, inspects, or destroys a Cella machine.

Beside the build-time half sit two runtime-seam modules that also drive no
machine: the cella wire vocabulary
(:mod:`titanium.environments.cella.wire`) and the minimal policy engine
that turns a task's ``allow_internet`` flag into per-crossing decisions
(:mod:`titanium.environments.cella.engine`). They serve the gRPC seam
cella's bridge dials (cella docs/WORLD-ENGINE.md) and are imported
directly rather than re-exported here.

Three load-bearing decisions are deliberately absent and arrive as callables:

* how a guest that does not boot systemd is made to
  (``systemd_boot.PlanSystemdProvisioning``),
* the boot layer Titanium contributes on top of it
  (``converter.RenderBootLayer``), and
* the flavor identity / cache key and the manifest inputs it pins
  (``converter.ComputeFlavorIdentity``).

Everything here is the mechanical frame around those three. The distro's own
systemd is PID 1, and the boot layer is empty until controller material is
added to it.
"""

from titanium.environments.cella.boot_layer import (
    BootEntry,
    BootLayer,
    BootLayerError,
    BootLayerInputs,
    GuestFile,
    GuestSymlink,
    validate_boot_layer,
)
from titanium.environments.cella.buildfile import (
    BuildFileError,
    PreparedContext,
    discover_build_file,
    prepare_build_context,
)
from titanium.environments.cella.constants import (
    BUILD_FILE_NAMES,
    CONVERTER_VERSION,
    MANIFEST_NAME,
    MAX_GUEST_FILE_MODE,
    ROOTFS_ARTIFACT_NAME,
    ROOTFS_BUILDER_BASE_IMAGE,
    ROOTFS_BUILDER_IMAGE,
    STRATEGY_ALREADY_SYSTEMD,
    SYSTEMD_BINARY_CANDIDATES,
)
from titanium.environments.cella.converter import (
    BuildFacts,
    ComputeFlavorIdentity,
    ConversionResult,
    FlavorIdentity,
    RenderBootLayer,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.flavor import (
    FlavorIntegrityError,
    ManifestFieldError,
    cella_home,
    rootfs_artifact_path,
    rootfs_flavor_dir,
    verify_flavor_dir,
)
from titanium.environments.cella.image_config import ImageRecord, parse_image_record
from titanium.environments.cella.podman import PodmanError
from titanium.environments.cella.rootfs import (
    build_ext4,
    ensure_rootfs_builder_image,
    rootfs_builder_image_id,
    sha3_256_file,
)
from titanium.environments.cella.systemd_boot import (
    BuildRun,
    GuestOsInfo,
    PlanSystemdProvisioning,
    PreparedSystemdRootfs,
    RootfsArchive,
    SystemdBootError,
    SystemdProvisionPlan,
    plan_systemd_provisioning,
    prepare_systemd_rootfs,
    probe_rootfs_tar,
    render_derived_build_file,
    validate_provision_plan,
)

__all__ = [
    "BUILD_FILE_NAMES",
    "CONVERTER_VERSION",
    "MANIFEST_NAME",
    "MAX_GUEST_FILE_MODE",
    "ROOTFS_ARTIFACT_NAME",
    "ROOTFS_BUILDER_BASE_IMAGE",
    "ROOTFS_BUILDER_IMAGE",
    "STRATEGY_ALREADY_SYSTEMD",
    "SYSTEMD_BINARY_CANDIDATES",
    "BootEntry",
    "BootLayer",
    "BootLayerError",
    "BootLayerInputs",
    "BuildFacts",
    "BuildFileError",
    "BuildRun",
    "ComputeFlavorIdentity",
    "ConversionResult",
    "FlavorIdentity",
    "FlavorIntegrityError",
    "GuestFile",
    "GuestOsInfo",
    "GuestSymlink",
    "ImageRecord",
    "ManifestFieldError",
    "PlanSystemdProvisioning",
    "PodmanError",
    "PreparedContext",
    "PreparedSystemdRootfs",
    "RenderBootLayer",
    "RootfsArchive",
    "SystemdBootError",
    "SystemdProvisionPlan",
    "build_ext4",
    "cella_home",
    "convert_task_to_rootfs_flavor",
    "discover_build_file",
    "ensure_rootfs_builder_image",
    "parse_image_record",
    "plan_systemd_provisioning",
    "prepare_build_context",
    "prepare_systemd_rootfs",
    "probe_rootfs_tar",
    "render_derived_build_file",
    "rootfs_artifact_path",
    "rootfs_builder_image_id",
    "rootfs_flavor_dir",
    "sha3_256_file",
    "validate_boot_layer",
    "validate_provision_plan",
    "verify_flavor_dir",
]
