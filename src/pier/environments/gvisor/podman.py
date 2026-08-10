"""A first-class gVisor-on-Podman environment, selected with ``--env gvisor-podman``.

``GVisorPodmanEnvironment`` composes the two existing environments rather than
duplicating either. Method resolution order is::

    GVisorPodmanEnvironment -> GVisorEnvironment -> PodmanEnvironment
                            -> DockerEnvironment -> BaseEnvironment

which yields exactly the split this environment needs:

* **Driving** comes from :class:`~pier.environments.podman.podman.PodmanEnvironment`:
  every compose command runs through ``podman-compose`` against libpod
  directly, so there is no Docker API socket -- and no Podman socket --
  anywhere in the path. Rootless operation is Podman's native mode; Podman
  sets up the user namespace and invokes ``runsc`` with UID 0 already
  established inside it, which is gVisor's supported rootless path.

* **Sandboxing** comes from :class:`~pier.environments.gvisor.environment.GVisorEnvironment`
  and is unchanged: only ``main`` runs under the gVisor runtime, verification
  is host-side and gates every command, transfers go through the scoped
  staging bind mounts (``podman cp`` cannot see a runsc container's
  sandbox-private root filesystem any more than ``docker cp`` can), and
  teardown is fail-closed against this exact Compose project.

What this class itself contains is only the seam between the two: the engine
hooks where Podman's schema differs from Docker's (see
:mod:`pier.environments.gvisor.podman_runtime` for the specifics and how each
was verified), service resolution by label because ``podman-compose ps``
cannot scope to a service, and the rootless ownership rule for staged exports.
"""

from __future__ import annotations

import os

from pier.environments.gvisor.environment import GVisorEnvironment
from pier.environments.gvisor.podman_runtime import (
    assert_runtime_resolvable,
    container_oci_runtime,
    project_container_ids_podman,
    project_network_refs_podman,
    runtime_name_matches,
    service_container_ids,
)
from pier.environments.gvisor.runtime import DEFAULT_RUNTIME
from pier.environments.gvisor.transfer import GVisorUnixOps
from pier.environments.podman.podman import PodmanEnvironment
from pier.models.environment_type import EnvironmentType


class GVisorPodmanUnixOps(GVisorUnixOps):
    """Staging transfer ops with the rootless ownership rule.

    ``GVisorUnixOps`` chowns staged exports to ``os.getuid():os.getgid()`` --
    correct under rootful Docker, where in-container UIDs are host UIDs.
    Under rootless Podman the mapping inverts: in-container UID 0 is the one
    that maps to the invoking host user, while the host user's own numeric UID
    maps into the subuid range and would leave exports the host cannot read or
    remove. Chowning the staged copy to in-container root therefore lands it
    host-user-owned, which is the contract downloads promise.
    """

    def _host_owner(self) -> str | None:
        return "0:0"


