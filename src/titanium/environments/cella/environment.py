"""The Cella environment: ``--env cella``.

Cella is not an OCI runtime and this class does not pretend otherwise. A
machine is staged from Cella-owned golden kernel/rootfs flavors, booted, and
observed; there is no image build, no bind mount, and no ``exec`` into a
running guest. The host-driven transfer verbs Titanium's other environments
rely on therefore refuse loudly instead of being simulated -- see
``_SEALED_GUEST_MESSAGE``.

What this class owns is the projection and the process boundary:

  * the task's ``environment/cella.toml`` declaration;
  * refusing the task shapes Cella cannot honour that no central validator
    already refuses;
  * the create/start/stop/destroy verbs, exactly as the current ``cella`` CLI
    spells them;
  * the trial-scoped ``CELLA_HOME`` every invocation runs under.

Machine identity itself is not computed here. It comes from
:func:`titanium.environments.cella.spec.build_machine_spec`, and the resulting
``CellaMachineSpec`` is the single source of truth for the name, both flavors,
memory, net, and ``CELLA_HOME``.

Environment knobs (all optional):

    TITANIUM_CELLA_BIN      cella binary   (default: cella)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from pydantic import ValidationError

from titanium.environments.base import BaseEnvironment, ExecResult
from titanium.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from titanium.environments.cella.definition import CellaEnvironmentDefinition
from titanium.environments.cella.spec import CellaMachineSpec, build_machine_spec
from titanium.models.environment_type import EnvironmentType

CELLA_DEFINITION_FILENAME = "cella.toml"

# The table `environment/cella.toml` carries, per the definition model.
CELLA_DEFINITION_TABLE = "cella"

# `machine::home` resolves the golden store *and* the machine registry under
# one root, so a trial-scoped CELLA_HOME hides the operator's goldens as well
# as their machines. These two axes are linked back in; `machines/` stays
# trial-private, which is what the isolation is for.
CELLA_GOLDEN_AXES = ("kernel", "rootfs")

_SEALED_GUEST_MESSAGE = (
    "The cella environment is guest-sealed: a machine boots from a "
    "Cella-owned golden rootfs and is observed after it stops, so there is "
    "no host-driven {verb} into a running guest. Task and verifier content "
    "belongs in the rootfs flavor (`cella build rootfs <flavor>`), and "
    "results are recovered post-mortem with `cella inspect`."
)


def cella_bin() -> str:
    """The ``cella`` binary this host should drive."""
    return os.environ.get("TITANIUM_CELLA_BIN", "cella")


def operator_cella_home() -> str | None:
    """The Cella home holding the goldens, mirroring Cella's own ``machine::home``.

    ``None`` when ``$HOME`` is unset and no ``CELLA_HOME`` is exported, which
    is the one case where Cella's rule has no answer either.
    """
    home = os.environ.get("CELLA_HOME")
    if home:
        return home
    base = os.environ.get("HOME")
    if not base:
        return None
    return str(Path(base) / ".cella")


class CellaEnvironment(BaseEnvironment):
    """One trial, one Cella microVM."""

    @staticmethod
    def type() -> EnvironmentType:
        return EnvironmentType.CELLA

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # `--net none` is the only network shape v1 offers, so isolation is
        # the capability Cella has; every other flag here describes an OCI
        # affordance Cella does not have at all.
        return EnvironmentCapabilities(disable_internet=True)

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        """``--mem-mb`` is the guest's physical memory, which is a hard ceiling.

        There is no CPU knob: `cella create` takes no ``--cpus`` and the guest
        gets one vCPU, so a task asking Titanium to enforce a CPU policy is
        refused up front rather than run unbounded.
        """
        return EnvironmentResourceCapabilities(memory_limit=True)

    @classmethod
    def preflight(cls) -> None:
        """Run Cella's own host check before any trial is queued."""
        binary = cella_bin()
        if not shutil.which(binary):
            raise SystemExit(
                f"{binary!r} is not installed or not on PATH. Build Cella and "
                "put it on PATH, or point TITANIUM_CELLA_BIN at the binary."
            )
        try:
            result = subprocess.run(
                [binary, "doctor", "check"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"`{binary} doctor check` timed out.")
        if result.returncode != 0:
            detail = (result.stdout or result.stderr or "").strip()
            raise SystemExit(f"`{binary} doctor check` failed:\n{detail}")

    def __init__(self, *args, **kwargs) -> None:
        # Set by _validate_definition, which BaseEnvironment.__init__ calls.
        self._definition: CellaEnvironmentDefinition | None = None

        super().__init__(*args, **kwargs)

        self.cella_bin = cella_bin()
        self.spec: CellaMachineSpec = build_machine_spec(
            session_id=self.session_id,
            environment_name=self.environment_name,
            task_env_config=self.task_env_config,
            definition=self.definition,
            trial_paths=self.trial_paths,
        )

    # -- definition --------------------------------------------------------

    @property
    def _definition_path(self) -> Path:
        return self.environment_dir / CELLA_DEFINITION_FILENAME

    @property
    def definition(self) -> CellaEnvironmentDefinition:
        assert self._definition is not None, "definition is loaded during __init__"
        return self._definition

    def _validate_definition(self) -> None:
        path = self._definition_path
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. A cella task declares its machine in "
                f"{CELLA_DEFINITION_FILENAME} with a [{CELLA_DEFINITION_TABLE}] "
                "table naming the golden kernel and rootfs flavors."
            )

        try:
            document = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} is not valid TOML: {exc}") from exc

        table = document.get(CELLA_DEFINITION_TABLE)
        if table is None:
            raise ValueError(
                f"{path} has no [{CELLA_DEFINITION_TABLE}] table. Declare the "
                'golden flavors there: kernel = "...", rootfs = "...".'
            )

        try:
            self._definition = CellaEnvironmentDefinition(**table)
        except (TypeError, ValidationError) as exc:
            raise ValueError(f"{path} is not a valid cella declaration: {exc}") from exc

        if self._definition.rootfs_digest is not None:
            # Refusing beats accepting a pin nothing checks. `cella doctor
            # verify rootfs <flavor>` only knows Cella's own built-in flavor
            # list and returns 0 for any other name, so a task's digest would
            # be silently unenforced.
            raise ValueError(
                f"{path} sets rootfs_digest, but Titanium cannot verify a "
                "rootfs pin yet: `cella doctor verify rootfs <flavor>` covers "
                "only Cella's built-in flavors. Remove the pin, or wait for a "
                "Cella verify that accepts an expected digest."
            )

        if self.task_env_config.allow_internet:
            # Not covered centrally: _validate_internet_config only refuses
            # allow_internet=False on environments that cannot isolate. A task
            # that *wants* egress gets `--net none` here, which would change
            # the task silently.
            raise ValueError(
                "The cella environment runs machines with --net none: Cella's "
                "membrane is not landed, so a sealed trial has no egress. This "
                "task sets [environment].allow_internet = true. Set it to "
                "false, or use an environment that can provide network access."
            )

    # -- process boundary --------------------------------------------------

    @property
    def cella_home(self) -> Path:
        return self.spec.cella_home

    def cella_process_env(self) -> dict[str, str]:
        """The process environment every ``cella`` invocation for this trial gets."""
        return {**os.environ, "CELLA_HOME": str(self.cella_home)}

    def _create_args(self) -> list[str]:
        """``cella create`` argv, projected from the spec and nothing else."""
        args = [
            "create",
            self.spec.name,
            "--kernel",
            self.spec.kernel_flavor,
            "--rootfs",
            self.spec.rootfs_flavor,
            "--net",
            self.spec.net,
        ]
        if self.spec.mem_mb is not None:
            # None means the task declared no memory; Cella's own default then
            # applies, and Titanium does not restate it.
            args += ["--mem-mb", str(self.spec.mem_mb)]
        return args

    def _prepare_cella_home(self) -> None:
        """Make the trial's registry usable: private machines, shared goldens."""
        self.cella_home.mkdir(parents=True, exist_ok=True)

        source_home = operator_cella_home()
        if source_home is None or Path(source_home) == self.cella_home:
            return
        for axis in CELLA_GOLDEN_AXES:
            link = self.cella_home / axis
            if not link.exists() and not link.is_symlink():
                link.symlink_to(Path(source_home) / axis)

    async def _run_cella(
        self,
        args: list[str],
        *,
        timeout_sec: int | None = None,
    ) -> tuple[int, str]:
        """Run one ``cella`` verb under this trial's CELLA_HOME."""
        process = await asyncio.create_subprocess_exec(
            self.cella_bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.cella_process_env(),
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        output = stdout.decode("utf-8", errors="replace").strip()
        assert process.returncode is not None
        return process.returncode, output

    async def _run_cella_checked(
        self,
        args: list[str],
        *,
        timeout_sec: int | None = None,
    ) -> str:
        return_code, output = await self._run_cella(args, timeout_sec=timeout_sec)
        if return_code != 0:
            raise RuntimeError(
                f"`{self.cella_bin} {' '.join(args)}` failed with code "
                f"{return_code}: {output or 'no output'}"
            )
        return output

    # -- lifecycle ---------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if force_build:
            # Cella owns the golden supply chain: `cella build kernel|rootfs`
            # is the only thing that makes one, and Titanium never calls it.
            self.logger.warning(
                "force_build has no effect on the cella environment: golden "
                "kernel and rootfs flavors are built by `cella build`, not by "
                "Titanium."
            )

        self._prepare_cella_home()
        await self._run_cella_checked(self._create_args(), timeout_sec=300)
        await self._run_cella_checked(["start", self.spec.name], timeout_sec=120)

    async def stop(self, delete: bool):
        # Best-effort: cleanup runs after failures too, and a machine that was
        # never created or never started must not mask the original error.
        return_code, output = await self._run_cella(
            ["stop", self.spec.name], timeout_sec=120
        )
        if return_code != 0:
            self.logger.warning(
                "`%s stop %s` failed with code %s: %s",
                self.cella_bin,
                self.spec.name,
                return_code,
                output or "no output",
            )

        if not delete:
            return

        return_code, output = await self._run_cella(
            ["destroy", self.spec.name], timeout_sec=120
        )
        if return_code != 0:
            self.logger.warning(
                "`%s destroy %s` failed with code %s: %s",
                self.cella_bin,
                self.spec.name,
                return_code,
                output or "no output",
            )

    # -- sealed guest ------------------------------------------------------

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        raise NotImplementedError(_SEALED_GUEST_MESSAGE.format(verb="exec"))

    async def upload_file(self, source_path: Path | str, target_path: str):
        raise NotImplementedError(_SEALED_GUEST_MESSAGE.format(verb="upload"))

    async def upload_dir(self, source_dir: Path | str, target_dir: str):
        raise NotImplementedError(_SEALED_GUEST_MESSAGE.format(verb="upload"))

    async def download_file(self, source_path: str, target_path: Path | str):
        raise NotImplementedError(_SEALED_GUEST_MESSAGE.format(verb="download"))

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        raise NotImplementedError(_SEALED_GUEST_MESSAGE.format(verb="download"))
