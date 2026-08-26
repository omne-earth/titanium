"""A first-class krun-on-Podman environment, selected with ``--env krun-podman``.

krun is crun built with the libkrun handler; it runs each container in a
KVM microVM. ``KrunPodmanEnvironment`` extends
:class:`~titanium.environments.gvisor.podman.GVisorPodmanEnvironment` and changes
only what the runtime changes. Method resolution order is::

    KrunPodmanEnvironment -> GVisorPodmanEnvironment -> GVisorEnvironment
                          -> PodmanEnvironment -> DockerEnvironment
                          -> BaseEnvironment

so all of the Podman driving, the label-based discovery, the fail-closed
teardown, the staging transfers, and the rootless ownership rules are
inherited as-is. What differs from the runsc flavor:

* **Runtime identity.** The sandbox runtime is ``krun``. The digest pin is
  the one scripts/init/krun-podman.sh records for the installed binary,
  with its own env knob (``TITANIUM_KRUN_DIGEST_PIN``). The krun pin and the
  runsc pin are separate on purpose: each runtime is blessed, rotated, and
  verified on its own.

* **The SELinux process label stays on.** Podman labels every container
  process on an enforcing host. runsc rejects a labeled spec, so the runsc
  flavor must send ``label=disable``; crun supports SELinux, so this flavor
  keeps the label and confinement is stronger here, not weaker. The staging
  bind mounts keep the same relabel handling ('z' by default).

* **No engine redirect.** Titanium has no docker-daemon flavor of krun, so
  ``engine="docker"`` fails with a krun-specific message instead of the
  inherited redirect to ``--env gvisor`` (a different sandbox technology).

What stays inherited deliberately, although krun would allow less: a krun
rootfs is virtiofs-shared, so ``podman cp`` would likely work, and TSI
networking makes in-VM DNS behave close to a normal container. The staging
transfer path and the resolver handling are correct for both runtimes, and
one code path for the whole lineage is simpler to trust than two. Trim only
after the live smoke proves a simpler path.
"""

from __future__ import annotations

import os

from titanium.environments.gvisor.podman import GVisorPodmanEnvironment
from titanium.environments.gvisor.podman_runtime import assert_runtime_digest
from titanium.models.environment_type import EnvironmentType

# The runtime name scripts/init/krun-podman.sh registers with Podman.
DEFAULT_RUNTIME = "krun"

# Where scripts/init/krun-podman.sh records `<sha3-512>  <path>` for the
# dnf-installed krun binary (trust-on-first-use). Separate from the runsc
# pin. Overridable so deployments and tests can relocate the pin.
KRUN_DIGEST_PIN = "/usr/local/share/titanium/krun.sha3-512"

KRUN_INIT_SCRIPT = "scripts/init/krun-podman.sh"


class KrunPodmanEnvironment(GVisorPodmanEnvironment):
    """Podman-driven environment that runs the untrusted service under krun."""

    _PREFLIGHT_RUNTIME = DEFAULT_RUNTIME
    # crun supports SELinux; keep the process label the runsc flavor must
    # disable.
    _DISABLE_PROCESS_LABEL = False

    def __init__(
        self,
        *args,
        engine: str = "podman",
        runtime: str = DEFAULT_RUNTIME,
        **kwargs,
    ):
        # Checked here, before the inherited engine resolution: for a
        # docker request that resolution would redirect to --env gvisor,
        # which is a different sandbox technology, not a docker flavor of
        # this one.
        if str(engine).strip().lower() != "podman":
            raise ValueError(
                f"The 'krun-podman' environment only drives the 'podman' "
                f"container engine, got engine={engine!r}. No docker flavor "
                "of the krun sandbox exists. Refusing to continue rather "
                "than silently driving a different engine than the one "
                "asked for."
            )
        super().__init__(*args, engine=engine, runtime=runtime, **kwargs)

    # -- identity ----------------------------------------------------------

    @staticmethod
    def type() -> str:
        return EnvironmentType.KRUN_PODMAN

    # -- runtime trust -----------------------------------------------------

    @classmethod
    def _assert_runtime_digest(cls) -> None:
        assert_runtime_digest(
            os.environ.get("TITANIUM_KRUN_DIGEST_PIN", KRUN_DIGEST_PIN),
            init_script=KRUN_INIT_SCRIPT,
        )
