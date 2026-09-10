"""The cella environment: titanium driving cella as its runtime.

This module grows into the ``--env cella`` environment class. Its first
resident is the one decision every cella machine is created under: the
task's ``allow_internet`` flag **defines the network topology**, not a
firewall posture inside one.

``allow_internet`` is harbor's knob -- the task author's declaration,
which every rung must honor however it can. On the cella rung the two
values are two different machines:

- ``false`` -- ``--net none``. No nic exists. Not a shut valve on a
  network: no translator, no membrane traffic, no ledger, and none of
  the judgment machinery (no ``gateway open``, no engine, no bridge).
- ``true`` -- ``--net world``, then ``cella gateway <vm> open`` after
  start. Open is the membrane, not a free path: every crossing parks
  for a decision, and the engine enforcing the task's ``cella.policy``
  is what answers.

The two knobs are orthogonal, and stay so: harbor's flag picks which
of these machines exists, and ``cella.policy`` states the egress and
ingress rules at a border. Neither reads the other -- an engine only
runs where a border exists, so it never consults the flag, and the
flag never reaches into a grant.

The mapping is total and closed: there is no third topology, and no
kwarg reopens the question somewhere else.
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

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
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

    Airgapped tasks only, for now: ``allow_internet = true`` (the
    judged world nic, the policy engine, the bridge) is the -www leg
    and raises until it lands.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._topology = network_topology(self.task_env_config.allow_internet)
        if self._topology.judged:
            raise NotImplementedError(
                "The cella environment runs airgapped tasks only so far: "
                "allow_internet=true needs the judged world nic, which is "
                "the -www leg of smoke-cella-policy-engine."
            )
        self._work: Path | None = None
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
        self.preflight()
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
        self._queue_file(Path(source_path), str(PurePosixPath(target_path)))

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
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

        run_as = user if user is not None else self.default_user
        exports = "".join(
            f"export {key}={_shell_quote(value)}\n" for key, value in merged_env.items()
        )
        if run_as in (None, 0, "0", "root"):
            invoke = f"bash {_RUNNER_DIR}/command.sh"
        else:
            invoke = (
                f"runuser -u {_shell_quote(str(run_as))} -- "
                f"bash {_RUNNER_DIR}/command.sh"
            )
        job = (
            "#!/bin/bash\n"
            "# Generated by titanium's cella environment: one exec, one boot.\n"
            f"mkdir -p {_RUNNER_DIR}/result /logs/agent /logs/verifier /logs/artifacts\n"
            f"cd {_shell_quote(effective_cwd)} || cd /\n"
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

    def _wait_for_result(self, name: str, deadline: float) -> None:
        pid_path = self._machine_dir(name) / "pid"
        while time.monotonic() < deadline:
            pid_text = None
            try:
                pid_text = pid_path.read_text().strip()
            except OSError:
                return
            if pid_text:
                try:
                    os.kill(int(pid_text), 0)
                except (ProcessLookupError, ValueError):
                    return
                except PermissionError:
                    pass
            if self._result_landed(name):
                # Give the guest its poweroff: the filesystem quiesces
                # (sync, unmount, remount-ro) before the host stops the
                # halted machine.
                time.sleep(self._POWEROFF_GRACE_SEC)
                return
            time.sleep(self._RESULT_POLL_SEC)
        raise CellaError(
            f"machine {name} produced no result in time; vmm.log tail:\n"
            + _tail(self._machine_dir(name) / "vmm.log")
        )

    def _read_from_image(self, image: Path, script: str, out_dir: Path) -> None:
        """Run one read-only extraction script against *image* in a krun
        guest, with the image at ``/img`` and *out_dir* at ``/out``.

        The disk's contents are the workload's own writing, and ext4
        metadata is an attack surface like any parser input. A hostile
        filesystem compromises a disposable KVM guest with no network,
        never the host.
        """
        builder = ensure_rootfs_builder_image(
            timeout_sec=self.task_env_config.build_timeout_sec
        )
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
                f"{image}:/img:ro,z",
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
            self._read_from_image(evidence, script, out_dir)
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
        if self._base_tar is None:
            raise CellaError("exec before start: the environment is not running")
        boot_layer = BootLayer(
            entries=tuple(self._pending)
            + tuple(self._job_files(command, cwd, env, user))
        )
        flavor = self._publish_cycle_flavor(boot_layer)
        name = flavor
        self._machine = name
        memory_mb = self._effective_memory_mb or 1024
        try:
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
                self._topology.net,
                "--root",
                "rw",
            )
            self._cella("start", name)
            budget = (timeout_sec or _DEFAULT_EXEC_TIMEOUT_SEC) + _BOOT_MARGIN_SEC
            self._wait_for_result(name, time.monotonic() + budget)
            self._cella("stop", name)
            result = self._harvest(name)
        finally:
            self._destroy_quietly(name)
            self._machine = None
            flavor_dir = rootfs_flavor_dir(flavor)
            if flavor_dir.exists():
                shutil.rmtree(flavor_dir, ignore_errors=True)
        self._pending = []
        self._pending_paths = set()
        self._cycle += 1
        return result

    def _destroy_quietly(self, name: str) -> None:
        for verb in ("stop", "destroy"):
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
        self._download_file_sync(source_path, target_path)

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
        self._download_dir_sync(source_dir, target_dir)

    def _image_file_read(self, source_path: str, target: Path) -> None:
        assert self._work is not None and self._state_img is not None
        guest = str(PurePosixPath("/") / str(source_path).lstrip("/"))
        out_dir = Path(tempfile.mkdtemp(prefix="cella-read-", dir=self._work))
        try:
            script = (
                f'debugfs -R "dump {_shell_quote(guest)} /out/file" /img 2>/dev/null'
            )
            self._read_from_image(self._state_img, script, out_dir)
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
            self._read_from_image(self._state_img, script, out_dir)
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
        if self._machine is not None:
            self._destroy_quietly(self._machine)
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