class GVisorPodmanEnvironment(GVisorEnvironment, PodmanEnvironment):
    """Podman-driven environment that runs the untrusted service under runsc."""

    _SUPPORTED_ENGINES: tuple[str, ...] = ("podman",)

    def __init__(self, *args, engine: str = "podman", **kwargs):
        # The engine kwarg stays accepted for symmetry with --env gvisor, but
        # only "podman" resolves here: resolve_engine_cli (called by the
        # GVisorEnvironment initializer with _SUPPORTED_ENGINES) redirects
        # engine=docker to --env gvisor rather than silently driving Docker.
        super().__init__(*args, engine=engine, **kwargs)

        # Host-side inspection and cleanup must honor PIER_PODMAN_BIN exactly
        # like the compose driving does; PodmanEnvironment resolved it during
        # construction, so every engine hook below routes through the same
        # binary as the commands that created the resources.
        self._engine_cli = self.podman_bin

        # GVisorEnvironment installed GVisorUnixOps after its super() chain
        # ran (deliberately clobbering PodmanUnixOps: `podman cp` cannot see
        # the sandbox-private rootfs); narrow it further to the rootless
        # ownership rule.
        self._platform = GVisorPodmanUnixOps(self)

    # -- identity ----------------------------------------------------------

    @staticmethod
    def type() -> str:
        return EnvironmentType.GVISOR_PODMAN

    @classmethod
    def preflight(cls) -> None:
        """Check Podman, podman-compose, *and* that runsc is resolvable.

        Runs once at CLI time, before any trial is queued, so a host without
        runsc fails immediately instead of after the first image build.
        ``PodmanEnvironment.preflight`` is named explicitly rather than
        reached via ``super()``: the MRO's next preflight is
        ``GVisorEnvironment``'s, which asserts against the Docker daemon.
        """
        PodmanEnvironment.preflight()
        assert_runtime_resolvable(
            DEFAULT_RUNTIME, os.environ.get("PIER_PODMAN_BIN", "podman")
        )

    # -- engine seam overrides ---------------------------------------------

    def _assert_runtime_registered(self) -> None:
        # Podman has no daemon registry to consult; make Podman itself resolve
        # the runtime, which is the same resolution compose-up will perform.
        assert_runtime_resolvable(self._runtime, self._engine_cli)

    async def _container_runtime(self, container_id: str) -> str | None:
        # Podman's {{.HostConfig.Runtime}} is a compat placeholder ("oci");
        # {{.OCIRuntime}} is the runtime Podman actually used.
        return await container_oci_runtime(container_id, self._engine_cli)

    def _runtime_matches(self, actual: str | None) -> bool:
        # Podman reports the configured name or the resolved path depending on
        # how the runtime was selected; both must count.
        return runtime_name_matches(actual, self._runtime)

    async def _query_project_container_ids(self) -> list[str]:
        return await project_container_ids_podman(self._project_name, self._engine_cli)

    async def _query_project_network_ids(self) -> list[str]:
        # "IDs" may be names: `podman network ls --quiet` prints names on
        # Podman 4.x, and `podman network rm` accepts either.
        return await project_network_refs_podman(self._project_name, self._engine_cli)

    # -- verification wiring -----------------------------------------------

    async def _compose_container_id(
        self, service: str, *, include_stopped: bool = False
    ) -> str | None:
        """Resolve *service* by label instead of through ``podman-compose ps``.

        podman-compose's ``ps`` ignores the service argument and always lists
        the whole project with ``--all``, so the parent's implementation would
        hand verification an arbitrary project container -- the trusted proxy,
        or a stopped one -- as readily as the running ``main``. Label-filtered
        ``podman ps`` preserves both the service scoping and the running-only
        default the verification contract states.
        """
        ids = await service_container_ids(
            self._project_name,
            service,
            self._engine_cli,
            include_stopped=include_stopped,
        )
        return ids[0] if ids else None

    # -- compose wiring ----------------------------------------------------

    # ``-T`` on programmatic execs is inherited from PodmanEnvironment. It is
    # load-bearing here rather than cosmetic: runsc's exec implements no
    # ``-tty`` flag, so without it every exec fails with "flag provided but
    # not defined: -tty" (crun merely tolerates the pty).

    def _prepare_gvisor(self) -> None:
        """Write the override with Podman's SELinux relabel option.

        Podman does not relabel bind mounts, so on an SELinux-enforcing host
        the sandbox would be denied its own staging directories. The relabel
        value mirrors PodmanEnvironment's handling of task mounts: same
        environment knob, same 'z'-not-'Z' default, and podman-compose honors
        the long-form ``bind.selinux`` option (Docker Compose has no use for
        it, which is why the docker flavor never sets one).

        The process label is the counterpart problem: Podman labels every
        container process on an enforcing host, and runsc rejects a labeled
        spec outright, so the override also carries ``label=disable``.
        """
        relabel = os.environ.get("PIER_PODMAN_SELINUX_RELABEL", "z")
        self._stage_in.mkdir(parents=True, exist_ok=True)
        self._stage_out.mkdir(parents=True, exist_ok=True)

        if self.task_env_config.allow_internet:
            from pier.environments.gvisor import network

            self._resolvers = network.select_resolvers(self._dns_option)

        from pier.environments.gvisor.runtime import (
            COMPOSE_OVERRIDE_NAME,
            write_compose_override,
        )

        self._compose_override_path = write_compose_override(
            self.trial_paths.trial_dir / COMPOSE_OVERRIDE_NAME,
            runtime=self._runtime,
            stage_in=self._stage_in,
            stage_out=self._stage_out,
            dns=self._resolvers,
            selinux_relabel=relabel if relabel in ("z", "Z") else None,
            disable_process_label=True,
        )

    # -- ownership ----------------------------------------------------------

    async def _chown_to_host_user(self, path: str, recursive: bool = False) -> None:
        """No-op, inherited rationale from PodmanEnvironment.

        Redeclared because the MRO would otherwise resolve to
        DockerEnvironment's implementation through GVisorEnvironment: rootless
        Podman already maps container root to the invoking user, and a chown
        to ``os.getuid()`` would resolve through the userns into the subuid
        range, leaving artifacts the host cannot write.
        """
        return await PodmanEnvironment._chown_to_host_user(
            self, path, recursive=recursive
        )
