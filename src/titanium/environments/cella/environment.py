"""The cella environment: titanium driving cella as its runtime.

This module grows into the ``--env cella`` environment class. Its first
resident is the one decision every cella machine is created under: the
task's ``allow_internet`` flag **defines the network topology**, not a
firewall posture inside one.

``allow_internet`` is harbor's knob -- the task author's declaration,
which every rung must honor however it can. On the cella rung the two
values are two different machines:

- ``false`` -- ``--net none`` for an agentless trial. No nic exists:
  no translator, no membrane traffic, no ledger, and none of the
  judgment machinery.
- ``true`` (or an agent present) -- **the terminated pair**
  (:mod:`titanium.environments.cella.terminator`, cella's
  docs/integration/TLS-TERMINATOR.md). The task machine is a *member*
  on a ``wire:`` to a terminator appliance that holds the world nic;
  both borders are judged by titanium's engine. Open is the membrane,
  not a free path: every crossing parks for a decision.

The two knobs stay orthogonal: harbor's flag decides whether the
task's *own* world domains are granted on the appliance border, and
``cella.policy`` states the rules. An agent always needs its inference
line, so the pair stands whenever an agent is baked *or*
``allow_internet=true``; only an agentless airgapped trial is bare
``--net none``. The appliance grants the agent's inference host
always, and the task's declared egress domains only when
``allow_internet=true`` -- so an airgapped task stays airgapped while
its agent still resolves its API. World egress is judged by the
resolved **name** the appliance stamps on each crossing, never by a
rotating ip.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkTopology:
    """What ``allow_internet`` means at the cella verbs.

    Attributes:
        net: The ``--net`` argument to ``cella create``.
        open_gateway: Whether ``cella gateway <vm> open`` runs after
            start. Meaningless without a nic; load-bearing with one --
            the valve is born closed, and open is what makes crossings
            park instead of nothing moving at all.
        judged: Whether the judgment loop must run for this machine:
            the policy engine serving the task's ``cella.policy``, and
            cella's bridge streaming the parks to it. A ``--net none``
            machine has nothing to judge.
    """

    net: str
    open_gateway: bool
    judged: bool


def network_topology(allow_internet: bool) -> NetworkTopology:
    """The topology the task's ``allow_internet`` declaration defines."""
    if allow_internet:
        return NetworkTopology(net="world", open_gateway=True, judged=True)
    return NetworkTopology(net="none", open_gateway=False, judged=False)


# ---------------------------------------------------------------------------
# The environment class: bake, run, collect -- one cycle per exec
# ---------------------------------------------------------------------------
#
# Cella has no exec-into, by design, so the whole trial is one baked
# experiment (sealed_oneshot): every input -- uploads, the agent's
# command spec, the tests, the guest orchestrator -- enters the boot
# layer, one member boots and runs every phase, and the forced reset
# ends the VMM (the completion signal, host-observed). Results and
# downloads are extracted from the still machine with `cella extract`;
# no guest-produced filesystem is ever mounted or parsed by titanium.

import asyncio
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

from titanium.environments.base import BaseEnvironment, ExecResult
from titanium.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from titanium.environments.cella.boot_layer import (
    BootEntry,
    BootLayer,
    GuestFile,
    GuestSymlink,
)
from titanium.environments.cella.buildfile import (
    discover_build_file,
    prepare_build_context,
)
from titanium.environments.cella.constants import (
    BOOT_MARGIN_SEC,
    CELLA_EXEC_TIMEOUT,
    CELLA_VERB_TIMEOUT_SEC,
    ENGINE_LOG_DRAIN_SEC,
    POWEROFF_GRACE_SEC,
    ROOTFS_ARTIFACT_NAME,
    RUNNER_DIR,
)
from titanium.environments.cella.engine import bound_port, build_judge, serve
from titanium.environments.cella.flavor import (
    cella_home,
    render_golden_json,
    rootfs_flavor_dir,
    staging_flavor_dir,
    write_manifest,
)
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    build_image,
    export_rootfs_tar,
    inspect_image,
    new_build_tag,
    untag_image,
)
from titanium.environments.cella.policy import Policy
from titanium.environments.cella.rootfs import (
    build_ext4,
    sha3_256_file,
)
from titanium.environments.cella.systemd_boot import (
    plan_systemd_provisioning,
    prepare_systemd_rootfs,
)
from titanium.environments.cella.terminator import (
    appliance_border_policy_text,
    member_policy_text,
    member_prelude,
    member_trust_entries,
    pair_ca_path,
)


class _BufferedEngineLog(logging.Handler):
    """The engine's per-machine log sink, kept off the decide hot path.

    Now that cella delivers a park to the judge in ~1ms, a synchronous
    file write per crossing (a plain FileHandler flushes every record)
    would be the throughput ceiling. So ``emit`` only appends the record
    to an in-memory buffer -- microseconds, no I/O -- and a background
    drainer (:meth:`CellaEnvironment._drain_engine_logs`) formats and
    writes the batch once a second, off the engine loop. The buffer is
    FIFO, so the log reads in the exact order the engine judged; the
    verdicts themselves are never batched (the decide loop still answers
    each park in order). ``close`` drains the remainder, so the record is
    complete."""

    def __init__(self, path: Path):
        super().__init__()
        # The path, not a held handle: each drain opens, appends the
        # batch, and closes -- one open per second costs nothing, and
        # no descriptor outlives its write.
        self._path = path
        self._buf: list[logging.LogRecord] = []
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, record: logging.LogRecord) -> None:
        # Stamp the wall-clock now, at the crossing, not at the drain a
        # second later. host_ns (nanoseconds since the epoch) matches
        # cella's audit book, so the two logs read on one clock.
        record.host_ns = time.time_ns()
        with self._lock:
            if not self._closed:
                self._buf.append(record)

    def _drain(self) -> None:
        pending, self._buf = self._buf, []
        if not pending:
            return
        with self._path.open("a", encoding="utf-8") as sink:
            sink.write("".join(self.format(r) + "\n" for r in pending))

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._drain()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._drain()
                self._closed = True
        super().close()


class CellaError(RuntimeError):
    """A cella verb or an evidence read failed."""


def cella_bin() -> str:
    """The cella CLI, resolved as the release install: CELLA_BIN, then
    PATH, then the field install's own home."""
    override = os.environ.get("CELLA_BIN")
    if override:
        return override
    found = shutil.which("cella")
    if found:
        return found
    return str(Path.home() / ".cella" / "bin" / "cella")


