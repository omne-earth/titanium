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
# Cella has no exec-into, by design, so the BaseEnvironment contract is
# honored the only honest way a sealed runtime allows: every `exec` is
# one whole experiment. Files uploaded since the last cycle plus a
# oneshot unit running the command are baked into a fresh ext4; a fresh
# machine boots it, runs, and powers off; the still disk is copied and
# read as evidence (fuse2fs, read-only, on the copy), which yields the
# command's exit code and output *and* becomes the next cycle's
# filesystem -- state moves forward only as evidence off still disks.
# Downloads never touch a machine at all: they read the current state
# tar. Nothing reaches into a running guest, nothing is installed after
# a boot, and no channel outlives a cycle.

import asyncio
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
from titanium.environments.cella.engine import bound_port, build_judge, serve
from titanium.environments.cella.flavor import (
    ROOTFS_ARTIFACT_NAME,
    cella_home,
    render_golden_json,
    rootfs_flavor_dir,
    staging_flavor_dir,
    write_manifest,
)
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    PodmanError,
    build_image,
    export_rootfs_tar,
    inspect_image,
    new_build_tag,
    run_podman,
    untag_image,
)
from titanium.environments.cella.policy import Policy
from titanium.environments.cella.rootfs import (
    build_ext4,
    ensure_rootfs_builder_image,
    place_into_ext4,
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
    terminator_conf_entry,
    terminator_golden_rootfs,
)

# The guest-side scratch the runner owns. Deliberately one directory:
# excluded from the state carried to the next cycle, so a cycle's
# result files never masquerade as task state.
_RUNNER_DIR = "/titanium"

# How long past the exec timeout the guest gets to boot and halt.
_BOOT_MARGIN_SEC = 180.0

