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

* **Exec rides a mailbox.** The libkrun handler does not implement exec.
  The trial only uses exec as a serial chain, so this flavor overrides
  ``exec()`` with a file protocol over the staging mounts: the host writes
  command files, a runner loop (installed as ``main``'s command in place
  of the ``sleep infinity`` keepalive) executes them, and result files
  come back. Interactive ``attach`` is not supported. The probe record in
  KRUN-PODMAN.md §5 carries the measurements behind this design.

Everything else stays inherited: the staging transfer pipeline runs
unchanged on top of the mailbox exec, and the network wiring is required
under krun for a new reason (TSI bypasses aardvark names; the proxy is
addressed by literal IP, so the sandbox never needs DNS).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shlex
import uuid
from pathlib import Path

from titanium.environments.base import ExecResult
from titanium.environments.gvisor.podman import GVisorPodmanEnvironment
from titanium.environments.gvisor.podman_runtime import assert_runtime_digest
from titanium.environments.gvisor.runtime import STAGE_IN, STAGE_OUT
from titanium.models.environment_type import EnvironmentType

# The runtime name scripts/init/krun-podman.sh registers with Podman.
DEFAULT_RUNTIME = "krun"

# Where scripts/init/krun-podman.sh records `<sha3-512>  <path>` for the
# dnf-installed krun binary (trust-on-first-use). Separate from the runsc
# pin. Overridable so deployments and tests can relocate the pin.
KRUN_DIGEST_PIN = "/usr/local/share/titanium/krun.sha3-512"

KRUN_INIT_SCRIPT = "scripts/init/krun-podman.sh"

# Tightened seccomp profile for the VMM process, applied through the
# compose override. Subtractive: podman's default profile (which the
# probe record confirms already filters the VMM) minus unconditional
# allowances a VMM never needs after crun's setup -- ptrace,
# process_vm_readv/writev, keyctl, memfd_secret, mount, umount, umount2,
# pivot_root, unshare, setns. Generated from containers-common's
# /usr/share/containers/seccomp.json (Fedora 44, 2026-08-26); regenerate
# by re-running the subtraction against a newer default. The battery
# validates boot, virtiofs, vsock, and TSI egress under it.
KRUN_SECCOMP_PROFILE = Path(__file__).parent / "seccomp.json"

# The mailbox: the libkrun handler has no exec, so commands travel as
# files. The host writes command files under the read-only staging mount;
# the runner (main's command) executes them and writes results under the
# writable one. Both channels are measured coherent within ~1s while the
# guest runs (KRUN-PODMAN.md §5). Namespaced subdirectories keep the
# mailbox clear of the staging transfer files that share the mounts.
MAILBOX_DIR_NAME = ".mbox"
MAILBOX_CMD_DIR = STAGE_IN / MAILBOX_DIR_NAME
MAILBOX_RES_DIR = STAGE_OUT / MAILBOX_DIR_NAME
MAILBOX_RUNNER_NAME = "runner.sh"

# How long the first exec waits for the runner's alive marker. Overridable
# for tests. The wait covers VM boot plus the first loop turn.
MAILBOX_ALIVE_TIMEOUT_ENV = "TITANIUM_KRUN_MAILBOX_ALIVE_TIMEOUT"
MAILBOX_ALIVE_TIMEOUT_SEC = 60.0

# Poll interval for result files. The runner's own poll is 0.2s.
_MAILBOX_POLL_SEC = 0.1

# The grace the host adds on top of a command's own timeout before it
# declares the guest unresponsive. The guest-side `timeout` is the real
# limit; the host margin only covers scheduling and poll latency.
_MAILBOX_TIMEOUT_GRACE_SEC = 30.0

