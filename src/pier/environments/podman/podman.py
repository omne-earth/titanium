"""Podman environment for Pier — no Docker API socket anywhere in the path.

``docker compose`` speaks the Docker HTTP API, which against Podman means
``podman system service`` + ``DOCKER_HOST``, i.e. a socket. ``podman-compose``
emits ``podman`` CLI calls against libpod directly, so this class swaps the
compose provider and patches the places where DockerEnvironment assumes the
Docker CLI specifically.

Environment knobs (all optional):

    PIER_PODMAN_BIN         podman binary            (default: podman)
    PIER_PODMAN_COMPOSE     compose provider + args  (default: podman-compose)
    PIER_PODMAN_IN_POD      --in-pod value           (default: false)
    PIER_PODMAN_RUN_ARGS    extra args for podman run
    PIER_PODMAN_CGROUP_FAIL_CLOSED
        report no limit support when cgroup state cannot be queried
        (default: assume the common modern case and report support)
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from pier.environments.base import ExecResult
from pier.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from pier.environments.docker.docker import (
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)
from pier.environments.podman.podman_unix import PodmanUnixOps
from pier.models.environment_type import EnvironmentType
from pier.models.trial.config import ResourceMode

# podman-compose stamps both the docker-compatible labels and its own
# namespaced set, depending on version; try them in order.
_PROJECT_LABELS = ("com.docker.compose.project", "io.podman.compose.project")
_SERVICE_LABELS = ("com.docker.compose.service", "io.podman.compose.service")


def _which_compose(name: str) -> str | None:
    """PATH first (so PIER_PODMAN_COMPOSE overrides behave normally), then the
    interpreter's bin dir — podman-compose is a pier dependency, so it sits in
    ``.venv/bin``, which is not on PATH when pier is invoked as
    ``.venv/bin/pier``."""
    return shutil.which(name) or shutil.which(
        name, path=os.path.dirname(sys.executable)
    )


class PodmanEnvironment(DockerEnvironment):
    """DockerEnvironment with the container runtime CLI swapped for Podman."""

    @staticmethod
    def type() -> str:
        return EnvironmentType.PODMAN.value

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # Same as Docker except Windows containers, which Podman cannot run.
        return EnvironmentCapabilities(
            disable_internet=True,
            filtered_egress=True,
            preinstall_agents=True,
            windows=False,
            mounted=True,
            docker_compose=True,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        """Rootless Podman enforces `--cpus`/`--memory` only on cgroups v2 with
        the controllers delegated; otherwise it warns and silently drops the
        limit. Reporting False makes Pier reject LIMIT/GUARANTEE tasks up front
        instead of running them unbounded."""
        cpu, memory = cls._cgroup_controllers()
        return EnvironmentResourceCapabilities(cpu_limit=cpu, memory_limit=memory)

    @staticmethod
    def _cgroup_controllers() -> tuple[bool, bool]:
        podman = os.environ.get("PIER_PODMAN_BIN", "podman")
        try:
            info = subprocess.run(
                [
                    podman,
                    "info",
                    "--format",
                    "{{.Host.CgroupsVersion}}|{{.Host.CgroupControllers}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.strip()
        except Exception:
            # Can't tell. The default assumes the common modern case rather
            # than blocking; deployments that prefer refusal over a possibly
            # unbounded run opt into failing closed.
            if os.environ.get("PIER_PODMAN_CGROUP_FAIL_CLOSED", "").lower() in (
                "1",
                "true",
                "yes",
            ):
                return False, False
            return True, True

        version, _, controllers = info.partition("|")
        if version.strip() != "v2":
            # cgroups v1 rootless cannot enforce either limit.
            return False, False
        return "cpu" in controllers, "memory" in controllers

    # ------------------------------------------------------------------ setup

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        if getattr(self, "_is_windows_container", False):
            raise RuntimeError(
                "The podman environment supports Linux containers only; this task "
                "declares [environment].os = 'windows'."
            )

        self.podman_bin = os.environ.get("PIER_PODMAN_BIN", "podman")
        self.compose_cmd = shlex.split(
            os.environ.get("PIER_PODMAN_COMPOSE", "podman-compose")
        )
        self.compose_cmd[0] = _which_compose(self.compose_cmd[0]) or self.compose_cmd[0]
        self.in_pod = os.environ.get("PIER_PODMAN_IN_POD", "false")
        self.run_args = os.environ.get("PIER_PODMAN_RUN_ARGS", "")

        self._apply_selinux_relabel()

        # podman-compose has no `cp` subcommand, so file transfer goes through
        # `podman cp` against the resolved container instead.
        self._platform = PodmanUnixOps(self)

    def _apply_selinux_relabel(self) -> None:
        """Podman does not relabel bind mounts, so on an enforcing host the
        agent dies writing to the mounted log dirs; `bind.selinux` makes Podman
        relabel at container start (ignored on non-SELinux hosts). `z` not `Z`:
        a separate-mode verifier container shares the same artifact dirs, and a
        private category would lock it out."""
        relabel = os.environ.get("PIER_PODMAN_SELINUX_RELABEL", "z")
        if relabel not in ("z", "Z"):
            return
        for mount in self._mounts_json or []:
            if mount.get("type") == "bind":
                mount.setdefault("bind", {}).setdefault("selinux", relabel)

    @classmethod
    def preflight(cls) -> None:
        podman = os.environ.get("PIER_PODMAN_BIN", "podman")
        compose = shlex.split(os.environ.get("PIER_PODMAN_COMPOSE", "podman-compose"))

        if not shutil.which(podman):
            raise SystemExit(
                f"{podman!r} is not installed or not on PATH. Install Podman and "
                "try again."
            )
        if not _which_compose(compose[0]):
            raise SystemExit(
                f"{compose[0]!r} is not on PATH. Install it with:\n"
                "  uv tool install podman-compose   # or: pip install podman-compose\n"
                "Note: the Go `docker compose` plugin will NOT work without a "
                "Podman API socket, which is exactly what this environment avoids."
            )
        try:
            # `podman info` talks to libpod in-process — no socket or service.
            subprocess.run(
                [podman, "info"], capture_output=True, timeout=30, check=True
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip()
            raise SystemExit(f"`{podman} info` failed:\n{detail}")
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"`{podman} info` timed out — first run may be initialising "
                "storage. Run it manually once, then retry."
            )

    # ---------------------------------------------------- limit verification

    # Verify enforcement, not configuration: Podman accepts `--cpus`/`--memory`
    # and then, on hosts that cannot enforce them (rootless v1, undelegated
    # controllers, runsc-in-userns silently ignoring cgroup errors), drops the
    # limit without failing the start. Reading the container's own cgroup files
    # from the host is the only signal that reflects what is actually applied.
    _CGROUP_FS = Path("/sys/fs/cgroup")

    async def start(self, force_build: bool):
        await super().start(force_build)
        await self._verify_resource_limits()

    async def _verify_resource_limits(self) -> None:
        checks: list[tuple[str, ResourceMode, int]] = []
        cpu_limit = self._resource_limit_value("cpu", auto_mode=ResourceMode.LIMIT)
        if cpu_limit is not None:
            checks.append(("cpu", self._cpu_resource_mode, cpu_limit))
        memory_limit = self._resource_limit_value(
            "memory", auto_mode=ResourceMode.LIMIT
        )
        if memory_limit is not None:
            checks.append(("memory", self._memory_resource_mode, memory_limit))
        if not checks:
            return

        cgroup_dir = await self._main_cgroup_dir()
        for resource, mode, declared in checks:
            problem = (
                self._cgroup_limit_problem(cgroup_dir, resource, declared)
                if cgroup_dir is not None
                else "the container's cgroup path could not be resolved"
            )
            if problem is None:
                continue
            message = (
                f"Declared {resource} limit is not enforced for "
                f"{self.environment_name}: {problem}. The workload would run "
                "unbounded."
            )
            if mode in (ResourceMode.LIMIT, ResourceMode.GUARANTEE):
                raise RuntimeError(message)
            self.logger.warning(message)

    async def _main_cgroup_dir(self) -> Path | None:
        try:
            container = await self.resolve_container("main")
            result = await self._podman(
                ["inspect", "--format", "{{.State.CgroupPath}}", container],
                check=False,
            )
        except Exception:
            return None
        if result.return_code != 0:
            return None
        cgroup_path = (result.stdout or "").strip()
        if not cgroup_path.startswith("/"):
            return None
        cgroup_dir = self._CGROUP_FS / cgroup_path.lstrip("/")
        return cgroup_dir if cgroup_dir.is_dir() else None

    @staticmethod
    def _cgroup_limit_problem(
        cgroup_dir: Path, resource: str, declared: int
    ) -> str | None:
        """None when the cgroup enforces *declared*, else what is wrong.

        `declared` is CPUs for cpu and MB for memory. Comparison allows 10%
        for unit rounding between compose byte-suffix parsing and the kernel;
        the failure mode being caught is a dropped limit ("max"), not an
        off-by-a-few-bytes one.
        """
        filename = "cpu.max" if resource == "cpu" else "memory.max"
        try:
            raw = (cgroup_dir / filename).read_text().strip()
        except OSError as exc:
            return f"{filename} is not readable under {cgroup_dir} ({exc})"

        if resource == "cpu":
            quota, _, period = raw.partition(" ")
            if quota == "max":
                return f"cpu.max reports no quota ({raw!r})"
            actual = int(quota) / int(period or "100000")
        else:
            if raw == "max":
                return "memory.max reports no limit ('max')"
            actual = int(raw) / (1024 * 1024)

        if abs(actual - declared) > declared * 0.1:
            return f"{filename} reports {actual:g}, task declares {declared}"
        return None

    async def _chown_to_host_user(self, path: str, recursive: bool = False) -> None:
        """No-op: rootless Podman already maps container root to the invoking
        user, so files arrive correctly owned. The parent's chown to
        ``os.getuid()`` would resolve through the userns into the subuid range,
        leaving artifacts the host cannot write. (Recover already-broken dirs
        with ``podman unshare chown -R 0:0 <dir>``.)"""
        return

    @staticmethod
    def _detect_daemon_os() -> str | None:
        """Podman's info schema has no OSType field, so the parent's
        ``docker info --format {{.OSType}}`` would return empty and trip the
        daemon-mode validation. Podman only runs Linux containers."""
        return "linux"

    # --------------------------------------------------------------- commands

    def _compose_base(self) -> list[str]:
        cmd = list(self.compose_cmd)

        # A shared pod breaks allow_internet=false tasks: `main` runs with
        # network_mode: none while the egress proxy still needs a net.
        if self.in_pod:
            cmd.append(f"--in-pod={self.in_pod}")
        if self.run_args:
            # '=' required: argparse won't take a '-'-leading token as a value.
            cmd.append(f"--podman-run-args={self.run_args}")

        cmd.extend(["--project-name", self._project_name])
        for path in self._docker_compose_paths:
            cmd.extend(["-f", str(path.resolve().absolute())])
        return cmd

    @property
    def _project_name(self) -> str:
        return _sanitize_docker_compose_project_name(self.session_id)

    def _compose_env(self) -> dict[str, str]:
        env = self._env_vars.to_env_dict(include_os_env=True)
        if self._compose_task_env:
            env.update(self._compose_task_env)
        if self._persistent_env:
            env.update(self._persistent_env)
        # Guarantee nothing downstream falls back to a Docker socket.
        env.pop("DOCKER_HOST", None)
        return env

    async def _run_docker_compose_command(
        self, command: list[str], check: bool = True, timeout_sec: int | None = None
    ) -> ExecResult:
        """Parent's contract retargeted at podman-compose, which has no
        ``--project-directory`` — the subprocess cwd stands in for it. Every
        other flag Pier uses is supported as-is.

        Programmatic execs run with ``-T``: podman-compose passes ``--tty``
        otherwise, and pty line discipline rewrites ``\\n`` to ``\\r\\n`` and
        reorders stream interleaving in captured output. crun tolerates the
        pty (runsc rejects it outright), but transcripts should be pipe-clean
        either way. Interactive ``attach`` builds its own command and keeps
        its TTY."""
        if command and command[0] == "exec" and "-T" not in command:
            command = ["exec", "-T", *command[1:]]
        full_command = self._compose_base() + list(command)

        process = await asyncio.create_subprocess_exec(
            *full_command,
            env=self._compose_env(),
            cwd=str(self.environment_dir.resolve().absolute()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            if timeout_sec:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_sec
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.TimeoutError:
            process.terminate()
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=5
                )
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")

        result = ExecResult(
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else None,
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else None,
            return_code=process.returncode or 0,
        )

        if check and result.return_code != 0:
            raise RuntimeError(
                f"podman-compose command failed for environment "
                f"{self.environment_name}. "
                f"Command: {' '.join(full_command)}. "
                f"Return code: {result.return_code}. "
                f"Stdout: {result.stdout}. "
                f"Stderr: {result.stderr}."
            )
        return result

    async def _podman(
        self, args: list[str], check: bool = True, timeout_sec: int | None = None
    ) -> ExecResult:
        """Run a raw `podman` command (used for cp / inspect)."""
        process = await asyncio.create_subprocess_exec(
            self.podman_bin,
            *args,
            env=self._compose_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await (
            asyncio.wait_for(process.communicate(), timeout=timeout_sec)
            if timeout_sec
            else process.communicate()
        )
        result = ExecResult(
            stdout=stdout_bytes.decode(errors="replace") if stdout_bytes else None,
            stderr=stderr_bytes.decode(errors="replace") if stderr_bytes else None,
            return_code=process.returncode or 0,
        )
        if check and result.return_code != 0:
            raise RuntimeError(
                f"podman {' '.join(args)} failed ({result.return_code}): "
                f"{result.stderr or result.stdout}"
            )
        return result

    # -------------------------------------------------------------- container

    async def resolve_container(self, service: str = "main") -> str:
        """Container ID for *service*, resolved by label rather than name so it
        survives podman-compose's naming convention changing."""
        for project_label, service_label in zip(_PROJECT_LABELS, _SERVICE_LABELS):
            result = await self._podman(
                [
                    "ps",
                    "--all",
                    "--filter",
                    f"label={project_label}={self._project_name}",
                    "--filter",
                    f"label={service_label}={service}",
                    "--format",
                    "{{.ID}}",
                ],
                check=False,
            )
            ids = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            if ids:
                return ids[0]

        raise RuntimeError(
            f"No container found for service {service!r} in project "
            f"{self._project_name!r}. Is the environment started?"
        )

    async def podman_cp(self, source: str, target: str) -> None:
        await self._podman(["cp", source, target])

    # -------------------------------------------------------------- overrides

    async def _validate_image_os(self, image_name: str) -> None:
        """Parent shells out to ``docker inspect``; use ``podman image
        inspect``. Advisory only — failures are swallowed like the parent's."""
        try:
            result = await self._podman(
                ["image", "inspect", "--format", "{{.Os}}", image_name], check=False
            )
        except Exception as exc:
            self.logger.debug(f"Skipping image OS validation for {image_name}: {exc}")
            return

        if result.return_code != 0:
            self.logger.debug(
                f"Skipping image OS validation for {image_name}: "
                f"{result.stderr or result.stdout}"
            )
            return

        image_os = (result.stdout or "").strip().lower()
        if image_os and image_os != "linux":
            raise RuntimeError(
                f"Image {image_name!r} targets {image_os!r}; the podman "
                "environment supports Linux images only."
            )

    async def attach(self) -> None:
        variables = " ".join(
            f"export {k}={shlex.quote(str(v))}"
            for k, v in self._env_vars.to_env_dict(include_os_env=False).items()
        )
        base = [shlex.quote(part) for part in self._compose_base()]
        os.execvp(
            "bash",
            [
                "bash",
                "-c",
                f"cd {shlex.quote(str(self.environment_dir.resolve()))}; "
                f"{variables}; "
                + " ".join(base + ["exec", "main", "bash"])
                + "; "
                + " ".join(base + ["down"]),
            ],
        )