class CellaEnvironment(BaseEnvironment):
    """``--env cella``: the sealed-VM rung.

    An agentless ``allow_internet = false`` trial boots ``--net none``
    machines: no nic, no judge. Otherwise the terminated pair stands: a
    member on a wire to a terminator appliance holding the world,
    titanium's engine judging both borders (the member's fixed wire
    grants and the appliance's world leg, by name), and cella's bridge
    streaming every park to it. ``--ek dry_run=true`` flips the
    appliance engine to collection: every world crossing releases and
    its resolved name lands in the task's ``cella.policy`` as a grant.
    """

    def __init__(
        self,
        *args,
        dry_run: bool | str = False,
        on_completion: str = "teardown",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._topology = network_topology(self.task_env_config.allow_internet)
        # --ek values arrive as strings; anything but an explicit yes
        # is enforce mode.
        self._dry_run = str(dry_run).lower() in ("true", "1", "yes")
        # What to do with a machine when its life ends. `teardown` (the
        # absent default) destroys it. `archive` latches it as a cella
        # artifact (`cella archive`) -- a rock: inspectable with `cella
        # inspect`, and forkable back to a runnable machine with `cella
        # branch`, but never thawed (thaw resumes a live frozen machine;
        # archive closes that door). The forensic path for a run you
        # want to hold. Anything but an explicit `archive` is teardown.
        self._on_completion = (
            "archive" if str(on_completion).lower() == "archive" else "teardown"
        )
        # Each engine is an in-process grpclib server (no subprocess):
        # {vm-id: (server, port)}, all hosted on one background asyncio
        # loop thread so they never block the harness's event loop.
        self._engines: dict[str, tuple] = {}
        self._engine_loop: asyncio.AbstractEventLoop | None = None
        self._engine_loop_thread: threading.Thread | None = None
        # Per-machine engine-log sinks and the one drainer that writes their
        # batches to disk every second, off the decide hot path.
        self._engine_log_sinks: dict[str, _BufferedEngineLog] = {}
        self._log_drain_thread: threading.Thread | None = None
        self._log_drain_stop = threading.Event()
        self._work: Path | None = None
        # The terminated pair (terminator.py) stands when the workload
        # needs the world at all: an agent always needs its inference
        # line, and allow_internet=true adds the task's own egress. Only
        # an agentless airgapped trial (--net none) needs no appliance.
        self._agent = self.agent_install_spec is not None
        self._paired = self._agent or self._topology.judged
        self._appliance: str | None = None
        self._appliance_bridge: subprocess.Popen | None = None
        self._appliance_thaw: threading.Thread | None = None
        self._appliance_stop = threading.Event()
        # One lifecycle at a time on this environment. A `to_thread`
        # body cannot be cancelled, so when a verifier `wait_for` times
        # out it abandons the *await* while the worker thread keeps
        # running; the retry then calls exec again. Without this lock the
        # two threads would race on the shared cycle state and disk paths
        # (a FileExistsError on the harvest dir). The lock serializes them
        # per environment; different trials hold different locks and stay
        # concurrent.
        self._lifecycle = threading.Lock()
        # Cycle 0 boots from the prepared tar; every later cycle boots
        # from the previous cycle's evidence disk, edited in place.
        self._base_tar: Path | None = None
        self._pending: list[BootEntry] = []
        self._pending_paths: set[str] = set()
        self._cycle = 0
        # The trial phase the current machines serve (set_phase). Each
        # cycle's machine is named with its phase, so a chronicle reads as
        # setup -> agent -> collect -> verify. Defaults to setup: the boots
        # before the agent runs are the harness preparing the guest.
        self._phase = "setup"
        self._image_config: dict = {}
        self._machine: str | None = None
        # After the sealed boot ends, the halted member is the evidence:
        # downloads extract from it (once per directory, cached), and
        # stop(delete) retires it.
        self._evidence_machine: str | None = None
        self._evidence_cache: dict[str, Path] = {}
        # Machines whose host-side books mirror live into the trial dir
        # (the drain thread copies changed files once a second).
        self._mirrored: dict[str, Path] = {}

    @staticmethod
    def type() -> str:
        return "cella"

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            disable_internet=True,
            preinstall_agents=True,
            # The agent line: cella's terminated pair -- a wire to a
            # terminator appliance that resolves, terminates, and splices
            # named world egress, judged by name. See terminator.py.
            filtered_egress=True,
            # The whole trial is one baked boot; the trial flow bakes
            # and calls run_sealed_trial instead of issuing execs.
            sealed_oneshot=True,
        )

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # One vCPU, always: a cpu ceiling cannot be honored as asked.
        # --mem-mb is a real limit.
        return EnvironmentResourceCapabilities(memory_limit=True)

    @classmethod
    def preflight(cls) -> None:
        binary = cella_bin()
        if shutil.which(binary) is None and not Path(binary).is_file():
            raise RuntimeError(
                f"no cella CLI at {binary!r}: run `make .cella`, or set CELLA_BIN"
            )

    def _validate_definition(self):
        discover_build_file(self.environment_dir)

    # ------------------------------------------------------------- verbs

    def _cella(self, *args: str, timeout_sec: float | None = CELLA_VERB_TIMEOUT_SEC) -> str:
        # Inherit cella's own environment untouched -- in particular
        # never set CELLA_THAW_PREFAULT. Its default (deep) re-warms all
        # guest memory on a thaw, which is what keeps frozen time truly
        # cryogenic: the guest cannot tell it was frozen. Cheapening the
        # thaw (ept/off) lazy-faults and lets real time bleed into the
        # freeze, breaking that guarantee. Latency is answered by
        # membrane memory (not freezing on hot paths), never by a
        # weaker thaw.
        command = [cella_bin(), *args]
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        if completed.returncode != 0:
            raise CellaError(
                f"`cella {' '.join(args)}` failed ({completed.returncode}): "
                f"{completed.stderr.decode(errors='replace').strip() or 'no output'}"
            )
        return completed.stdout.decode(errors="replace")

    def _machine_dir(self, name: str) -> Path:
        return cella_home() / "machines" / name

    # ------------------------------------------------------------- start

    async def start(self, force_build: bool) -> None:
        # The whole cella lifecycle is blocking: podman/krun builds, cella
        # verbs (subprocess.run), VM boot waits, and sleeps. Run it in a
        # worker thread so it never holds the asyncio event loop -- the
        # trial queue runs every trial as a coroutine on one loop, and a
        # blocking body here would serialize all of them (one cella-env
        # at a time on an otherwise idle host). Threads release the GIL
        # across subprocess/IO/sleep, which is exactly what this does, so
        # the trials genuinely overlap.
        await asyncio.to_thread(self._start_blocking, force_build)

    def _start_blocking(self, force_build: bool) -> None:
        self.preflight()
        if self._paired and not self._bridge_bin().is_file():
            raise CellaError(
                f"no bridge at {self._bridge_bin()}: the terminated pair "
                "needs cella-engine (re-run `make .cella`)"
            )
        self._work = Path(
            tempfile.mkdtemp(prefix="cella-env-", dir=self.trial_paths.trial_dir)
        )
        tag = new_build_tag("titanium-cella-env")
        try:
            context = prepare_build_context(
                environment_dir=self.environment_dir,
                context_dir=self._work / "context",
                agent_install_spec=self.agent_install_spec,
                agent_user=self.default_user,
            )
            build_image(
                context_dir=context.context_dir,
                build_file=context.build_file,
                tag=tag,
                timeout_sec=self.task_env_config.build_timeout_sec,
            )
            record = parse_image_record(
                inspect_image(tag, timeout_sec=self.task_env_config.build_timeout_sec)
            )
            self._image_config = dict(record.config)
            source_tar = self._work / "rootfs-source.tar"
            export_rootfs_tar(
                image=tag,
                dest_tar=source_tar,
                timeout_sec=self.task_env_config.build_timeout_sec,
            )
            prepared = prepare_systemd_rootfs(
                source_tag=tag,
                source_image_id=record.image_id,
                source_rootfs_tar=source_tar,
                work_dir=self._work,
                plan_provisioning=plan_systemd_provisioning,
                timeout_sec=self.task_env_config.build_timeout_sec,
            )
            self._base_tar = self._work / "state-0000.tar"
            if prepared.rootfs_tar != self._base_tar:
                shutil.copyfile(prepared.rootfs_tar, self._base_tar)
        finally:
            untag_image(tag)
        if self._paired:
            self._ensure_appliance()

    # ----------------------------------------------------------- uploads

    def _queue(self, entry: BootEntry) -> None:
        if entry.path in self._pending_paths:
            self._pending = [e for e in self._pending if e.path != entry.path]
        self._pending_paths.add(entry.path)
        self._pending.append(entry)

    def _queue_file(self, source: Path, target: str) -> None:
        mode = source.stat().st_mode & 0o777
        self._queue(
            GuestFile(
                path=target,
                contents=source.read_bytes(),
                mode=mode,
                uid=0,
                gid=0,
            )
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await asyncio.to_thread(
            self._queue_file, Path(source_path), str(PurePosixPath(target_path))
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await asyncio.to_thread(self._upload_dir_sync, source_dir, target_dir)

    def _upload_dir_sync(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        base = PurePosixPath(target_dir)
        for root, _dirs, files in os.walk(source):
            for name in files:
                host_path = Path(root) / name
                guest_path = str(base / host_path.relative_to(source).as_posix())
                if host_path.is_symlink():
                    self._queue(
                        GuestSymlink(
                            path=guest_path,
                            target=os.readlink(host_path),
                            uid=0,
                            gid=0,
                        )
                    )
                else:
                    self._queue_file(host_path, guest_path)

    # -------------------------------------------------------------- exec

    # --- the sealed one-shot trial (docs/environments/CELLA.md §4) ------

    def _orchestrator_files(self, phases) -> list[BootEntry]:
        """The guest state machine as boot-layer entries: one step
        script per step, one phase script per phase (steps in order, a
        failing step ends its phase), and the orchestrator that runs
        the phases under their baked budgets and ends the machine with
        a forced reset — the completion signal the host observes
        (reboot=k: the reset exits the VMM). Re-entry after a reset
        that re-booted instead of exiting sees the done marker and
        resets again."""
        effective_cwd = self._image_config.get("WorkingDir") or "/"
        entries: list[BootEntry] = []
        phase_lines: list[str] = []
        for phase in phases:
            phase_script_lines = [
                "#!/bin/bash",
                f"cd {_shell_quote(effective_cwd)} || cd /",
            ]
            for index, step in enumerate(phase.steps):
                merged_env: dict[str, str] = {}
                for declared in self._image_config.get("Env") or []:
                    key, _, value = str(declared).partition("=")
                    merged_env[key] = value
                merged_env.update(self._persistent_env)
                merged_env.update(step.env)
                run_as = step.user if step.user is not None else self.default_user
                if run_as is None:
                    run_as = self._image_config.get("User") or None
                if run_as in (None, 0, "0"):
                    run_as = "root"
                step_path = f"{RUNNER_DIR}/steps/{phase.name}-{index}.sh"
                entries.append(
                    GuestFile(
                        path=step_path,
                        contents=(step.command + "\n").encode(),
                        mode=0o755,
                        uid=0,
                        gid=0,
                    )
                )
                exports = "".join(
                    f"export {key}={_shell_quote(value)}\n"
                    for key, value in merged_env.items()
                )
                phase_script_lines.append(
                    "(\n"
                    + exports
                    + f"runuser -u {_shell_quote(str(run_as))} -- "
                    f"bash {step_path}\n"
                    f") >> $R/{phase.name}/stdout 2>> $R/{phase.name}/stderr\n"
                    "rc=$?\n"
                    "if [ $rc -ne 0 ]; then\n"
                    f"  echo $rc > $R/{phase.name}/rc\n"
                    "  exit $rc\n"
                    "fi"
                )
            phase_script_lines.append(f"echo 0 > $R/{phase.name}/rc")
            entries.append(
                GuestFile(
                    path=f"{RUNNER_DIR}/phases/{phase.name}.sh",
                    contents=(
                        "R=" + RUNNER_DIR + "/result\n"
                        + "\n".join(phase_script_lines)
                        + "\n"
                    ).encode(),
                    mode=0o755,
                    uid=0,
                    gid=0,
                )
            )
            budget = (
                f"timeout -k 10 {int(phase.timeout_sec)} "
                if phase.timeout_sec
                else ""
            )
            phase_lines.append(
                f"mkdir -p $R/{phase.name}\n"
                f"touch $R/{phase.name}/stdout $R/{phase.name}/stderr\n"
                f"{budget}bash {RUNNER_DIR}/phases/{phase.name}.sh\n"
                f"[ -f $R/{phase.name}/rc ] || echo 124 > $R/{phase.name}/rc"
            )
        wire_prelude = member_prelude("eth0") if self._paired else ""
        orchestrator = (
            "#!/bin/bash\n"
            "# Generated by titanium: the sealed one-shot trial's state\n"
            "# machine. One boot runs every phase; the forced reset is\n"
            "# the completion signal (reboot=k exits the VMM).\n"
            f"R={RUNNER_DIR}/result\n"
            "end() { sync; reboot -f; echo 1 > /proc/sys/kernel/sysrq; "
            "echo b > /proc/sysrq-trigger; }\n"
            'if [ -f "$R/done" ]; then end; fi\n'
            f"mkdir -p $R /logs/agent /logs/verifier /logs/artifacts\n"
            + wire_prelude
            + "\n".join(phase_lines)
            + "\ntouch $R/done\nend\n"
        )
        unit = (
            "[Unit]\n"
            "Description=Titanium sealed one-shot trial\n"
            "After=basic.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=yes\n"
            f"ExecStart=/bin/bash {RUNNER_DIR}/orchestrator.sh\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        entries.append(
            GuestFile(
                path=f"{RUNNER_DIR}/orchestrator.sh",
                contents=orchestrator.encode(),
                mode=0o755,
                uid=0,
                gid=0,
            )
        )
        entries.append(
            GuestFile(
                path="/etc/systemd/system/titanium-trial.service",
                contents=unit.encode(),
                mode=0o644,
                uid=0,
                gid=0,
            )
        )
        entries.append(
            GuestSymlink(
                path=(
                    "/etc/systemd/system/multi-user.target.wants/"
                    "titanium-trial.service"
                ),
                target="../titanium-trial.service",
                uid=0,
                gid=0,
            )
        )
        return entries

    async def run_sealed_trial(self, phases) -> dict[str, ExecResult]:
        return await asyncio.to_thread(self._run_sealed_blocking, list(phases))

    def _run_sealed_blocking(self, phases) -> dict[str, ExecResult]:
        with self._lifecycle:
            return self._run_sealed_locked(phases)

    def _run_sealed_locked(self, phases) -> dict[str, ExecResult]:
        if self._base_tar is None:
            raise CellaError("sealed trial before start: not running")
        job_entries = self._orchestrator_files(phases)
        if self._paired:
            ca_pem = pair_ca_path(Path.home()).read_bytes()
            job_entries = member_trust_entries(ca_pem) + job_entries
        boot_layer = BootLayer(entries=tuple(self._pending) + tuple(job_entries))
        flavor = self._publish_cycle_flavor(boot_layer)
        name = flavor
        self._machine = name
        memory_mb = self._effective_memory_mb or 1024
        bridge: subprocess.Popen | None = None
        judged = self._paired
        try:
            self._destroy_quietly(name)
            self._cella(
                "create",
                name,
                "--kernel",
                "canonical",
                "--rootfs",
                flavor,
                "--mem-mb",
                str(memory_mb),
                "--net",
                self._task_net(),
                "--root",
                "rw",
            )
            self._register_mirror(name)
            self._cella("start", name)
            if judged:
                self._cella("gateway", name, "open")
                port = self._ensure_engine(
                    name, self._member_policy_path(), dry_run=False
                )
                bridge = self._spawn_bridge(name, port)
            # The total budget: the phases' declared budgets summed,
            # the config ceiling standing in per phase that declared
            # none, plus one boot margin.
            budget = (
                sum(p.timeout_sec or CELLA_EXEC_TIMEOUT for p in phases)
                + BOOT_MARGIN_SEC
            )
            self._wait_for_reset(name, time.monotonic() + budget)
        finally:
            if bridge is not None:
                bridge.terminate()
                try:
                    bridge.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    bridge.kill()
            self._kill_engine(name)
            self._preserve_chronicle(name)
            self._preserve_edge_log(name)
            self._machine = None
            flavor_dir = rootfs_flavor_dir(flavor)
            if flavor_dir.exists():
                shutil.rmtree(flavor_dir, ignore_errors=True)
        # The machine is the evidence now: every later download
        # extracts from it, and stop(delete) retires it.
        self._evidence_machine = name
        return self._sealed_results(name, phases)

    def _wait_for_reset(self, name: str, deadline: float) -> None:
        """Wait for the guest's forced reset, thawing through parks.

        Completion is two host-side facts: the VMM pid is gone and no
        frozen ``state`` file exists. A park still freezes the member
        (the park is the freeze), so the wait pumps thaws exactly as
        the exec model did; no guest byte is ever read here."""
        machine_dir = self._machine_dir(name)
        while time.monotonic() < deadline:
            if (machine_dir / "state").is_file():
                time.sleep(1.0)
                try:
                    self._cella("thaw", name)
                except CellaError:
                    pass  # raced a concurrent transition; loop decides
                continue
            if not self._vmm_alive(name):
                if (machine_dir / "state").is_file():
                    continue
                return
            time.sleep(1.0)
        raise CellaError(
            f"machine {name} did not reset in time; vmm.log tail:\n"
            + _tail(machine_dir / "vmm.log")
        )

    def _sealed_results(self, name: str, phases) -> dict[str, ExecResult]:
        assert self._work is not None
        out_dir = Path(tempfile.mkdtemp(prefix="cella-results-", dir=self._work))
        try:
            result_root = self._extract_dir(name, f"{RUNNER_DIR}/result", out_dir)
            results: dict[str, ExecResult] = {}
            for phase in phases:
                phase_dir = result_root / phase.name
                if not phase_dir.is_dir():
                    continue
                try:
                    return_code = int((phase_dir / "rc").read_text().strip())
                except (OSError, ValueError):
                    continue
                results[phase.name] = ExecResult(
                    stdout=_read_or_empty(phase_dir / "stdout"),
                    stderr=_read_or_empty(phase_dir / "stderr"),
                    return_code=return_code,
                )
            if not results:
                raise CellaError(
                    "the guest reset without a result; vmm.log tail:\n"
                    + _tail(self._machine_dir(name) / "vmm.log")
                )
            return results
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _evidence_cache_dir(self, guest_dir: str) -> Path:
        """The host-side cache of one extracted guest directory.

        One extractor boot per *directory*, ever: the first read under
        a root extracts it once; every later file or dir read under it
        is a host-side copy from the cache. ``/logs`` is the standing
        root (agent, verifier, and artifacts all live under it), so a
        whole trial's downloads normally cost one extract. A path
        outside every cached root extracts its own parent directory,
        which then joins the cache."""
        assert self._work is not None and self._evidence_machine is not None
        for cached_guest, cached_host in self._evidence_cache.items():
            if guest_dir == cached_guest or guest_dir.startswith(cached_guest + "/"):
                inner = cached_host / guest_dir[len(cached_guest) :].lstrip("/")
                return inner
        root = "/logs" if guest_dir.startswith("/logs") else guest_dir
        host = Path(
            tempfile.mkdtemp(prefix="cella-evidence-", dir=self._work)
        )
        extracted = self._extract_dir(self._evidence_machine, root, host)
        self._evidence_cache[root] = extracted
        return extracted / guest_dir[len(root) :].lstrip("/")

    def _evidence_file_read(self, source_path: str, target: Path) -> None:
        """One file out of the evidence cache (extracted per directory,
        never per file — see :meth:`_evidence_cache_dir`)."""
        guest = str(PurePosixPath("/") / str(source_path).lstrip("/"))
        parent = str(PurePosixPath(guest).parent)
        cached = self._evidence_cache_dir(parent)
        extracted = cached / PurePosixPath(guest).name
        if not extracted.is_file():
            raise FileNotFoundError(f"{source_path} is not in the evidence tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(extracted, target)

    def _extract_dir(self, name: str, guest_path: str, target: Path) -> Path:
        """``cella extract``: the machine's own evidence verb. The tar
        arrives trailer-verified on stdout; extraction filters every
        member (stdlib ``data`` filter), so a hostile archive cannot
        write outside *target*. The guest tars from the filesystem
        root (`tar -C /rock .<path>`), so members carry the full guest
        path; the returned path is the extracted *guest_path* inside
        *target*."""
        target.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [cella_bin(), "extract", name, guest_path],
            capture_output=True,
            timeout=self.task_env_config.build_timeout_sec,
            check=False,
        )
        if completed.returncode != 0:
            raise FileNotFoundError(
                f"cella extract {name} {guest_path} failed: "
                + completed.stderr.decode(errors="replace").strip()[-500:]
            )
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            archive.extractall(target, filter="data")
        return target / str(guest_path).strip("/")

    def _bridge_bin(self) -> Path:
        return Path(cella_bin()).parent / "cella-engine"

    def _policy_path(self) -> Path:
        """The task's cella.policy, beside its build file. In dry-run
        the engine writes it there, into the staged task copy, and the
        make target carries it back to the example for review."""
        return self.environment_dir / "cella.policy"

    def _member_policy_path(self) -> Path:
        """The policy the member (task) machine's engine serves.

        A paired member reaches only its appliance, so its border is
        fixed (terminator.py's wire grants) -- the wire plane's ARP and
        the appliance's three ports. The task's own cella.policy names
        *world* domains, which are judged on the appliance's border, not
        here. An unpaired airgapped member has no engine at all.
        """
        assert self._work is not None
        composed = self._work / "member.policy"
        if not composed.exists():
            composed.write_text(member_policy_text())
        self._preserve_policy(composed)
        return composed

    def _world_hosts(self) -> list[str]:
        """The world domains the appliance may reach for this trial: the
        agent's inference host always (an agent always needs its line),
        and the task's own cella.policy domains when allow_internet=true
        (its declared egress). allow_internet=false keeps the task
        airgapped while the agent still resolves its API."""
        hosts: set[str] = set()
        if self._agent:
            hosts.update(self.network_allowlist.domains)
        if self._topology.judged and self._policy_path().exists():
            task_policy = Policy.parse(self._policy_path().read_text())
            hosts.update(
                grant.host
                for grant in task_policy.grants
                if grant.host and grant.verb == "release"
            )
        return sorted(hosts)

    def _appliance_policy_path(self) -> Path:
        """The policy the appliance's engine serves: the world leg,
        judged by name (terminator.py). Composed in the work directory
        from the trial's allowed world hosts; in dry-run the appliance
        instead records what it saw into the task's reviewable file."""
        assert self._work is not None
        composed = self._work / "appliance.policy"
        composed.write_text(appliance_border_policy_text(self._world_hosts()))
        self._preserve_policy(composed)
        return composed

    def _preserve_policy(self, composed: Path) -> None:
        """Copy a composed border policy into the trial's evidence, so
        what the engine actually enforced survives the work directory's
        cleanup and sits beside the chronicle it decided. The task's own
        cella.policy is its reviewable source and stays in place; these
        are the harness-composed borders (member wire grants, appliance
        world leg) that the run computed."""
        kept = self.trial_paths.trial_dir / "cella-policy"
        kept.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(composed, kept / composed.name)

    def _engine_dir(self, vm_id: str) -> Path:
        """The per-machine log directory: cella-engine/<vm-id>/ holds
        that one machine's engine and bridge logs, so a cycle's crossings
        (the task) stay separate from the next cycle's (the verifier),
        each named by the machine that made them."""
        directory = self.trial_paths.trial_dir / "cella-engine" / vm_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _ensure_engine_loop(self) -> asyncio.AbstractEventLoop:
        """The one background asyncio loop that hosts every in-process
        engine server for this environment, started on first use. The
        harness drives cella from a worker thread (`_exec_blocking`), so
        the engines get their own loop rather than borrow that thread."""
        if self._engine_loop is None:
            self._engine_loop = asyncio.new_event_loop()
            # grpclib logs each bridge disconnect at INFO; Decide already
            # reports it in cella's words, so quiet grpclib's version.
            logging.getLogger("grpclib.server").setLevel(logging.WARNING)
            self._engine_loop_thread = threading.Thread(
                target=self._engine_loop.run_forever, daemon=True
            )
            self._engine_loop_thread.start()
        return self._engine_loop

    def _engine_logger(self, key: str) -> logging.Logger:
        """A per-machine logger writing to cella-engine/<vm-id>/engine.log
        -- one file per machine, so the task's crossings stay separate
        from the verifier's."""
        engine_logger = logging.getLogger(f"titanium.cella.engine.{key}")
        engine_logger.setLevel(logging.INFO)
        engine_logger.propagate = False
        if not engine_logger.handlers:
            handler = _BufferedEngineLog(self._engine_dir(key) / "engine.log")
            # host_ns first, then the message -- uniform with the audit book.
            handler.setFormatter(logging.Formatter("host_ns=%(host_ns)d %(message)s"))
            engine_logger.addHandler(handler)
            self._engine_log_sinks[key] = handler
            self._ensure_log_drain()
        return engine_logger

    def _ensure_log_drain(self) -> None:
        """Start the one background thread that writes every machine's
        engine-log batch to disk each second, if it is not running."""
        if self._log_drain_thread is None:
            self._log_drain_stop.clear()
            self._log_drain_thread = threading.Thread(
                target=self._drain_engine_logs, name="cella-log-drain", daemon=True
            )
            self._log_drain_thread.start()

    def _drain_engine_logs(self) -> None:
        """The one-second housekeeping thread: flush each machine's
        buffered engine log, and mirror each registered machine's
        host-side books into the trial dir -- so the chronicle and
        edge.log read live, not only at teardown. File writes happen
        here, never on the decide hot path. `wait` returns True only
        when stop is set, so teardown ends the loop at once; the final
        flush is each sink's close(), and the final book copy is
        _preserve_chronicle."""
        while not self._log_drain_stop.wait(ENGINE_LOG_DRAIN_SEC):
            for sink in list(self._engine_log_sinks.values()):
                sink.flush()
            self._mirror_books()

    def _register_mirror(self, name: str) -> None:
        """Mirror *name*'s books live: create its trial-dir folders now
        (the record appears when the machine does) and let the drain
        thread copy changed files each second. Only the still-disk
        evidence (results, artifacts) waits for the trial's end."""
        chronicle = self.trial_paths.trial_dir / "cella-chronicle" / name
        (chronicle / "network").mkdir(parents=True, exist_ok=True)
        self._mirrored[name] = self._machine_dir(name)
        self._ensure_log_drain()

    def _mirror_books(self) -> None:
        for name, machine_dir in list(self._mirrored.items()):
            out = self.trial_paths.trial_dir / "cella-chronicle" / name
            for relative in self._CHRONICLE_FILES:
                self._mirror_one(machine_dir / relative, out / relative)
            self._mirror_one(
                machine_dir / "edge.log", self._engine_dir(name) / "edge.log"
            )

    @staticmethod
    def _mirror_one(source: Path, dest: Path) -> None:
        try:
            stat = source.stat()
        except OSError:
            return
        try:
            if dest.exists():
                d = dest.stat()
                if d.st_size == stat.st_size and d.st_mtime >= stat.st_mtime:
                    return
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        except OSError:
            return  # a book mid-write copies on the next tick

    def _ensure_engine(self, key: str, policy_path: Path, dry_run: bool) -> int:
        """Start one in-process policy engine for the machine named
        *key*; return its port. Keyed by the machine (vm id): the
        member's engine is a fresh one per exec cycle, the appliance's a
        stable one for the trial, and each logs under
        cella-engine/<vm-id>/. No subprocess, no spawn, no readiness
        poll -- serve() returns only once listening."""
        running = self._engines.get(key)
        if running is not None:
            return running[1]
        judge = build_judge(policy_path, dry_run)
        engine_logger = self._engine_logger(key)
        loop = self._ensure_engine_loop()
        server = asyncio.run_coroutine_threadsafe(
            serve(judge, "127.0.0.1", 0, engine_logger=engine_logger), loop
        ).result(timeout=15)
        port = bound_port(server)
        self._engines[key] = (server, port)
        return port

    def _close_server(self, server) -> None:
        async def _close() -> None:
            server.close()
            await server.wait_closed()

        if self._engine_loop is not None:
            # Best-effort teardown: the close can time out, and the loop
            # may already be stopping. Either way the daemon loop thread
            # dies with the process, so a failure here is not fatal.
            try:
                asyncio.run_coroutine_threadsafe(
                    _close(), self._engine_loop
                ).result(timeout=10)
            except (TimeoutError, OSError, RuntimeError) as exc:
                self.logger.debug("cella: engine server close failed: %s", exc)

    def _kill_engine(self, key: str) -> None:
        """Close one machine's engine server and forget it -- called when
        that machine is torn down at the end of its exec cycle."""
        running = self._engines.pop(key, None)
        if running is None:
            return
        self._close_server(running[0])
        # Drop the sink from the drainer's view first, then close it (close
        # flushes the last batch), so the drainer never races the close.
        self._engine_log_sinks.pop(key, None)
        engine_logger = logging.getLogger(f"titanium.cella.engine.{key}")
        for handler in list(engine_logger.handlers):
            handler.close()
            engine_logger.removeHandler(handler)

    def _stop_engines(self) -> None:
        for key in list(self._engines):
            self._kill_engine(key)
        self._engines = {}
        # Each sink was flushed and closed by its _kill_engine; stop the
        # drainer now that there is nothing left to write.
        if self._log_drain_thread is not None:
            self._log_drain_stop.set()
            self._log_drain_thread.join(timeout=5)
            self._log_drain_thread = None
        if self._engine_loop is not None:
            self._engine_loop.call_soon_threadsafe(self._engine_loop.stop)
            if self._engine_loop_thread is not None:
                self._engine_loop_thread.join(timeout=5)
            self._engine_loop.close()
            self._engine_loop = None
            self._engine_loop_thread = None

    def _spawn_bridge(self, name: str, port: int) -> subprocess.Popen:
        # The bridge is silent on stdout: it writes its real record to the
        # machine's own edge.log (cella-network events, the park/kick
        # cycle), preserved at teardown by _preserve_edge_log. So capture
        # nothing here -- a stdout file would only ever be empty.
        return subprocess.Popen(
            [str(self._bridge_bin()), name, "--dial", f"127.0.0.1:{port}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _preserve_edge_log(self, name: str) -> None:
        """Copy a machine's edge.log -- the bridge/gateway's real record --
        into cella-engine/<vm-id>/ before the machine is destroyed. It is
        evidence of the edge, and destroy takes it with the machine."""
        source = self._machine_dir(name) / "edge.log"
        if source.is_file():
            try:
                shutil.copyfile(source, self._engine_dir(name) / "edge.log")
            except OSError as exc:
                self.logger.debug("cella: could not preserve edge.log: %s", exc)

    def _wire_name(self) -> str:
        return f"{_flavor_name(self.session_id)[:40].rstrip('-')}-line"

    def _task_net(self) -> str:
        """The member (task) machine's --net. A paired member is
        wire-only: the appliance holds the world, and the member reaches
        it over the wire -- so task egress is impossible except through
        the terminator, whatever allow_internet says. An unpaired
        airgapped member has no nic at all (--net none)."""
        if not self._paired:
            return self._topology.net
        return f"wire:{self._wire_name()}"

    def _ensure_appliance(self) -> None:
        """Stand the terminator appliance for the trial, once.

        The appliance boots cella's terminator golden **directly** --
        no titanium flavor, no file injected: the golden's own init
        writes ``/etc/cella-terminator.conf`` at boot, and its
        defaults (wire 10.77.0.1, resolver 9.9.9.9, listen 443,80) are
        exactly the constants terminator.py grants against. Titanium
        edits no filesystem anywhere on this rung. --net world,wire,
        the world membrane judged by titanium's engine serving the
        appliance border (the world leg, by name -- terminator.py). It
        runs for the trial's whole life; a background thread thaws it
        through every park (the park is the freeze, and the appliance
        parks on every DNS and world flow it forwards -- standing
        memory keeps the hot paths live).
        """
        if self._appliance is not None:
            return
        assert self._work is not None
        ca_path = pair_ca_path(Path.home())
        if not ca_path.is_file():
            raise CellaError(
                f"no pair CA at {ca_path}: the terminator golden is not "
                "built (re-run `make .cella`)"
            )
        name = f"{_flavor_name(self.session_id)[:33].rstrip('-')}-appliance"
        # Idempotent create: a trial-level retry regenerates this exact
        # appliance name (per session, no cycle) while the prior
        # attempt's may linger. Clear it first.
        self._destroy_quietly(name)
        self._cella(
            "create",
            name,
            "--kernel",
            "canonical",
            "--rootfs",
            "terminator",
            "--mem-mb",
            "512",
            "--net",
            f"world,wire:{self._wire_name()}",
            "--root",
            "rw",
        )
        self._register_mirror(name)
        self._cella("start", name)
        self._cella("gateway", name, "open")
        # In dry-run the appliance records the world leg (by name) into
        # the task's reviewable cella.policy; otherwise it enforces the
        # composed appliance border.
        if self._dry_run:
            port = self._ensure_engine(name, self._policy_path(), dry_run=True)
        else:
            port = self._ensure_engine(
                name, self._appliance_policy_path(), dry_run=False
            )
        self._appliance_bridge = self._spawn_bridge(name, port)
        self._appliance = name
        self._appliance_stop.clear()
        self._appliance_thaw = threading.Thread(
            target=self._thaw_forever, args=(name,), daemon=True
        )
        self._appliance_thaw.start()
    def _thaw_forever(self, name: str) -> None:
        """The appliance's side of the freeze dance, for the machine's
        whole life: every park freezes it, every staged decision
        applies at the thaw."""
        state = self._machine_dir(name) / "state"
        while not self._appliance_stop.is_set():
            if state.is_file():
                time.sleep(0.5)
                try:
                    self._cella("thaw", name)
                except (CellaError, subprocess.TimeoutExpired):
                    pass
            else:
                time.sleep(0.5)

    def _stop_appliance(self) -> None:
        if self._appliance is None:
            return
        self._appliance_stop.set()
        if self._appliance_thaw is not None:
            self._appliance_thaw.join(timeout=10)
            self._appliance_thaw = None
        if self._appliance_bridge is not None:
            self._appliance_bridge.terminate()
            try:
                self._appliance_bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._appliance_bridge.kill()
            self._appliance_bridge = None
        # The appliance's border is judged too; its chronicle is part of
        # the run's record -- the world leg of every crossing the member
        # made, granted and refused, judged by name.
        self._preserve_chronicle(self._appliance)
        self._preserve_edge_log(self._appliance)
        self._retire_machine(self._appliance)
        # No flavor to clean: the appliance boots the terminator golden
        # directly, and the golden belongs to `make .cella`, not a trial.
        self._appliance = None

    async def set_phase(self, phase: str) -> None:
        """Record the trial phase. One boot runs every phase, so the
        phase no longer names machines; it stays for the trial flow's
        bookkeeping (the verifier announces itself here)."""
        self._phase = phase

    def _publish_cycle_flavor(self, boot_layer: BootLayer) -> str:
        assert self._work is not None
        # The session id alone: one boot runs the whole trial, so the
        # member carries no phase suffix -- and `cella extract`'s twin
        # reads as `<session>-extractor`, not `<session>-trial-extractor`.
        flavor = _flavor_name(self.session_id)
        size_bytes = (self._effective_storage_mb or 5120) * (1 << 20)
        with staging_flavor_dir(home=None) as staging:
            artifact = staging / ROOTFS_ARTIFACT_NAME
            # One boot, one build: from the prepared tar. Its bytes
            # came from the task's own image build, so podman's default
            # runtime is enough; no guest-produced filesystem is ever
            # mounted or edited on this rung.
            assert self._base_tar is not None
            build_ext4(
                rootfs_tar=self._base_tar,
                boot_layer=boot_layer,
                size_bytes=size_bytes,
                dest=artifact,
                timeout_sec=self.task_env_config.build_timeout_sec,
            )
            write_manifest(
                staging,
                render_golden_json(
                    flavor=flavor,
                    sha3_256=sha3_256_file(artifact),
                    size_bytes=artifact.stat().st_size,
                    built_epoch=int(time.time()),
                    extra_fields={},
                ),
            )
            destination = rootfs_flavor_dir(flavor)
            if destination.exists():
                shutil.rmtree(destination)
            os.rename(staging, destination)
            # staging_flavor_dir's cleanup finds nothing: the rename
            # moved it. Recreate so rmtree in its finally is a no-op.
            staging.mkdir(exist_ok=True)
        return flavor

    # How often the live disk is probed for the result, and the grace
    # the guest gets to finish its poweroff after the result appears.
    _POWEROFF_GRACE_SEC = POWEROFF_GRACE_SEC

    def _vmm_alive(self, name: str) -> bool:
        try:
            pid_text = (self._machine_dir(name) / "pid").read_text().strip()
        except OSError:
            return False
        if not pid_text:
            return False
        try:
            os.kill(int(pid_text), 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            pass
        return True

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        raise CellaError(
            "the cella rung is sealed one-shot: no per-command exec exists. "
            "The trial flow bakes every input and calls run_sealed_trial; "
            "a task healthcheck or interactive agent cannot run here."
        )

    _CHRONICLE_FILES = (
        "network/ledger",
        "network/names",
        "verdict",
        "audit",
        "membrane-memory",
        "manifest.json",
        "vmm.log",
        "valve",
        "uid",
    )

    # The books cella's own --dump decoder renders to a .txt beside
    # the raw bytes.
    _CHRONICLE_DUMPABLE = (
        "network/ledger",
        "network/names",
        "verdict",
        "audit",
        "membrane-memory",
    )

    def _preserve_chronicle(self, name: str) -> None:
        """Copy a still machine's audit files into the trial dir before
        it is destroyed, and render each book to a ``.txt`` beside its
        raw bytes with ``cella --dump``. Best-effort throughout: a
        missing file (an airgapped machine has no verdict or memory) is
        skipped, and neither a copy nor a dump error ever fails the
        exec that produced a result."""
        machine_dir = self._machine_dir(name)
        out = self.trial_paths.trial_dir / "cella-chronicle" / name
        for relative in self._CHRONICLE_FILES:
            source = machine_dir / relative
            if not source.is_file():
                continue
            destination = out / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError as exc:
                self.logger.warning("cella: could not preserve %s: %s", source, exc)
                continue
            if relative in self._CHRONICLE_DUMPABLE:
                self._dump_chronicle(destination)

    def _dump_chronicle(self, raw: Path) -> None:
        """Render one preserved book to ``<raw>.txt`` via ``cella --dump``.
        The dump is cella's authoritative decoder; titanium keeps no
        codec of its own for this. Best-effort: a decode failure leaves
        the raw bytes as the record."""
        try:
            text = self._cella("--dump", str(raw), timeout_sec=60.0)
        except (CellaError, subprocess.TimeoutExpired) as exc:
            self.logger.warning("cella: could not dump %s: %s", raw, exc)
            return
        try:
            raw.with_suffix(raw.suffix + ".txt").write_text(text)
        except OSError as exc:
            self.logger.warning("cella: could not write dump for %s: %s", raw, exc)

    def _destroy_quietly(self, name: str) -> None:
        for verb in ("stop", "destroy"):
            try:
                self._cella(verb, name)
            except (CellaError, subprocess.TimeoutExpired):
                pass

    def _retire_machine(self, name: str) -> None:
        self._mirrored.pop(name, None)
        """End a machine's life at teardown. `teardown` (the default)
        destroys it. `archive` stops it and latches it as a cella
        artifact (`cella archive`) -- a rock, inspected with `cella
        inspect` and forked back to a runnable machine with `cella
        branch`, never thawed. The evidence is already copied out either
        way (the chronicle); this only decides whether the machine
        itself survives."""
        if self._on_completion != "archive":
            self._destroy_quietly(name)
            return
        for verb in ("stop", "archive"):
            try:
                self._cella(verb, name)
            except (CellaError, subprocess.TimeoutExpired):
                pass

    # ----------------------------------------------------------- downloads
    #
    # Before the first cycle the evidence is the prepared base tar;
    # after it, the last evidence disk. Either way a download is a
    # targeted read of exactly the asked-for paths -- never a machine,
    # never a full-tree pass.

    def _state_members(self) -> tarfile.TarFile:
        if self._base_tar is None:
            raise CellaError("download before start: there is no evidence yet")
        return tarfile.open(self._base_tar, "r:")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await asyncio.to_thread(self._download_file_sync, source_path, target_path)

    def _download_file_sync(self, source_path: str, target_path: Path | str) -> None:
        if self._evidence_machine is not None:
            self._evidence_file_read(source_path, Path(target_path))
            return
        wanted = "./" + str(PurePosixPath(source_path)).lstrip("/")
        with self._state_members() as archive:
            try:
                member = archive.getmember(wanted)
            except KeyError as exc:
                raise FileNotFoundError(
                    f"{source_path} is not in the evidence tree"
                ) from exc
            handle = archive.extractfile(member)
            if handle is None:
                raise FileNotFoundError(f"{source_path} is not a regular file")
            target = Path(target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle, target.open("wb") as sink:
                shutil.copyfileobj(handle, sink)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await asyncio.to_thread(self._download_dir_sync, source_dir, target_dir)

    def _download_dir_sync(self, source_dir: str, target_dir: Path | str) -> None:
        if self._evidence_machine is not None:
            target = Path(target_dir)
            guest = str(PurePosixPath("/") / str(source_dir).lstrip("/"))
            try:
                cached = self._evidence_cache_dir(guest)
            except FileNotFoundError:
                # An absent directory downloads as empty, matching the
                # tolerant log-collection paths in the trial flow.
                target.mkdir(parents=True, exist_ok=True)
                return
            if cached.is_dir():
                shutil.copytree(cached, target, dirs_exist_ok=True, symlinks=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
            return
        prefix = "./" + str(PurePosixPath(source_dir)).lstrip("/")
        prefix = prefix.rstrip("/") + "/"
        target = Path(target_dir)
        found = False
        with self._state_members() as archive:
            for member in archive.getmembers():
                if not member.name.startswith(prefix) or not member.isfile():
                    continue
                found = True
                relative = member.name[len(prefix) :]
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                with handle, destination.open("wb") as sink:
                    shutil.copyfileobj(handle, sink)
        if not found:
            # An absent directory downloads as empty, matching the
            # tolerant log-collection paths in the trial flow.
            target.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- stop

    async def stop(self, delete: bool) -> None:
        # Off the event loop: teardown thaws/destroys machines (blocking
        # cella verbs) and preserves the chronicle. See _start_blocking.
        await asyncio.to_thread(self._stop_blocking, delete)

    def _stop_blocking(self, delete: bool) -> None:
        # Wait for any abandoned exec thread to finish before teardown,
        # so destroy does not race a cycle still writing its evidence.
        with self._lifecycle:
            self._stop_appliance()
            self._stop_engines()
            if self._machine is not None:
                self._retire_machine(self._machine)
                self._machine = None
            # The evidence machine held the trial's still disk for the
            # post-run extracts; its life ends with the environment.
            if self._evidence_machine is not None:
                self._retire_machine(self._evidence_machine)
                self._evidence_machine = None
            # The work dir (cella-env-<id>/ in the trial dir) is part of
            # the trial's record and survives teardown: the build
            # context, the base tar, and the extracted evidence caches
            # say what the machine was made from and what came out.
            if delete:
                self._work = None
                self._base_tar = None


def _flavor_name(session_id: str) -> str:
    """A name valid as both a flavor and a machine name.

    The machine name is the stricter contract: cella accepts only
    lowercase letters, digits, and dashes there, at most 64 of them,
    and `cella extract` appends `-extractor` (10 more) to name its
    twin. One boot runs the whole trial, so the session id alone
    names it: no harness prefix, no cycle counter. The session cap of
    40 plus the longest phase suffix and the extractor twin stays
    under cella's 64. Runs of anything else collapse to one dash so
    two session ids cannot alias by punctuation alone.
    """
    safe = re.sub(r"[^a-z0-9]+", "-", session_id.lower())
    return safe[:40].strip("-") or "trial"


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no vmm.log)"


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""