# The runner. Plain sh and coreutils only: it must run in any image that
# survives the `sleep infinity` keepalive it replaces. The exit file is
# written last and renamed into place, so the host never reads a result
# before stdout is complete. Lexicographic glob order equals submission
# order because command ids carry a zero-padded sequence number.
MAILBOX_RUNNER_SCRIPT = f"""#!/bin/sh
# titanium mailbox runner -- main's command under krun.
# The libkrun handler has no exec. Commands arrive as files on the
# read-only staging mount; results leave on the writable one.
# See docs/environments/KRUN-PODMAN.md section 5.
CMD={MAILBOX_CMD_DIR}
RES={MAILBOX_RES_DIR}
mkdir -p "$RES"
: > "$RES/.runner-alive"
while true; do
  for f in "$CMD"/cmd-*.sh; do
    [ -f "$f" ] || continue
    id=${{f##*/}}
    id=${{id%.sh}}
    [ -f "$RES/$id.exit" ] && continue
    sh "$f" > "$RES/$id.out" 2>&1
    rc=$?
    echo "$rc" > "$RES/$id.exit.tmp"
    mv "$RES/$id.exit.tmp" "$RES/$id.exit"
  done
  sleep 0.2 2>/dev/null || sleep 1
done
"""


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
        # Mailbox state; _prepare_gvisor refreshes both per start attempt.
        self._mailbox_seq = itertools.count()
        self._mailbox_lock = asyncio.Lock()

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

    # -- mailbox exec ------------------------------------------------------
    #
    # The libkrun handler does not implement exec, and the trial only uses
    # exec as a serial chain (KRUN-PODMAN.md §5 row 0). The mailbox carries
    # that chain over the staging mounts. Trust is unchanged from exec: the
    # guest cooperates or lies, so nothing evidentiary rides this channel
    # and every verification gate stays host-side.

    def _main_command(self) -> list[str] | None:
        # The runner ships on the read-only staging mount; `sh <path>` needs
        # no execute bit there.
        return ["sh", str(MAILBOX_CMD_DIR / MAILBOX_RUNNER_NAME)]

    def _main_user(self) -> str | None:
        # Root, so the runner can serve the user="root" commands transfers
        # and setup rely on, and drop with `su` when a command asks for a
        # different user. The residual: images without `su` fail non-root
        # exec requests loudly.
        return "0"

    def _main_annotations(self) -> dict[str, str] | None:
        # Size the guest explicitly, straight to the handler, through the
        # krun.* annotations (highest precedence in crun's krun handler).
        # Without krun.cpus the guest sees the host's cores (capped at 16)
        # regardless of the task's declaration, so thread pools sized by
        # core count oversubscribe against the cgroup quota. Without
        # krun.ram_mib the guest RAM rides the OCI memory limit — an
        # implicit dependency this makes explicit. Tasks that declare
        # nothing fall to the handler defaults, and that is recorded in
        # KRUN-PODMAN.md §2.8.
        annotations: dict[str, str] = {}
        if self.task_env_config.cpus:
            annotations["krun.cpus"] = str(int(self.task_env_config.cpus))
        if self.task_env_config.memory_mb:
            annotations["krun.ram_mib"] = str(int(self.task_env_config.memory_mb))
        return annotations or None

    def _extra_security_opt(self) -> list[str]:
        # The VMM is the host-facing attack surface; see the profile's
        # provenance comment above.
        return [f"seccomp={KRUN_SECCOMP_PROFILE}"]

    def _main_stop_signal(self) -> str | None:
        # Signals stop at the VMM: the guest never sees SIGTERM, so the
        # default stop grace is a 10-second dead wait before the SIGKILL
        # that was always coming. Say so up front.
        return "SIGKILL"

    def _prepare_gvisor(self) -> None:
        host_cmd_dir = self._stage_in / MAILBOX_DIR_NAME
        host_res_dir = self._stage_out / MAILBOX_DIR_NAME
        host_cmd_dir.mkdir(parents=True, exist_ok=True)
        host_res_dir.mkdir(parents=True, exist_ok=True)
        (host_cmd_dir / MAILBOX_RUNNER_NAME).write_text(MAILBOX_RUNNER_SCRIPT)
        self._mailbox_seq = itertools.count()
        self._mailbox_lock = asyncio.Lock()
        super()._prepare_gvisor()

    def _render_command_file(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        timeout_sec: int | None,
        user: str | int | None,
    ) -> str:
        lines = ["#!/bin/sh"]
        for key, value in env.items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        if cwd:
            quoted = shlex.quote(cwd)
            lines.append(f'cd {quoted} || {{ echo "mailbox: cd {quoted} failed"; exit 125; }}')
        inner = f"bash -c {shlex.quote(command)}"
        if user is not None and str(user) not in ("root", "0"):
            # -p preserves the exported environment across the drop.
            inner = f"su -p -s /bin/sh {shlex.quote(str(user))} -c {shlex.quote(inner)}"
        if timeout_sec:
            inner = f"timeout {int(timeout_sec)} {inner}"
        lines.append(f"exec {inner}")
        return "\n".join(lines) + "\n"

    async def _await_mailbox_file(self, path, deadline: float | None):
        while not path.exists():
            if deadline is not None and asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(_MAILBOX_POLL_SEC)
        return True

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        # The same gate as the inherited exec: verification first, and the
        # verifying task itself passes straight through.
        await self._ensure_verified()

        user = self._resolve_user(user)
        env = self._merge_env(env) or {}
        effective_cwd = cwd or self.task_env_config.workdir

        host_cmd_dir = self._stage_in / MAILBOX_DIR_NAME
        host_res_dir = self._stage_out / MAILBOX_DIR_NAME
        loop = asyncio.get_running_loop()

        async with self._mailbox_lock:
            # Fail fast and clearly when the runner never came up, instead
            # of polling a dead mailbox forever.
            alive_timeout = float(
                os.environ.get(MAILBOX_ALIVE_TIMEOUT_ENV, MAILBOX_ALIVE_TIMEOUT_SEC)
            )
            alive = await self._await_mailbox_file(
                host_res_dir / ".runner-alive", loop.time() + alive_timeout
            )
            if not alive:
                raise RuntimeError(
                    "The krun mailbox runner did not come up within "
                    f"{alive_timeout:.0f}s: no {MAILBOX_DIR_NAME}/.runner-alive "
                    "marker appeared on the staging mount. The sandbox is "
                    "running without its exec channel; refusing to continue."
                )

            cmd_id = f"cmd-{next(self._mailbox_seq):06d}-{uuid.uuid4().hex[:8]}"
            script = self._render_command_file(
                command, effective_cwd, env, timeout_sec, user
            )
            # Atomic publish: the runner globs *.sh, so the temp name is
            # invisible until the rename.
            tmp_path = host_cmd_dir / f"{cmd_id}.tmp"
            tmp_path.write_text(script)
            tmp_path.replace(host_cmd_dir / f"{cmd_id}.sh")

            deadline = None
            if timeout_sec:
                deadline = loop.time() + timeout_sec + _MAILBOX_TIMEOUT_GRACE_SEC
            done = await self._await_mailbox_file(
                host_res_dir / f"{cmd_id}.exit", deadline
            )
            if not done:
                raise RuntimeError(f"Command timed out after {timeout_sec} seconds")

            return_code = int(
                (host_res_dir / f"{cmd_id}.exit").read_text().strip() or "1"
            )
            stdout = ""
            out_path = host_res_dir / f"{cmd_id}.out"
            if out_path.exists():
                stdout = out_path.read_text(errors="replace")

        # The guest-side `timeout` wrapper exits 124. Mirror the inherited
        # timeout behavior: raise, never hand back a silent partial result.
        if timeout_sec and return_code == 124:
            raise RuntimeError(f"Command timed out after {timeout_sec} seconds")

        # Output arrives merged, like the inherited exec path, which pipes
        # stderr into stdout at the subprocess level.
        return ExecResult(stdout=stdout, stderr=None, return_code=return_code)

    async def attach(self) -> None:
        raise NotImplementedError(
            "krun-podman is batch-only: the libkrun handler has no exec, so "
            "there is no interactive attach. Inspect the trial with logs and "
            "artifacts instead."
        )
