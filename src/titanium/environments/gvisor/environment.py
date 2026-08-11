"""A first-class gVisor environment, selected with ``--env gvisor``.

``GVisorEnvironment`` subclasses :class:`~titanium.environments.docker.docker.DockerEnvironment`
because this slice still uses the Docker CLI, Docker Compose, Docker build
behaviour, Docker exec behaviour, the Docker service lifecycle and Docker
networking primitives. Only what is genuinely different under gVisor is
overridden; no part of the Docker lifecycle is duplicated, and
``src/titanium/environments/docker/`` is not modified at all.

Only the untrusted ``main`` service runs under the gVisor runtime. Every other
service -- notably the trusted egress proxy -- keeps Docker's default runtime,
and Docker's own default runtime is never changed.

**Verification is host-side and gates every command.** ``exec`` runs
:meth:`_ensure_verified` first, so no command of any kind reaches the sandbox
before ``docker inspect`` has confirmed, from the trusted host, which runtime
the daemon actually used. That includes the ``chmod`` that
``DockerEnvironment.start()`` issues immediately after ``compose up``. Nothing
produced inside the sandbox -- ``uname``, ``dmesg``, a file, or a claim from the
agent -- is ever treated as evidence.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from titanium.environments.agent_setup import (
    EGRESS_PROXY_PORT,
    EGRESS_PROXY_SERVICE,
    proxy_environment,
)
from titanium.environments.base import ExecResult
from titanium.environments.capabilities import EnvironmentCapabilities
from titanium.environments.docker.docker import (
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from titanium.environments.gvisor import network
from titanium.environments.gvisor.runtime import (
    COMPOSE_OVERRIDE_NAME,
    COMPOSE_PROJECT_LABEL,
    DEFAULT_RUNTIME,
    assert_runtime_registered,
    container_networks,
    container_runtime,
    container_state,
    parse_container_ids,
    project_container_ids,
    project_network_ids,
    remove_containers,
    remove_networks,
    resolve_engine_cli,
    shared_network_ipv4,
    stage_dirs,
    write_compose_override,
)
from titanium.environments.gvisor.transfer import GVisorUnixOps
from titanium.models.environment_type import EnvironmentType


class VerificationState(str, Enum):
    """Lifecycle of the trusted host-side runtime check.

    Explicit states rather than a boolean, because "verified" is not one
    condition: the host-side runtime check and the in-sandbox network repair
    happen at different points and only the first may gate the second.
    """

    NOT_STARTED = "not_started"
    VERIFYING = "verifying"
    RUNTIME_VERIFIED = "runtime_verified"
    READY = "ready"
    FAILED = "failed"


class GVisorEnvironment(DockerEnvironment):
    """Docker-backed environment that runs the untrusted service under runsc."""

    # Engines this class actually drives. The Podman flavor
    # (:class:`~titanium.environments.gvisor.podman.GVisorPodmanEnvironment`)
    # narrows this to ("podman",); resolve_engine_cli redirects a known but
    # unsupported engine to the --env selector that does drive it.
    _SUPPORTED_ENGINES: tuple[str, ...] = ("docker",)

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths,
        task_env_config,
        keep_containers: bool = False,
        mounts_json=None,
        *,
        engine: str = "docker",
        runtime: str = DEFAULT_RUNTIME,
        dns: object = None,
        probe_host: str = network.DEFAULT_PROBE_HOST,
        probe_port: int = network.DEFAULT_PROBE_PORT,
        probe_timeout_sec: int = network.DEFAULT_PROBE_TIMEOUT_SEC,
        **kwargs,
    ):
        # Resolved first: an unsupported engine is rejected at construction,
        # before anything is validated, built or started. There is no fallback
        # to Docker -- a caller who asked for Podman must not silently receive a
        # sandbox with different isolation properties.
        self._engine = str(engine)
        self._engine_cli = resolve_engine_cli(
            self._engine, supported=self._SUPPORTED_ENGINES
        )
        self._runtime = str(runtime)

        self._dns_option = dns
        self._resolvers: list[str] = []
        self._probe_host = probe_host
        self._probe_port = int(probe_port)
        self._probe_timeout_sec = int(probe_timeout_sec)

        # Staging paths are derived before super().__init__() because the
        # platform helpers read them during transfers.
        self._stage_in, self._stage_out = stage_dirs(trial_paths.trial_dir)
        self._compose_override_path: Path | None = None

        # Serializes concurrent or repeated _teardown() calls (a failure
        # inside _ensure_verified() and a failure in start() can both try to
        # tear down the same attempt) so cleanup work is never run twice at
        # once and "no resources remain" is checked against a settled state.
        self._teardown_lock = asyncio.Lock()

        self._state = VerificationState.NOT_STARTED
        self._failure: BaseException | None = None
        self._verifying_task: asyncio.Task | None = None
        self._verified_event = asyncio.Event()
        self._torn_down = False
        self._stopping = False

        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            keep_containers=keep_containers,
            mounts_json=mounts_json,
            **kwargs,
        )

        # Replace the Docker transfer helpers: `docker compose cp` cannot see
        # the sandbox-private root filesystem in either direction.
        self._platform = GVisorUnixOps(self)

        self._validate_mounts()

    # -- identity and capabilities ----------------------------------------

    @staticmethod
    def type() -> str:
        return EnvironmentType.GVISOR

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # Differs from Docker in exactly two places, and both are enforced by
        # BaseEnvironment's own validators rather than by ad-hoc checks here:
        # runsc is Linux-only, and a task's own compose file could re-add
        # list-valued keys (privileged, cap_add, devices, volumes) that an
        # override cannot remove.
        return EnvironmentCapabilities(
            disable_internet=True,
            filtered_egress=True,
            preinstall_agents=True,
            windows=False,
            mounted=True,
            docker_compose=False,
        )

    @classmethod
    def preflight(cls) -> None:
        """Check Docker *and* that the sandbox runtime is registered.

        Runs once at CLI time, before any trial is queued, so a host without
        runsc fails immediately instead of after the first image build.
        """
        super().preflight()
        assert_runtime_registered(DEFAULT_RUNTIME)

    # -- read-only accessors ----------------------------------------------

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def _project_name(self) -> str:
        """The exact Compose project label this instance's resources carry.

        Computed the same way DockerEnvironment computes it so fallback
        cleanup targets precisely (and only) this environment's own resources
        -- never another project's, even one sharing a prefix. A property
        rather than an attribute assigned in ``__init__`` so the Podman flavor
        -- whose PodmanEnvironment base declares the identical read-only
        property for its compose driving -- composes without the instance
        assignment tripping over the property's missing setter.
        """
        return _sanitize_docker_compose_project_name(self.session_id)

    @property
    def project_name(self) -> str:
        """The exact Compose ``--project-name`` this environment's resources carry."""
        return self._project_name

    @property
    def stage_in(self) -> Path:
        """Host directory bind-mounted read-only into the sandbox for uploads."""
        return self._stage_in

    @property
    def stage_out(self) -> Path:
        """Host directory bind-mounted writable into the sandbox for exports."""
        return self._stage_out

    @property
    def resolvers(self) -> list[str]:
        """Nameservers chosen for unrestricted-internet mode (empty otherwise)."""
        return list(self._resolvers)

    @property
    def verification_state(self) -> VerificationState:
        return self._state

    # -- engine seam -------------------------------------------------------
    #
    # Every host-side interaction whose schema or semantics differ between
    # engines goes through one of these hooks. The defaults are the Docker
    # behavior this class has always had; the Podman flavor overrides exactly
    # the hooks where Podman differs (inspect fields, label namespaces,
    # runtime registration) and nothing else. Verification, teardown and the
    # network logic above them stay engine-agnostic by construction.

    def _assert_runtime_registered(self) -> None:
        """Fail closed unless the sandbox runtime is available to the engine.

        For Docker the daemon's registry is authoritative; other engines
        resolve runtimes differently and override this hook.
        """
        assert_runtime_registered(self._runtime, self._engine_cli)

    async def _container_runtime(self, container_id: str) -> str | None:
        """The OCI runtime the engine actually used for *container_id*."""
        return await container_runtime(container_id, self._engine_cli)

    def _runtime_matches(self, actual: str | None) -> bool:
        """Whether the engine-reported runtime is the requested sandbox runtime.

        Docker reports exactly the registered name, so equality is the whole
        test. Engines that may report a resolved path instead override this.
        """
        return actual == self._runtime

    async def _container_state(self, container_id: str) -> dict | None:
        return await container_state(container_id, self._engine_cli)

    async def _container_networks(self, container_id: str) -> dict[str, dict]:
        return await container_networks(container_id, self._engine_cli)

    async def _query_project_container_ids(self) -> list[str]:
        """Container IDs (any state) labeled for this exact Compose project."""
        return await project_container_ids(self._project_name, self._engine_cli)

    async def _query_project_network_ids(self) -> list[str]:
        """Network references labeled for this exact Compose project."""
        return await project_network_ids(self._project_name, self._engine_cli)

    async def _remove_containers(self, container_ids: list[str]) -> None:
        await remove_containers(container_ids, self._engine_cli)

    async def _remove_networks(self, network_ids: list[str]) -> None:
        await remove_networks(network_ids, self._engine_cli)

    # -- validation --------------------------------------------------------

    def _validate_definition(self) -> None:
        super()._validate_definition()

        if sys.platform != "linux":
            raise RuntimeError(
                "The gVisor environment requires a Linux host, but this host "
                f"reports {sys.platform!r}. gVisor's runsc runtime is "
                "Linux-only. Use --env docker to run this task under the "
                "default runtime."
            )

        if self._environment_docker_compose_path.exists():
            raise ValueError(
                "The gVisor environment is currently supported only for "
                "Dockerfile or prebuilt-image tasks, not docker-compose tasks. "
                "A Compose override cannot remove list-valued keys such as "
                "privileged, cap_add, devices or extra volumes from a task's "
                "own compose file, so the task could weaken the sandbox."
            )

    def _validate_mounts(self) -> None:
        """Confine bind-mount sources to the trial directory.

        The security contract admits no broad host mounts and no engine socket,
        and a Compose override cannot remove a volume a caller already asked for
        -- Compose appends list-valued keys rather than replacing them. So the
        only place this can be enforced is here, by refusing to start. Plain
        Docker keeps accepting whatever the caller passes.
        """
        trial_dir = self.trial_paths.trial_dir.resolve()
        for mount in self._mounts_json or []:
            if mount.get("type") != "bind":
                continue
            source = str(mount.get("source", ""))
            resolved = Path(source).resolve()
            if resolved == trial_dir or resolved.is_relative_to(trial_dir):
                continue
            raise ValueError(
                f"The gVisor environment refuses the bind mount {source!r}: its "
                f"source resolves to {str(resolved)!r}, outside the trial "
                f"directory {str(trial_dir)!r}. Host mounts would let a task "
                "reach outside its sandbox; mount under the trial directory "
                "instead, or use --env docker."
            )

    # -- compose wiring ----------------------------------------------------

    @property
    def _docker_compose_paths(self) -> list[Path]:
        paths = list(super()._docker_compose_paths)
        # Last: Compose resolves scalars as last-writer-wins, so anything
        # earlier could have its `runtime` flipped back to the default.
        if self._compose_override_path:
            paths.append(self._compose_override_path)
        return paths

    def _prepare_gvisor(self) -> None:
        """Create the staging directories and write the gVisor override.

        Resolver selection happens here, before any image is built, so an
        unrestricted-internet task on a host with no usable resolver fails fast
        instead of after a long build.
        """
        self._stage_in.mkdir(parents=True, exist_ok=True)
        self._stage_out.mkdir(parents=True, exist_ok=True)

        if self.task_env_config.allow_internet:
            self._resolvers = network.select_resolvers(self._dns_option)

        self._compose_override_path = write_compose_override(
            self.trial_paths.trial_dir / COMPOSE_OVERRIDE_NAME,
            runtime=self._runtime,
            stage_in=self._stage_in,
            stage_out=self._stage_out,
            dns=self._resolvers,
        )

    # -- lifecycle ---------------------------------------------------------

    def _reset_for_new_start_attempt(self) -> None:
        """Make every start() call an independent attempt.

        Titanium -- not this class -- owns retry policy, and a retried start() can
        be called on the *same* instance (e.g. after a startup timeout). Without
        this reset, a terminal ``FAILED`` verification state from a previous
        attempt would refuse to re-verify a brand-new container, and a stale
        ``_torn_down`` would make this attempt's own teardown a silent no-op
        while its resources still exist.
        """
        self._state = VerificationState.NOT_STARTED
        self._failure = None
        self._verifying_task = None
        self._verified_event = asyncio.Event()
        self._torn_down = False
        self._stopping = False

    async def _teardown_preserving(self, primary: BaseException) -> None:
        """Run teardown without letting a cleanup failure replace *primary*.

        *primary* -- the original startup or verification failure -- must stay
        the top-level exception the caller re-raises: it is what actually
        explains why the environment did not come up, and a caller catching
        this failure by type must still see it, not a wrapper. If teardown
        also fails, that failure is attached to *primary* as a note rather
        than raised or discarded, so it is still visible (e.g. in a traceback
        or ``primary.__notes__``) without becoming the primary exception.
        """
        try:
            await self._teardown()
        except Exception as teardown_exc:
            primary.add_note(
                "The subsequent cleanup of Compose project "
                f"{self._project_name!r} also failed ({teardown_exc!r}). "
                "Manual cleanup may be required: remove the containers and "
                f"networks labeled {COMPOSE_PROJECT_LABEL}={self._project_name!r}."
            )

    async def start(self, force_build: bool):
        # Before anything expensive: refuse to build an image for a sandbox the
        # daemon cannot actually provide.
        # Deliberately outside the try below: nothing has been created yet, so
        # there is nothing to tear down and no image must be built.
        self._assert_runtime_registered()
        self._reset_for_new_start_attempt()
        try:
            # Resolver selection happens here too, so an unrestricted-internet
            # task on a host with no usable resolver fails before the build.
            self._prepare_gvisor()
            # DockerEnvironment.start() ends with `self.exec("chmod 777 ...")`,
            # which routes through the gate below, so verification happens
            # before that -- the first and only command it sends to the sandbox.
            await super().start(force_build)
            # Defensive: if the Docker start path ever stops issuing a command,
            # verification must still have run before the caller gets a handle.
            await self._ensure_verified()
        except BaseException as start_exc:
            await self._teardown_preserving(start_exc)
            raise

    async def stop(self, delete: bool):
        self._stopping = True
        try:
            await super().stop(delete)
        finally:
            self._cleanup_staging()

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        await self._ensure_verified()
        return await super().exec(
            command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user
        )

    # -- verification gate -------------------------------------------------

    async def _ensure_verified(self) -> None:
        """Run trusted host-side verification exactly once, before any exec.

        Resolution order matters:

        * ``READY`` -- nothing to do.
        * ``FAILED`` -- never retried, and never lets a command through. A
          failed environment cannot become ready.
        * called from the task that is *currently* verifying -- return
          immediately. This is what breaks recursion: the resolv.conf read,
          rewrite and probe all go through :meth:`exec`, and each of those would
          otherwise re-enter verification forever.
        * called from another task while verification is in flight -- wait for
          the in-flight run and adopt its outcome, so verification runs once and
          no caller observes a partially verified environment.
        """
        if self._state is VerificationState.READY:
            return

        if self._state is VerificationState.FAILED:
            raise RuntimeError(
                "The gVisor environment failed its runtime verification, so no "
                "command may run inside the sandbox."
            ) from self._failure

        if self._stopping:
            # Teardown must never trigger verification. Raising here is safe:
            # DockerEnvironment.prepare_logs_for_host() already treats a failed
            # chown as best-effort and logs it.
            raise RuntimeError(
                "The gVisor environment is stopping and is not verified; "
                "refusing to run a command inside the sandbox."
            )

        if self._verifying_task is not None:
            if asyncio.current_task() is self._verifying_task:
                return
            await self._verified_event.wait()
            if self._state is VerificationState.READY:
                return
            raise RuntimeError(
                "The gVisor environment failed its runtime verification, so no "
                "command may run inside the sandbox."
            ) from self._failure

        self._verifying_task = asyncio.current_task()
        self._state = VerificationState.VERIFYING
        try:
            await self._run_verification()
        except BaseException as exc:
            self._state = VerificationState.FAILED
            self._failure = exc
            # Never leave a started-but-unverified sandbox reachable.
            await self._teardown_preserving(exc)
            raise
        finally:
            self._verifying_task = None
            self._verified_event.set()

    async def _run_verification(self) -> None:
        """Host-side runtime checks first; only then anything inside the sandbox."""
        main_id = await self._verify_main_runtime()

        if self._egress_proxy_compose_path:
            # The proxy is declared, so it must be present and verifiable:
            # skipping here would leave the agent pointed at an unresolved
            # service name the sandbox cannot look up.
            proxy_id = await self._compose_container_id(EGRESS_PROXY_SERVICE)
            if proxy_id is None:
                raise RuntimeError(
                    "The gVisor environment could not resolve the "
                    f"{EGRESS_PROXY_SERVICE!r} container after startup, so it "
                    "can neither verify the proxy's runtime nor determine the "
                    "address the sandbox must use to reach it."
                )
            await self._verify_proxy_runtime(proxy_id)
            await self._resolve_proxy_address(main_id, proxy_id)

        # The runtime is now proven from the host. Only past this point may a
        # command run inside the sandbox.
        self._state = VerificationState.RUNTIME_VERIFIED

        if self.task_env_config.allow_internet:
            await self._normalize_sandbox_dns()
            await self._probe_connectivity()

        self._state = VerificationState.READY

    async def _compose_container_id(
        self, service: str, *, include_stopped: bool = False
    ) -> str | None:
        """Return the container ID for *service*, or None.

        Running-only by default, matching what verification is allowed to act
        on. Passing ``include_stopped`` runs the trusted host-side equivalent of
        ``docker compose ps --all --quiet <service>``, which is only ever used
        to *explain* why a container could not be verified -- never to gate
        exec, since a Created or Exited container must still be rejected.
        """
        command = ["ps"]
        if include_stopped:
            command.append("--all")
        command += ["--quiet", service]
        result = await self._run_docker_compose_command(command, check=False)
        ids = parse_container_ids(result.stdout)
        return ids[0] if ids else None

    async def _describe_non_running_main(self, container_id: str) -> str:
        """Trusted host-side detail for a 'main' that exists but is not running.

        Never execs into *container_id*: everything here comes from ``docker
        inspect`` on the host, which is safe to run against a Created, Exited
        or otherwise unverified container.
        """
        state = await self._container_state(container_id)
        status = (state or {}).get("Status", "unknown")
        error = (state or {}).get("Error") or "none"
        configured = await self._container_runtime(container_id)
        return (
            f"container ID {container_id}, State.Status={status!r}, "
            f"State.Error={error!r}, configured runtime={configured!r}"
        )

    async def _verify_main_runtime(self) -> str:
        """Confirm the daemon really placed ``main`` under the sandbox runtime."""
        main_id = await self._compose_container_id("main")
        if main_id is None:
            stale_id = await self._compose_container_id("main", include_stopped=True)
            if stale_id is not None:
                detail = await self._describe_non_running_main(stale_id)
                raise RuntimeError(
                    "The gVisor environment found the 'main' container but it "
                    f"is not running, so it refuses to verify or exec into it "
                    f"({detail}). Inspect and remove it, then retry."
                )
            raise RuntimeError(
                "The gVisor environment could not resolve the 'main' container "
                "after startup, so it cannot verify which runtime Docker used."
            )

        actual = await self._container_runtime(main_id)
        if not self._runtime_matches(actual):
            raise RuntimeError(
                f"Expected the 'main' container to run under {self._runtime!r} "
                f"but {self._engine_cli} reports {actual!r}. Refusing to run "
                "untrusted code under an unexpected runtime."
            )
        self._record_runtime_evidence("main", actual)
        return main_id

    async def _verify_proxy_runtime(self, proxy_id: str) -> None:
        actual = await self._container_runtime(proxy_id)
        if actual is None:
            raise RuntimeError(
                "The gVisor environment could not determine the runtime of the "
                "egress proxy container, so it cannot confirm the proxy stayed "
                "off the sandbox runtime."
            )
        if self._runtime_matches(actual):
            raise RuntimeError(
                f"The trusted egress proxy is running under {actual!r}, "
                "but it must stay on Docker's default runtime: it is the "
                "component that still needs Docker's embedded DNS to resolve "
                "allowlisted hosts."
            )
        self._record_runtime_evidence(EGRESS_PROXY_SERVICE, actual)

    def _record_runtime_evidence(self, service: str, reported: str | None) -> None:
        """Record the engine-reported runtime identity into the trial dir.

        Post-hoc audit of *which* runtime each trial ran under, written only
        after the corresponding verification gate passed. The value is the
        engine's own report verbatim -- a name when the runtime was selected
        by name, a resolved path when selected by path; re-deriving a path
        from a name would re-implement the engine's search order, which
        runtime trust deliberately avoids. Bookkeeping, not a gate: a write
        failure is logged, never fatal, and the file proves nothing an
        attacker in the sandbox could forge -- it is host-side output about
        host-side state.
        """
        path = self.trial_paths.trial_dir / "runtime-verification.json"
        try:
            evidence = json.loads(path.read_text()) if path.exists() else {}
            evidence.setdefault("engine", self._engine_cli)
            evidence.setdefault("expected_runtime", self._runtime)
            evidence.setdefault("services", {})[service] = {
                "reported": reported,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(evidence, indent=2))
        except OSError as exc:
            self.logger.warning(f"Could not record runtime evidence: {exc}")

    def _proxy_token(self) -> str | None:
        """Recover the proxy token from the URL the Docker path already built.

        ``proxy_environment`` renders ``http://agent:<token>@<host>:<port>``.
        Reading the token back keeps ``docker.py`` byte-identical to
        ``origin/edge`` instead of adding a field there for gVisor's benefit.
        """
        url = (self._egress_proxy_env or {}).get("HTTP_PROXY")
        if not url:
            return None
        return urlparse(url).password

    async def _resolve_proxy_address(self, main_id: str, proxy_id: str) -> None:
        """Point the agent's proxy URL at the proxy's literal IPv4 address.

        The sandbox cannot use Compose service-name resolution, so the trusted
        control plane resolves the address after startup instead. Docker keeps
        allocating the project network normally -- no pinned subnet, no fixed
        address, so concurrent trials cannot collide.
        """
        token = self._proxy_token()
        if not self._egress_proxy_env or token is None:
            return

        address = shared_network_ipv4(
            await self._container_networks(proxy_id),
            await self._container_networks(main_id),
        )
        if not address:
            raise RuntimeError(
                "Could not determine the egress proxy's address on the network "
                "it shares with the 'main' container. gVisor cannot use "
                "Docker's embedded DNS (google/gvisor#7469), so a reachable "
                "proxy IP is required."
            )

        self._egress_proxy_env = proxy_environment(token, address, EGRESS_PROXY_PORT)

    # -- unrestricted-internet networking ----------------------------------

    def _require_runtime_verified(self, what: str) -> None:
        if self._state is not VerificationState.RUNTIME_VERIFIED:
            raise RuntimeError(
                f"Refusing to {what}: the sandbox runtime has not been verified "
                f"(state {self._state.value!r})."
            )

    async def _normalize_sandbox_dns(self) -> None:
        """Repair the sandbox's resolver, but only when it is unusable.

        Applies to unrestricted-internet mode only. The no-network path has no
        resolver to fix, and in allowlist mode the trusted proxy resolves
        hostnames on the sandbox's behalf, so rewriting ``resolv.conf`` there
        would create direct DNS egress that the policy does not grant.
        """
        self._require_runtime_verified("rewrite the sandbox resolver")

        read = await self.exec(network.resolv_conf_read_command(), user="root")
        if read.return_code != 0:
            raise RuntimeError(
                "The gVisor environment could not read "
                f"{network.SANDBOX_RESOLV_PATH} inside the sandbox "
                f"(exit code {read.return_code}): "
                f"{read.stderr or read.stdout or 'no output'}"
            )

        current = read.stdout or ""
        if not network.needs_normalization(current):
            return

        _, preserved = network.parse_resolv_conf(current)
        content = network.render_resolv_conf(self._resolvers, preserved)
        write = await self.exec(network.resolv_conf_write_command(content), user="root")
        if write.return_code != 0:
            raise RuntimeError(
                "The gVisor environment could not rewrite "
                f"{network.SANDBOX_RESOLV_PATH} inside the sandbox "
                f"(exit code {write.return_code}): "
                f"{write.stderr or write.stdout or 'no output'}"
            )

        confirm = await self.exec(network.resolv_conf_read_command(), user="root")
        if confirm.return_code != 0 or network.needs_normalization(
            confirm.stdout or ""
        ):
            raise RuntimeError(
                "The gVisor environment rewrote "
                f"{network.SANDBOX_RESOLV_PATH} but the sandbox still reports no "
                "usable nameserver. Refusing to start a task whose network "
                "policy cannot be enforced."
            )

    async def _probe_connectivity(self) -> None:
        """Prove hostname resolution *and* outbound TCP from inside the sandbox.

        "The container started" is not evidence that an unrestricted-internet
        task can reach the internet. A failure tears the environment down rather
        than quietly downgrading to a weaker network mode.
        """
        self._require_runtime_verified("probe outbound connectivity")

        command = network.probe_command(
            self._probe_host, self._probe_port, self._probe_timeout_sec
        )
        result = await self.exec(command, user="root")
        if result.return_code != 0 or network.PROBE_OK_MARKER not in (
            result.stdout or ""
        ):
            raise RuntimeError(
                "The gVisor environment could not reach "
                f"{self._probe_host}:{self._probe_port} from inside the sandbox, "
                "so it cannot confirm that this allow_internet=true task has "
                "working DNS and outbound connectivity. Configure a reachable "
                "resolver with --ek dns=<address>, or a different probe target "
                "with --ek probe_host=<host>. Note that the probe uses bash's "
                "/dev/tcp redirection, so a task image whose bash was built "
                "without net redirections will fail here. Output: "
                f"{result.stdout or result.stderr or 'no output'}"
            )

    # -- teardown ----------------------------------------------------------

    def _cleanup_staging(self) -> None:
        for directory in (self._stage_in, self._stage_out):
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception as exc:  # pragma: no cover - rmtree already lenient
                self.logger.debug(
                    f"Failed to remove gVisor staging directory {directory}: {exc}"
                )

    async def _teardown(self) -> None:
        """Fail-closed teardown, verified against this exact Compose project.

        Never routes through :meth:`_ensure_verified` (the exec gate): a
        container too untrusted to run a command in is still safe to inspect
        and remove from the host side, and gating cleanup on verification
        would make a failed-verification environment untearable.

        Idempotent and safe under repeated calls, concurrent callers and
        cancellation. ``_torn_down`` is set only after a fresh query confirms
        no container or network labeled for this exact project remains --
        never optimistically beforehand -- so an earlier incomplete cleanup
        never makes a later call here (or a later ``start()`` attempt on this
        same instance) a silent no-op while this project's resources still
        exist.
        """
        async with self._teardown_lock:
            if self._torn_down:
                return

            try:
                result = await self._run_docker_compose_command(
                    ["down", "--remove-orphans"], check=False
                )
            except Exception as exc:
                self.logger.warning(
                    f"docker compose down for gVisor project "
                    f"{self._project_name!r} raised {exc!r}; falling back to "
                    "targeted cleanup by exact project label."
                )
            else:
                if result.return_code != 0:
                    self.logger.warning(
                        "docker compose down returned "
                        f"{result.return_code} for gVisor project "
                        f"{self._project_name!r}: "
                        f"{result.stderr or result.stdout or 'no output'}. "
                        "Falling back to targeted cleanup by exact project "
                        "label."
                    )

            # Containers first: a network cannot be removed while a container
            # from this project is still attached to it.
            containers = await self._query_project_container_ids()
            if containers:
                await self._remove_containers(containers)

            networks = await self._query_project_network_ids()
            if networks:
                await self._remove_networks(networks)

            self._cleanup_staging()

            remaining_containers = await self._query_project_container_ids()
            remaining_networks = await self._query_project_network_ids()
            if remaining_containers or remaining_networks:
                raise RuntimeError(
                    "The gVisor environment could not fully clean up Compose "
                    f"project {self._project_name!r}: "
                    f"{len(remaining_containers)} container(s) "
                    f"{remaining_containers} and {len(remaining_networks)} "
                    f"network(s) {remaining_networks} remain. Remove them "
                    f"manually (e.g. '{self._engine_cli} rm --force <id>' and "
                    f"'{self._engine_cli} network rm <id>') before retrying this trial."
                )

            self._torn_down = True