_DEFAULT_EXEC_TIMEOUT_SEC = 600.0


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
        # absent default) destroys it. `archive` keeps it as a cella
        # artifact (`cella archive`), resumable and inspectable later --
        # the forensic path for a run you want to hold. Anything but an
        # explicit `archive` is teardown.
        self._on_completion = (
            "archive" if str(on_completion).lower() == "archive" else "teardown"
        )
        # Each engine is an in-process grpclib server (no subprocess):
        # {vm-id: (server, port)}, all hosted on one background asyncio
        # loop thread so they never block the harness's event loop.
        self._engines: dict[str, tuple] = {}
        self._engine_loop: asyncio.AbstractEventLoop | None = None
        self._engine_loop_thread: threading.Thread | None = None
        self._work: Path | None = None
        # The terminated pair (terminator.py) stands when the workload
        # needs the world at all: an agent always needs its inference
        # line, and allow_internet=true adds the task's own egress. Only
        # an agentless airgapped trial (--net none) needs no appliance.
        self._agent = self.agent_install_spec is not None
        self._paired = self._agent or self._topology.judged
        self._term: str | None = None
        self._term_bridge: subprocess.Popen | None = None
        self._term_thaw: threading.Thread | None = None
        self._term_stop = threading.Event()
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
        self._state_img: Path | None = None
        self._pending: list[BootEntry] = []
        self._pending_paths: set[str] = set()
        self._cycle = 0
        self._image_config: dict = {}
        self._machine: str | None = None

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
        # Evidence disks and guest-produced tars are parsed inside krun
        # microVMs, never by host-side code: krun is a hard dependency of
        # this environment, not an option.
        if shutil.which("krun") is None:
            raise RuntimeError(
                "krun is not on PATH: the cella environment parses evidence "
                "disks inside krun microVMs (run `make .krun-podman`)"
            )

    def _validate_definition(self):
        discover_build_file(self.environment_dir)

    # ------------------------------------------------------------- verbs

    def _cella(self, *args: str, timeout_sec: float | None = 120.0) -> str:
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
            self._ensure_terminator()

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

    def _job_files(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        user: str | int | None,
    ) -> list[BootEntry]:
        effective_cwd = cwd or self._image_config.get("WorkingDir") or "/"
        merged_env: dict[str, str] = {}
        for declared in self._image_config.get("Env") or []:
            key, _, value = str(declared).partition("=")
            merged_env[key] = value
        merged_env.update(self._persistent_env)
        merged_env.update(env or {})

        # The identity ladder matches the container rungs: an explicit
        # user, else titanium's declared default, else the image's own
        # Config.User -- which after an agent bake is the agent user
        # the install steps ran as (their `USER` directive wins), so
        # the baked agent's ~/.local paths resolve. runuser supplies
        # that user's HOME.
        run_as = user if user is not None else self.default_user
        if run_as is None:
            run_as = self._image_config.get("User") or None
        if run_as in (None, 0, "0"):
            run_as = "root"
        exports = "".join(
            f"export {key}={_shell_quote(value)}\n" for key, value in merged_env.items()
        )
        # Always through runuser, root included: the job runs under a
        # systemd unit with no HOME at all, and a baked agent's
        # ~/.local paths need the target user's real HOME. runuser
        # sets HOME/USER/LOGNAME for the target and keeps the exports.
        invoke = (
            f"runuser -u {_shell_quote(str(run_as))} -- bash {_RUNNER_DIR}/command.sh"
        )
        # A paired member is wire-only: the appliance holds the world, so
        # its one nic is eth0. The prelude addresses it (no kernel
        # autoconfiguration on a wire), folds the pair CA into the trust
        # bundle, and pins the ephemeral range to the reply window.
        wire_prelude = ""
        if self._paired:
            wire_prelude = member_prelude("eth0")
        job = (
            "#!/bin/bash\n"
            "# Generated by titanium's cella environment: one exec, one boot.\n"
            f"mkdir -p {_RUNNER_DIR}/result /logs/agent /logs/verifier /logs/artifacts\n"
            + wire_prelude
            + f"cd {_shell_quote(effective_cwd)} || cd /\n"
            f"{exports}"
            f"{invoke} > {_RUNNER_DIR}/result/stdout 2> {_RUNNER_DIR}/result/stderr\n"
            "rc=$?\n"
            "sync\n"
            f"echo $rc > {_RUNNER_DIR}/result/rc\n"
            "sync\n"
            "systemctl poweroff --no-block\n"
        )
        unit = (
            "[Unit]\n"
            "Description=Titanium exec cycle\n"
            "After=basic.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=yes\n"
            f"ExecStart=/bin/bash {_RUNNER_DIR}/job.sh\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        return [
            GuestFile(
                path=f"{_RUNNER_DIR}/command.sh",
                contents=(command + "\n").encode(),
                mode=0o755,
                uid=0,
                gid=0,
            ),
            GuestFile(
                path=f"{_RUNNER_DIR}/job.sh",
                contents=job.encode(),
                mode=0o755,
                uid=0,
                gid=0,
            ),
            GuestFile(
                path="/etc/systemd/system/titanium-exec.service",
                contents=unit.encode(),
                mode=0o644,
                uid=0,
                gid=0,
            ),
            GuestSymlink(
                path="/etc/systemd/system/multi-user.target.wants/titanium-exec.service",
                target="../titanium-exec.service",
                uid=0,
                gid=0,
            ),
        ]

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
            handler = logging.FileHandler(self._engine_dir(key) / "engine.log")
            handler.setFormatter(logging.Formatter("%(message)s"))
            engine_logger.addHandler(handler)
        return engine_logger

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
        engine_logger = logging.getLogger(f"titanium.cella.engine.{key}")
        for handler in list(engine_logger.handlers):
            handler.close()
            engine_logger.removeHandler(handler)

    def _stop_engines(self) -> None:
        for key in list(self._engines):
            self._kill_engine(key)
        self._engines = {}
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
        return f"{_flavor_name(self.session_id, 0)[:40].rstrip('-')}-line"

    def _task_net(self) -> str:
        """The member (task) machine's --net. A paired member is
        wire-only: the appliance holds the world, and the member reaches
        it over the wire -- so task egress is impossible except through
        the terminator, whatever allow_internet says. An unpaired
        airgapped member has no nic at all (--net none)."""
        if not self._paired:
            return self._topology.net
        return f"wire:{self._wire_name()}"

    def _ensure_terminator(self) -> None:
        """Stand the terminator appliance for the trial, once.

        The appliance is cella's terminator golden with titanium's
        constant ``/etc/cella-terminator.conf`` injected: --net
        world,wire, its world membrane judged by titanium's engine
        serving the appliance border (the world leg, by name --
        terminator.py). It runs for the trial's whole life; a
        background thread thaws it through every park (the park is the
        freeze, and the appliance parks on every DNS and world flow it
        forwards -- standing memory keeps the hot paths live).
        """
        if self._term is not None:
            return
        assert self._work is not None
        ca_path = pair_ca_path(Path.home())
        if not ca_path.is_file():
            raise CellaError(
                f"no pair CA at {ca_path}: the terminator golden is not "
                "built (re-run `make .cella`)"
            )
        # Titanium's appliance flavor: a copy of the terminator golden
        # with the conf injected. The conf is constant, so this is a
        # per-trial rebuild of one fixed template.
        name = f"{_flavor_name(self.session_id, 0)[:38].rstrip('-')}-term"
        with staging_flavor_dir(home=None) as staging:
            artifact = staging / ROOTFS_ARTIFACT_NAME
            shutil.copyfile(terminator_golden_rootfs(Path.home()), artifact)
            place_into_ext4(
                image=artifact,
                boot_layer=BootLayer(entries=(terminator_conf_entry(),)),
                timeout_sec=self.task_env_config.build_timeout_sec,
            )
            write_manifest(
                staging,
                render_golden_json(
                    flavor=name,
                    sha3_256=sha3_256_file(artifact),
                    size_bytes=artifact.stat().st_size,
                    built_epoch=int(time.time()),
                    extra_fields={},
                ),
            )
            destination = rootfs_flavor_dir(name)
            if destination.exists():
                shutil.rmtree(destination)
            os.rename(staging, destination)
            staging.mkdir(exist_ok=True)

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
            name,
            "--mem-mb",
            "512",
            "--net",
            f"world,wire:{self._wire_name()}",
            "--root",
            "rw",
        )
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
        self._term_bridge = self._spawn_bridge(name, port)
        self._term = name
        self._term_stop.clear()
        self._term_thaw = threading.Thread(
            target=self._thaw_forever, args=(name,), daemon=True
        )
        self._term_thaw.start()
    def _thaw_forever(self, name: str) -> None:
        """The appliance's side of the freeze dance, for the machine's
        whole life: every park freezes it, every staged decision
        applies at the thaw."""
        state = self._machine_dir(name) / "state"
        while not self._term_stop.is_set():
            if state.is_file():
                time.sleep(0.5)
                try:
                    self._cella("thaw", name)
                except (CellaError, subprocess.TimeoutExpired):
                    pass
            else:
                time.sleep(0.5)

    def _stop_terminator(self) -> None:
        if self._term is None:
            return
        self._term_stop.set()
        if self._term_thaw is not None:
            self._term_thaw.join(timeout=10)
            self._term_thaw = None
        if self._term_bridge is not None:
            self._term_bridge.terminate()
            try:
                self._term_bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._term_bridge.kill()
            self._term_bridge = None
        # The appliance's border is judged too; its chronicle is part of
        # the run's record -- the world leg of every crossing the member
        # made, granted and refused, judged by name.
        self._preserve_chronicle(self._term)
        self._preserve_edge_log(self._term)
        self._retire_machine(self._term)
        flavor_dir = rootfs_flavor_dir(self._term)
        if flavor_dir.exists():
            shutil.rmtree(flavor_dir, ignore_errors=True)
        self._term = None

    def _publish_cycle_flavor(self, boot_layer: BootLayer) -> str:
        assert self._work is not None
        flavor = _flavor_name(self.session_id, self._cycle)
        size_bytes = (self._effective_storage_mb or 5120) * (1 << 20)
        with staging_flavor_dir(home=None) as staging:
            artifact = staging / ROOTFS_ARTIFACT_NAME
            if self._state_img is None:
                # Cycle 0: build from the prepared tar. Its bytes came
                # from the task's own image build, so podman's default
                # runtime is enough here.
                assert self._base_tar is not None
                build_ext4(
                    rootfs_tar=self._base_tar,
                    boot_layer=boot_layer,
                    size_bytes=size_bytes,
                    dest=artifact,
                    timeout_sec=self.task_env_config.build_timeout_sec,
                )
            else:
                # Disk to disk: the previous evidence copy is the next
                # filesystem; only the boot layer changes. Guest-produced
                # bytes, so placement parses them under krun.
                shutil.copyfile(self._state_img, artifact)
                place_into_ext4(
                    image=artifact,
                    boot_layer=boot_layer,
                    # The carried disk holds the previous cycle's result;
                    # left in place it would answer the completion poll
                    # before this cycle's guest ever ran (measured: every
                    # cycle after the first returned its predecessor's rc).
                    purge=(f"{_RUNNER_DIR}/result",),
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
    _RESULT_POLL_SEC = 5.0
    _POWEROFF_GRACE_SEC = 3.0

    def _result_landed(self, name: str) -> bool:
        """Whether ``/titanium/result/rc`` exists on the machine's disk.

        The canonical kernel has no ACPI poweroff and the VMM exits only
        on a CPU reset, so a finished guest *halts* and its VMM lives
        on (measured; see docs/environments/CELLA.md §4). Completion is
        therefore read where the guest put it: the result file. The
        runner writes rc last, after a sync of the outputs, so rc's
        presence proves everything before it is durable. The read is a
        targeted debugfs dump inside krun against the machine's own
        disk file -- cella documents the machine directory as plain
        files that may be read while a machine runs, and nothing is
        written.
        """
        assert self._work is not None
        disk = self._machine_dir(name) / "disk.img"
        out_dir = Path(tempfile.mkdtemp(prefix="cella-poll-", dir=self._work))
        try:
            script = (
                f'debugfs -R "dump {_RUNNER_DIR}/result/rc /out/rc" /img 2>/dev/null'
            )
            try:
                self._read_from_image(disk, script, out_dir)
            except PodmanError:
                return False
            return (out_dir / "rc").is_file()
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

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

    def _let_the_guest_halt(self, name: str) -> None:
        """After the result lands, walk the guest to its halt.

        The shutdown itself can park (a last frame on the way down),
        and the park is the freeze -- so keep thawing through it for a
        bounded moment. A guest that halts cleanly unmounts its
        filesystem; one stopped frozen leaves a dirty journal that the
        copy-side recovery must replay.
        """
        machine_dir = self._machine_dir(name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (machine_dir / "state").is_file():
                time.sleep(1.0)
                try:
                    self._cella("thaw", name)
                except CellaError:
                    pass
                continue
            if not self._vmm_alive(name):
                return
            time.sleep(self._POWEROFF_GRACE_SEC)

    def _wait_for_result(self, name: str, deadline: float) -> None:
        """Wait for the guest's result, thawing through the judgments.

        On a judged machine **the park is the freeze**: cella's
        egress rule (one-shot) cryo-freezes the machine at every park,
        the bridge lands the engine's decision into the verdict file
        while the machine lies frozen, and the decision applies at the
        thaw edge, in park order. The harness's side of that contract
        is exactly cella's own engine gate's: whenever the state file
        exists, thaw. A thaw that raced ahead of a staged decision is
        harmless -- the next park freezes again and the probes retry.
        """
        machine_dir = self._machine_dir(name)
        thaw_pause = 1.0
        last_poll = 0.0
        while time.monotonic() < deadline:
            if (machine_dir / "state").is_file():
                # A short breath first: the bridge tails at 200ms, so
                # the decision is usually staged before this thaw.
                time.sleep(thaw_pause)
                try:
                    self._cella("thaw", name)
                except CellaError:
                    pass  # raced a concurrent transition; loop decides
                continue
            alive = self._vmm_alive(name)
            if time.monotonic() - last_poll >= self._RESULT_POLL_SEC:
                last_poll = time.monotonic()
                if self._result_landed(name):
                    self._let_the_guest_halt(name)
                    return
            if not alive and not (machine_dir / "state").is_file():
                # Halted (or gone) with no frozen state and no result
                # yet: one final result check below decides.
                if self._result_landed(name):
                    return
                raise CellaError(
                    f"machine {name} ended without a result; vmm.log tail:\n"
                    + _tail(machine_dir / "vmm.log")
                )
            time.sleep(1.0)
        raise CellaError(
            f"machine {name} produced no result in time; vmm.log tail:\n"
            + _tail(machine_dir / "vmm.log")
        )

    def _read_from_image(
        self, image: Path, script: str, out_dir: Path, recover: bool = False
    ) -> None:
        """Run one read-only extraction script against *image* in a krun
        guest, with the image at ``/img`` and *out_dir* at ``/out``.

        The disk's contents are the workload's own writing, and ext4
        metadata is an attack surface like any parser input. A hostile
        filesystem compromises a disposable KVM guest with no network,
        never the host.

        ``recover=True`` replays the journal first (``e2fsck -p``) --
        for titanium's own copies only, never a machine's live disk: a
        guest frozen mid-shutdown leaves a dirty journal that a plain
        read-only mount refuses, and the copy is titanium's to repair.
        """
        builder = ensure_rootfs_builder_image(
            timeout_sec=self.task_env_config.build_timeout_sec
        )
        if recover:
            # -fy, not preen: a disk frozen mid-shutdown needs the full
            # replay, and a dirty journal left in the image would be
            # replayed by the NEXT guest kernel over whatever placement
            # wrote meanwhile -- measured as vanished uploads.
            script = "e2fsck -fy /img >/dev/null 2>&1 || true\n" + script
        run_podman(
            [
                "run",
                "--rm",
                "--runtime",
                "krun",
                "--network=none",
                "--device",
                "/dev/fuse",
                "-v",
                f"{image}:/img:{'z' if recover else 'ro,z'}",
                "-v",
                f"{out_dir}:/out:z",
                builder,
                "sh",
                "-c",
                script,
            ],
            timeout_sec=self.task_env_config.build_timeout_sec,
        )

    def _harvest(self, name: str) -> ExecResult:
        """Copy the still disk, read the result triple, keep the disk.

        The one host-side act is the byte copy. The copy *is* the next
        cycle's filesystem (state moves disk to disk), so the harvest
        reads only ``/titanium/result/{rc,stdout,stderr}`` -- three
        targeted ``debugfs`` dumps inside a krun guest, no mount, no
        full-tree pass.
        """
        assert self._work is not None
        disk = self._machine_dir(name) / "disk.img"
        evidence = self._work / f"state-{self._cycle + 1:04d}.img"
        shutil.copyfile(disk, evidence)

        out_dir = self._work / f"harvest-{self._cycle:04d}"
        out_dir.mkdir()
        script = "\n".join(
            f'debugfs -R "dump {_RUNNER_DIR}/result/{f} /out/{f}" /img 2>/dev/null'
            for f in ("rc", "stdout", "stderr")
        )
        try:
            self._read_from_image(evidence, script, out_dir, recover=True)
        except PodmanError as exc:
            raise CellaError(
                f"cycle {self._cycle}: evidence extraction failed: {exc}; "
                f"vmm.log tail:\n" + _tail(self._machine_dir(name) / "vmm.log")
            ) from exc

        try:
            return_code = int((out_dir / "rc").read_text().strip())
        except (OSError, ValueError) as exc:
            raise CellaError(
                f"cycle {self._cycle}: the guest halted without a result "
                f"({exc}); vmm.log tail:\n" + _tail(self._machine_dir(name) / "vmm.log")
            ) from exc
        stdout = _read_or_empty(out_dir / "stdout")
        stderr = _read_or_empty(out_dir / "stderr")

        previous = self._state_img
        self._state_img = evidence
        if previous is not None:
            previous.unlink(missing_ok=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        # Off the event loop: one exec is a whole VM boot/run/collect
        # cycle of blocking cella verbs, sleeps, and krun disk reads.
        # See _start_blocking on why this must not block the loop.
        return await asyncio.to_thread(
            self._exec_blocking, command, cwd, env, timeout_sec, user
        )

    def _exec_blocking(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        user: str | int | None,
    ) -> ExecResult:
        # Serialize with any lifecycle still running from an abandoned
        # (timed-out) await: hold the lock for the whole cycle so no two
        # execs share the mutable cycle state or a disk path.
        with self._lifecycle:
            return self._exec_locked(command, cwd, env, timeout_sec, user)

    def _exec_locked(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_sec: int | None,
        user: str | int | None,
    ) -> ExecResult:
        if self._base_tar is None:
            raise CellaError("exec before start: the environment is not running")
        job_entries = self._job_files(command, cwd, env, user)
        if self._paired:
            # A paired member bakes the pair CA and points its resolver
            # at the appliance; the prelude folds the CA into the trust
            # bundle. No proxy env -- the resolver is the interceptor.
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
            # Idempotent create: a trial-level retry builds a fresh
            # environment with the same session id, so it regenerates
            # this exact machine name while the failed attempt's machine
            # may still linger. Clear it first -- cella refuses to create
            # over an existing name, and the name is this trial's alone.
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
            self._cella("start", name)
            if judged:
                # E1's order: start, then open -- open is the membrane,
                # and from here every crossing parks for the engine.
                # One valve per machine: with the line, the wire parks
                # under the same open.
                self._cella("gateway", name, "open")
                # The member border is fixed and always enforced; the
                # world leg (and any dry-run collection) is the
                # appliance's, stood up once in start(). The engine is
                # this member machine's own -- a fresh one per cycle.
                port = self._ensure_engine(
                    name, self._member_policy_path(), dry_run=False
                )
                bridge = self._spawn_bridge(name, port)
            budget = (timeout_sec or _DEFAULT_EXEC_TIMEOUT_SEC) + _BOOT_MARGIN_SEC
            self._wait_for_result(name, time.monotonic() + budget)
            try:
                self._cella("stop", name)
            except CellaError:
                # A machine that ended frozen refuses stop; frozen is
                # still, which is all the harvest needs, and destroy
                # (in the finally) takes a frozen machine.
                if not (self._machine_dir(name) / "state").is_file():
                    raise
            result = self._harvest(name)
        finally:
            if bridge is not None:
                # The tether ends the bridge when the machine directory
                # goes; the kill is just promptness.
                bridge.terminate()
                try:
                    bridge.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    bridge.kill()
            # This member machine's engine dies with it (per cycle); the
            # appliance's, keyed by its own name, is untouched here.
            self._kill_engine(name)
            self._preserve_chronicle(name)
            self._preserve_edge_log(name)
            self._retire_machine(name)
            self._machine = None
            flavor_dir = rootfs_flavor_dir(flavor)
            if flavor_dir.exists():
                shutil.rmtree(flavor_dir, ignore_errors=True)
            # Advance the cycle even when this exec raised: the verifier
            # retries a failed exec, and reusing the cycle number would
            # name the next machine after this one -- which cella refuses
            # as "already exists". A fresh number per attempt, always.
            self._cycle += 1
        # Success only (skipped when the exec raised): the queued uploads
        # were baked into this cycle and are done; a retry after a failure
        # keeps them so it re-bakes the same inputs.
        self._pending = []
        self._pending_paths = set()
        return result

    # The per-machine files that make the run auditable: the Event
    # chronicle, the Decision record, the witnessed verb book, the
    # standing memories the engine planted, the names the ratchet
    # learned, the VMM's own execution log (boot and the freeze/thaw
    # timings -- the cryogenic record), and the small state markers.
    # Destroy takes them with the machine, so they are copied out first
    # -- the rung's whole point is the record of every crossing the
    # workload attempted, granted and refused. What is deliberately left
    # behind: disk.img and ram.img (gigabytes, and a run-to-completion
    # machine has nothing to resume -- see the guide's note on forensic
    # archival), and the transient sockets and pid files.
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

    # The framed-protobuf books cella's ``--dump`` renders to text. The
    # manifest is already JSON; the disk and transients are not audit
    # evidence. ``--dump`` keys membrane-memory on its basename, which
    # the preserved copy keeps.
    _CHRONICLE_DUMPABLE = ("network/ledger", "verdict", "audit", "membrane-memory")

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
        """End a machine's life at teardown. `teardown` (the default)
        destroys it. `archive` stops it and keeps it as a cella artifact
        (`cella archive`), so it can be thawed or inspected later. The
        evidence is already copied out either way (the chronicle); this
        only decides whether the machine itself survives."""
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
        if self._state_img is not None:
            self._image_file_read(source_path, Path(target_path))
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

    def _image_file_read(self, source_path: str, target: Path) -> None:
        assert self._work is not None and self._state_img is not None
        guest = str(PurePosixPath("/") / str(source_path).lstrip("/"))
        out_dir = Path(tempfile.mkdtemp(prefix="cella-read-", dir=self._work))
        try:
            script = (
                f'debugfs -R "dump {_shell_quote(guest)} /out/file" /img 2>/dev/null'
            )
            self._read_from_image(self._state_img, script, out_dir, recover=True)
            extracted = out_dir / "file"
            if not extracted.is_file():
                raise FileNotFoundError(f"{source_path} is not in the evidence tree")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extracted, target)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _image_dir_read(self, source_dir: str, target: Path) -> None:
        assert self._work is not None and self._state_img is not None
        guest = str(PurePosixPath("/") / str(source_dir).lstrip("/"))
        out_dir = Path(tempfile.mkdtemp(prefix="cella-read-", dir=self._work))
        try:
            # A directory needs traversal, so this read mounts -- still
            # read-only, still inside krun, still only the asked-for
            # subtree tarred out.
            script = (
                "set -eu\n"
                "mkdir -p /work/mnt\n"
                "fuse2fs -o fakeroot,ro /img /work/mnt\n"
                f"if [ -d /work/mnt{guest} ]; then\n"
                f"  tar -cpf /out/dir.tar -C /work/mnt{guest} .\n"
                "fi\n"
                "umount /work/mnt\n"
            )
            self._read_from_image(self._state_img, script, out_dir, recover=True)
            bundle = out_dir / "dir.tar"
            target.mkdir(parents=True, exist_ok=True)
            if not bundle.is_file():
                # An absent directory downloads as empty, matching the
                # tolerant log-collection paths in the trial flow.
                return
            with tarfile.open(bundle, "r:") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    destination = target / member.name.lstrip("./")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    with handle, destination.open("wb") as sink:
                        shutil.copyfileobj(handle, sink)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def _download_dir_sync(self, source_dir: str, target_dir: Path | str) -> None:
        if self._state_img is not None:
            self._image_dir_read(source_dir, Path(target_dir))
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
            self._stop_terminator()
            self._stop_engines()
            if self._machine is not None:
                self._retire_machine(self._machine)
                self._machine = None
            if delete and self._work is not None:
                shutil.rmtree(self._work, ignore_errors=True)
                self._work = None
                self._base_tar = None
                self._state_img = None


def _flavor_name(session_id: str, cycle: int) -> str:
    """A name valid as both a flavor and a machine name.

    The machine name is the stricter contract: cella accepts only
    lowercase letters, digits, and dashes there, and the cycle's
    machine is named after its flavor. Runs of anything else collapse
    to one dash so two session ids cannot alias by punctuation alone.
    """
    safe = re.sub(r"[^a-z0-9]+", "-", session_id.lower())
    return f"titanium-{safe[:40].strip('-') or 'trial'}-c{cycle:04d}"


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
