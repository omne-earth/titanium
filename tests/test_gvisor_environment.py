"""Unit tests for the first-class gVisor environment (``--env gvisor``).

Every Docker interaction is mocked: these tests must pass on a host with no
gVisor installed and no Docker daemon running.
"""

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pier.environments.base import ExecResult
from pier.environments.docker.docker import DockerEnvironment
from pier.environments.docker.docker_unix import UnixOps
from pier.environments.gvisor import runtime as gvisor_runtime
from pier.environments.gvisor.environment import GVisorEnvironment, VerificationState
from pier.environments.gvisor.transfer import (
    GVisorUnixOps,
    safe_copy_tree,
    safe_place_file,
)
from pier.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from pier.models.task.config import TaskOS
from pier.models.trial.paths import TrialPaths

GVISOR_MODULE = "pier.environments.gvisor.environment"

MAIN_ID = "a" * 64
PROXY_ID = "b" * 64
STALE_MAIN_ID = "c" * 64

DOWN = ["down", "--remove-orphans"]

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the gVisor environment requires a Linux host"
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, None

    def terminate(self):  # pragma: no cover - timeout path not exercised
        pass

    def kill(self):  # pragma: no cover - timeout path not exercised
        pass


class RecordingExec:
    """Stand-in for ``asyncio.create_subprocess_exec`` that records argv.

    ``stage_root`` opts into simulating the writable staging mount, so the real
    ``exec`` code path can be driven end to end for downloads too.
    """

    _OUT_RE = re.compile(r"/\.pier-stage/out/[0-9a-f]+")

    def __init__(self, stdout: bytes = b"", returncode: int = 0, stage_root=None):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._stage_root = stage_root

    async def __call__(self, *argv, env=None, **kwargs):
        call = [str(arg) for arg in argv]
        self.calls.append(call)
        if self._stage_root is not None and call:
            match = self._OUT_RE.search(call[-1])
            if match:
                host_dir = self._stage_root / match.group(0).rsplit("/", 1)[1]
                host_dir.mkdir(parents=True, exist_ok=True)
                (host_dir / "result.txt").write_text("exported")
        return FakeProcess(self._stdout, self._returncode)


class FakeSandbox:
    """Records ``exec`` calls and simulates the staging mounts' host side.

    Export commands name a ``/.pier-stage/out/<op>`` directory; the fake maps
    that back to its host path and materialises ``export`` there, which is what
    the real sandbox would do through the writable bind mount.
    """

    _OUT_RE = re.compile(r"/\.pier-stage/out/[0-9a-f]+")
    _IN_RE = re.compile(r"/\.pier-stage/in/[0-9a-f]+")

    def __init__(self, env, *, export: dict[str, str] | None = None, return_code=0):
        self._env = env
        self.commands: list[str] = []
        self.users: list[object] = []
        self.staged_uploads: list[list[str]] = []
        self.export = export or {}
        self.return_code = return_code

    async def __call__(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append(command)
        self.users.append(user)

        upload = self._IN_RE.search(command)
        if upload:
            host_dir = self._env.stage_in / upload.group(0).rsplit("/", 1)[1]
            self.staged_uploads.append(
                sorted(
                    p.relative_to(host_dir).as_posix()
                    for p in host_dir.rglob("*")
                    if host_dir.exists()
                )
            )

        download = self._OUT_RE.search(command)
        if download and self.return_code == 0:
            host_dir = self._env.stage_out / download.group(0).rsplit("/", 1)[1]
            host_dir.mkdir(parents=True, exist_ok=True)
            for relative, content in self.export.items():
                target = host_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

        return ExecResult(stdout="", stderr=None, return_code=self.return_code)

    def op_dirs(self) -> list[str]:
        found = []
        for command in self.commands:
            match = self._OUT_RE.search(command) or self._IN_RE.search(command)
            if match:
                found.append(match.group(0))
        return found


class ExecutingSandbox:
    """Runs the generated shell command for real against the host staging dirs.

    Metadata preservation is a runtime property, so string-matching the command
    cannot prove it. Container staging paths are rewritten to their host
    equivalents and ``chown`` is stubbed on PATH (the tests do not run as root),
    which leaves the copy semantics themselves genuinely exercised.
    """

    def __init__(self, env, tmp_path):
        self._env = env
        self.commands: list[str] = []
        self.users: list[object] = []
        self.chown_log = tmp_path / "chown.log"
        self._bin_dir = tmp_path / "fakebin"
        self._bin_dir.mkdir(exist_ok=True)
        chown = self._bin_dir / "chown"
        chown.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.chown_log}\nexit 0\n'
        )
        chown.chmod(0o755)

    async def __call__(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append(command)
        self.users.append(user)
        rewritten = command.replace(
            str(gvisor_runtime.STAGE_IN), str(self._env.stage_in)
        ).replace(str(gvisor_runtime.STAGE_OUT), str(self._env.stage_out))
        completed = subprocess.run(
            ["bash", "-c", rewritten],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{self._bin_dir}:{os.environ['PATH']}"},
        )
        return ExecResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            return_code=completed.returncode,
        )

    def chowned(self) -> list[str]:
        if not self.chown_log.exists():
            return []
        return [
            line.strip()
            for line in self.chown_log.read_text().splitlines()
            if line.strip()
        ]


def _paths(tmp_path):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return environment_dir, trial_paths


def _make_env(cls, tmp_path, *, task_config=None, **kwargs):
    environment_dir, trial_paths = _paths(tmp_path)
    if task_config is None:
        task_config = TaskEnvironmentConfig(allow_internet=False)

    return cls(
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=task_config,
        **kwargs,
    )


def make_gvisor_env(tmp_path, **kwargs) -> GVisorEnvironment:
    return _make_env(GVisorEnvironment, tmp_path, **kwargs)


def make_plain_env(tmp_path, **kwargs) -> DockerEnvironment:
    return _make_env(DockerEnvironment, tmp_path, **kwargs)


def rendered_main(env) -> dict:
    return json.loads(env._compose_override_path.read_text())["services"]["main"]


def stub_registered_runtime(monkeypatch, runtimes=("runc", "runsc")):
    monkeypatch.setattr(
        gvisor_runtime, "engine_runtimes", lambda *a, **k: set(runtimes)
    )


def _stub_no_leftover_resources(monkeypatch):
    """No container or network is labeled for this project -- the common case."""

    async def no_containers(project, cli="docker"):
        return []

    async def no_networks(project, cli="docker"):
        return []

    monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", no_containers)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", no_networks)


# ---------------------------------------------------------------------------
# Compose override
# ---------------------------------------------------------------------------


def test_plain_docker_compose_paths_carry_no_gvisor_override(tmp_path):
    plain = make_plain_env(tmp_path / "plain")
    sandboxed = make_gvisor_env(tmp_path / "sandboxed")
    sandboxed._prepare_gvisor()

    plain_names = [p.name for p in plain._docker_compose_paths]
    sandboxed_names = [p.name for p in sandboxed._docker_compose_paths]

    # Identical but for the single appended override.
    assert sandboxed_names == plain_names + [gvisor_runtime.COMPOSE_OVERRIDE_NAME]
    assert not any("gvisor" in name for name in plain_names)


def test_gvisor_override_is_appended_last(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env._mounts_compose_path = env._write_mounts_compose_file()

    paths = env._docker_compose_paths
    assert paths[-1] == env._compose_override_path


def test_gvisor_override_renders_runtime_and_no_new_privileges(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    main = rendered_main(env)
    assert main["runtime"] == "runsc"
    assert main["security_opt"] == ["no-new-privileges:true"]


def test_gvisor_override_does_not_disable_selinux_labels(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    assert "label=disable" not in json.dumps(rendered_main(env))


def test_gvisor_override_never_sets_privileged_or_host_namespaces(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    main = rendered_main(env)
    for forbidden in (
        "privileged",
        "cap_add",
        "devices",
        "pid",
        "ipc",
        "network_mode",
    ):
        assert forbidden not in main


def test_gvisor_override_renders_both_staging_mounts(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    volumes = rendered_main(env)["volumes"]
    assert volumes == [
        {
            "type": "bind",
            "source": str(env.stage_in.resolve()),
            "target": "/.pier-stage/in",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(env.stage_out.resolve()),
            "target": "/.pier-stage/out",
        },
    ]
    assert env.stage_in.is_dir()
    assert env.stage_out.is_dir()


def test_custom_runtime_is_rendered(tmp_path):
    env = make_gvisor_env(tmp_path, runtime="runsc-custom")
    env._prepare_gvisor()

    assert rendered_main(env)["runtime"] == "runsc-custom"
    assert env.runtime == "runsc-custom"


def test_different_trial_dirs_never_share_staging_paths(tmp_path):
    first = make_gvisor_env(tmp_path / "one")
    second = make_gvisor_env(tmp_path / "two")

    assert first.stage_in != second.stage_in
    assert first.stage_out != second.stage_out
    assert first.stage_in.is_relative_to(first.trial_paths.trial_dir)
    assert second.stage_out.is_relative_to(second.trial_paths.trial_dir)


# ---------------------------------------------------------------------------
# Fail-closed configuration checks
# ---------------------------------------------------------------------------


def test_task_compose_file_is_rejected(tmp_path):
    environment_dir, trial_paths = _paths(tmp_path)
    (environment_dir / "docker-compose.yaml").write_text("services:\n  main: {}\n")

    with pytest.raises(ValueError, match="not docker-compose tasks"):
        GVisorEnvironment(
            environment_dir=environment_dir,
            environment_name="hello-world",
            session_id="hello-world__abc123",
            trial_paths=trial_paths,
            task_env_config=TaskEnvironmentConfig(allow_internet=False),
        )


def test_gvisor_does_not_advertise_docker_compose_support(tmp_path):
    env = make_gvisor_env(tmp_path)

    assert env.capabilities.docker_compose is False
    assert make_plain_env(tmp_path / "plain").capabilities.docker_compose is True


def test_windows_task_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="does not support Windows containers"):
        make_gvisor_env(
            tmp_path,
            task_config=TaskEnvironmentConfig(allow_internet=False, os=TaskOS.WINDOWS),
        )


def test_gvisor_does_not_advertise_windows_support(tmp_path):
    assert make_gvisor_env(tmp_path).capabilities.windows is False


def test_non_linux_host_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(RuntimeError, match="requires a Linux host"):
        make_gvisor_env(tmp_path)


def test_plain_docker_still_accepts_allow_internet_and_compose(tmp_path):
    env = make_plain_env(
        tmp_path, task_config=TaskEnvironmentConfig(allow_internet=True)
    )
    assert isinstance(env._platform, UnixOps)
    assert not isinstance(env._platform, GVisorUnixOps)
    assert env.capabilities.windows is True
    assert env.capabilities.docker_compose is True


def test_unregistered_runtime_is_rejected(monkeypatch):
    monkeypatch.setattr(gvisor_runtime, "engine_runtimes", lambda *a, **k: {"runc"})

    with pytest.raises(RuntimeError, match="requires the 'runsc' runtime"):
        gvisor_runtime.assert_runtime_registered("runsc")


def test_unqueryable_daemon_is_rejected(monkeypatch):
    monkeypatch.setattr(gvisor_runtime, "engine_runtimes", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="could not query the docker daemon"):
        gvisor_runtime.assert_runtime_registered("runsc")


def test_registered_runtime_passes(monkeypatch):
    stub_registered_runtime(monkeypatch)

    gvisor_runtime.assert_runtime_registered("runsc")


def test_start_checks_runtime_before_building(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    recorder = RecordingExec()
    monkeypatch.setattr(gvisor_runtime, "engine_runtimes", lambda *a, **k: {"runc"})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    with pytest.raises(RuntimeError, match="requires the 'runsc' runtime"):
        asyncio.run(env.start(force_build=False))

    assert recorder.calls == []


def test_preflight_requires_a_registered_runtime(monkeypatch):
    monkeypatch.setattr(DockerEnvironment, "preflight", classmethod(lambda cls: None))
    monkeypatch.setattr(gvisor_runtime, "engine_runtimes", lambda *a, **k: {"runc"})

    with pytest.raises(RuntimeError, match="requires the 'runsc' runtime"):
        GVisorEnvironment.preflight()


# ---------------------------------------------------------------------------
# Post-start runtime verification
# ---------------------------------------------------------------------------


def _stub_compose(env, monkeypatch, recorder: list[list[str]]):
    async def fake_compose(command, check=True, timeout_sec=None):
        recorder.append(list(command))
        if command[:2] == ["ps", "--quiet"]:
            return ExecResult(
                stdout=MAIN_ID if command[2] == "main" else PROXY_ID,
                return_code=0,
            )
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)


def _stub_runtime(monkeypatch, resolver):
    async def fake_runtime(container_id, cli="docker"):
        return resolver(container_id)

    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", fake_runtime)


def _stub_networks(monkeypatch, resolver):
    async def fake_networks(container_id, cli="docker"):
        return resolver(container_id)

    monkeypatch.setattr(f"{GVISOR_MODULE}.container_networks", fake_networks)


def test_main_runtime_mismatch_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runc")

    with pytest.raises(RuntimeError, match="Refusing to run untrusted code"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands
    assert env.verification_state is VerificationState.FAILED


def test_proxy_runtime_mismatch_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._egress_proxy_compose_path = tmp_path / "proxy.json"
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    # Both main and proxy -- the trusted proxy must not be sandboxed.
    _stub_runtime(monkeypatch, lambda cid: "runsc")

    with pytest.raises(RuntimeError, match="egress proxy is running under"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands


def test_verification_failure_runs_compose_down(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    # No container ID resolvable -> verification cannot succeed.
    with pytest.raises(RuntimeError, match="could not resolve the 'main' container"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands


def test_missing_proxy_container_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._egress_proxy_compose_path = tmp_path / "proxy.json"
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        if command[:2] == ["ps", "--quiet"]:
            # main resolves, the declared proxy does not.
            return ExecResult(
                stdout=MAIN_ID if command[2] == "main" else "", return_code=0
            )
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    _stub_runtime(monkeypatch, lambda cid: "runsc")

    with pytest.raises(RuntimeError, match="could not resolve the 'pier-egress-proxy'"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands


def test_uninspectable_proxy_runtime_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._egress_proxy_compose_path = tmp_path / "proxy.json"
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runsc" if cid == MAIN_ID else None)

    with pytest.raises(RuntimeError, match="could not determine the runtime"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands


def test_successful_verification_does_not_tear_down(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runsc")

    asyncio.run(env._ensure_verified())

    assert DOWN not in commands
    assert env.verification_state is VerificationState.READY


# ---------------------------------------------------------------------------
# Verify-before-exec gate
# ---------------------------------------------------------------------------


def _verified_env(tmp_path, monkeypatch, *, task_config=None):
    """A gVisor environment whose compose and inspect calls are stubbed.

    Returns ``(env, calls)`` where *calls* records, in order, every host-side
    inspection and every compose command -- including the ``exec`` that
    ``DockerEnvironment`` issues to run a command inside the sandbox.
    """
    env = make_gvisor_env(tmp_path, task_config=task_config)
    calls: list[str] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        calls.append(" ".join(str(part) for part in command))
        if command[:2] == ["ps", "--quiet"]:
            return ExecResult(stdout=MAIN_ID, return_code=0)
        return ExecResult(stdout="", return_code=0)

    async def fake_runtime(container_id, cli="docker"):
        calls.append(f"inspect-runtime {container_id}")
        return "runsc"

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", fake_runtime)
    return env, calls


def test_no_command_runs_before_runtime_verification(tmp_path, monkeypatch):
    env, calls = _verified_env(tmp_path, monkeypatch)

    asyncio.run(env.exec("echo hello"))

    inspected = next(i for i, c in enumerate(calls) if c.startswith("inspect-runtime"))
    executed = next(i for i, c in enumerate(calls) if c.startswith("exec "))
    assert inspected < executed, calls


def test_start_verifies_before_the_docker_chmod(tmp_path, monkeypatch):
    """The chmod DockerEnvironment issues after `up` is gated like any command."""
    env, calls = _verified_env(tmp_path, monkeypatch)
    stub_registered_runtime(monkeypatch)
    monkeypatch.setattr(env, "_validate_daemon_mode", lambda: None)

    async def no_image_check(image_name):
        return None

    monkeypatch.setattr(env, "_validate_image_os", no_image_check)

    asyncio.run(env.start(force_build=False))

    inspected = next(i for i, c in enumerate(calls) if c.startswith("inspect-runtime"))
    chmod = next(i for i, c in enumerate(calls) if "chmod 777" in c)
    up = next(i for i, c in enumerate(calls) if c.startswith("up "))
    assert up < inspected < chmod, calls
    assert env.verification_state is VerificationState.READY


def test_verification_runs_once_across_repeated_execs(tmp_path, monkeypatch):
    env, calls = _verified_env(tmp_path, monkeypatch)

    async def drive():
        for _ in range(5):
            await env.exec("echo hello")

    asyncio.run(drive())

    assert sum(1 for c in calls if c.startswith("inspect-runtime")) == 1


def test_concurrent_execs_verify_only_once(tmp_path, monkeypatch):
    env, calls = _verified_env(tmp_path, monkeypatch)

    async def drive():
        await asyncio.gather(*(env.exec("echo hello") for _ in range(8)))

    asyncio.run(drive())

    assert sum(1 for c in calls if c.startswith("inspect-runtime")) == 1
    assert env.verification_state is VerificationState.READY


def test_concurrent_execs_never_observe_a_partial_state(tmp_path, monkeypatch):
    """A concurrent caller must fail, not slip through mid-verification."""
    env = make_gvisor_env(tmp_path)
    observed: list[VerificationState] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        if command[:2] == ["ps", "--quiet"]:
            await asyncio.sleep(0)  # let the other callers queue up
            return ExecResult(stdout=MAIN_ID, return_code=0)
        return ExecResult(stdout="", return_code=0)

    async def fake_runtime(container_id, cli="docker"):
        await asyncio.sleep(0)
        return "runc"  # mismatch -> the whole environment must fail

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", fake_runtime)

    async def one():
        try:
            await env.exec("echo hello")
        except RuntimeError:
            observed.append(env.verification_state)
            return "failed"
        return "ran"

    async def drive():
        return await asyncio.gather(*(one() for _ in range(6)))

    results = asyncio.run(drive())

    assert results == ["failed"] * 6
    assert all(state is VerificationState.FAILED for state in observed)


def test_failed_verification_is_never_retried_or_marked_ready(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    attempts: list[str] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        if command[:2] == ["ps", "--quiet"]:
            attempts.append("ps")
            return ExecResult(stdout="", return_code=0)
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    async def drive():
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await env.exec("echo hello")

    asyncio.run(drive())

    assert len(attempts) == 1, "verification must not be retried after failing"
    assert env.verification_state is VerificationState.FAILED


def test_verification_does_not_recurse_through_its_own_execs(tmp_path, monkeypatch):
    """resolv.conf repair and probing run through exec() without re-entering."""
    env, calls = _verified_env(
        tmp_path, monkeypatch, task_config=TaskEnvironmentConfig(allow_internet=True)
    )
    env._resolvers = ["1.1.1.1"]
    env._state = VerificationState.NOT_STARTED

    sandbox_output = {
        "cat": "nameserver 127.0.0.11\nsearch example.internal\n",
        "printf": "",
    }
    seen: list[str] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        joined = " ".join(str(part) for part in command)
        calls.append(joined)
        if command[:2] == ["ps", "--quiet"]:
            return ExecResult(stdout=MAIN_ID, return_code=0)
        if command[0] == "exec":
            inner = command[-1]
            seen.append(inner)
            if inner.startswith("cat "):
                # After the rewrite the sandbox reports the new resolver.
                text = (
                    "nameserver 1.1.1.1\nsearch example.internal\n"
                    if any(s.startswith("printf") for s in seen)
                    else sandbox_output["cat"]
                )
                return ExecResult(stdout=text, return_code=0)
            if inner.startswith("printf"):
                return ExecResult(stdout="", return_code=0)
            return ExecResult(stdout="PIER_GVISOR_NET_OK\n", return_code=0)
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    asyncio.run(env._ensure_verified())

    # One host-side inspection, no matter how many execs verification itself made.
    assert sum(1 for c in calls if c.startswith("inspect-runtime")) == 1
    assert len(seen) >= 3  # read, rewrite, confirm, probe
    assert env.verification_state is VerificationState.READY


def test_dns_repair_cannot_run_before_the_runtime_is_verified(tmp_path):
    env = make_gvisor_env(
        tmp_path, task_config=TaskEnvironmentConfig(allow_internet=True)
    )

    with pytest.raises(RuntimeError, match="has not been verified"):
        asyncio.run(env._normalize_sandbox_dns())

    with pytest.raises(RuntimeError, match="has not been verified"):
        asyncio.run(env._probe_connectivity())


def test_teardown_is_idempotent(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    _stub_no_leftover_resources(monkeypatch)

    async def drive():
        await env._teardown()
        await env._teardown()

    asyncio.run(drive())

    assert commands.count(DOWN) == 1
    assert not env.stage_in.exists()


def test_stop_does_not_trigger_verification(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    inspected: list[str] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    async def fake_runtime(container_id, cli="docker"):
        inspected.append(container_id)
        return "runsc"

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", fake_runtime)

    asyncio.run(env.stop(delete=False))

    assert inspected == []
    assert env.verification_state is VerificationState.NOT_STARTED


def test_stop_after_a_verified_run_still_chowns_the_logs(tmp_path, monkeypatch):
    """The READY short-circuit must win over the stopping guard.

    DockerEnvironment.stop() chowns the bind-mounted logs through exec() so the
    host user can read them afterwards. If the stopping guard were checked
    before READY, that chown would silently stop happening on every successful
    run.
    """
    env, calls = _verified_env(tmp_path, monkeypatch)
    env._prepare_gvisor()

    async def drive():
        await env.exec("echo hello")  # verify
        await env.stop(delete=False)

    asyncio.run(drive())

    assert env.verification_state is VerificationState.READY
    assert any("chown" in call for call in calls), calls


# ---------------------------------------------------------------------------
# Non-running 'main' container: discovery and rejection
# ---------------------------------------------------------------------------


def test_compose_container_discovery_includes_stopped_containers(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        return ExecResult(stdout=MAIN_ID, return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    result = asyncio.run(env._compose_container_id("main", include_stopped=True))

    assert result == MAIN_ID
    assert commands == [["ps", "--all", "--quiet", "main"]]


def test_created_main_container_is_found_and_rejected_with_its_state(
    tmp_path, monkeypatch
):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        if command == ["ps", "--quiet", "main"]:
            return ExecResult(stdout="", return_code=0)  # not running
        if command == ["ps", "--all", "--quiet", "main"]:
            return ExecResult(stdout=STALE_MAIN_ID, return_code=0)
        return ExecResult(stdout="", return_code=0)

    async def fake_state(container_id, cli="docker"):
        assert container_id == STALE_MAIN_ID
        return {"Status": "created", "Error": ""}

    async def fake_runtime(container_id, cli="docker"):
        return "runsc"

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_state", fake_state)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", fake_runtime)
    _stub_no_leftover_resources(monkeypatch)

    with pytest.raises(RuntimeError, match="not running") as excinfo:
        asyncio.run(env._ensure_verified())

    message = str(excinfo.value)
    # Found -- not reported as unresolvable -- and rejected with real detail.
    assert STALE_MAIN_ID in message
    assert "created" in message
    assert "runsc" in message
    # Discovery included the stopped container.
    assert ["ps", "--all", "--quiet", "main"] in commands
    # Never executed anything inside the unverified/non-running container.
    assert not any(c and c[0] == "exec" for c in commands)


def test_running_main_container_never_triggers_stopped_discovery(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runsc")

    asyncio.run(env._ensure_verified())

    assert ["ps", "--all", "--quiet", "main"] not in commands


# ---------------------------------------------------------------------------
# Fail-closed teardown: exact-project discovery, targeted removal, retry
# ---------------------------------------------------------------------------


def test_teardown_removes_exact_project_containers_before_networks(
    tmp_path, monkeypatch
):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", stderr="conflict", return_code=1)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    containers_by_call = iter([["c1"], []])  # leftover, then verified gone
    networks_by_call = iter([["n1"], []])
    order: list[str] = []

    async def fake_project_containers(project, cli="docker"):
        assert project == env.project_name
        return next(containers_by_call)

    async def fake_project_networks(project, cli="docker"):
        assert project == env.project_name
        return next(networks_by_call)

    async def fake_remove_containers(ids, cli="docker"):
        order.append(("containers", tuple(ids)))

    async def fake_remove_networks(ids, cli="docker"):
        order.append(("networks", tuple(ids)))

    monkeypatch.setattr(
        f"{GVISOR_MODULE}.project_container_ids", fake_project_containers
    )
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", fake_project_networks)
    monkeypatch.setattr(f"{GVISOR_MODULE}.remove_containers", fake_remove_containers)
    monkeypatch.setattr(f"{GVISOR_MODULE}.remove_networks", fake_remove_networks)

    asyncio.run(env._teardown())

    assert order == [("containers", ("c1",)), ("networks", ("n1",))]
    assert env._torn_down is True


def test_compose_down_nonzero_return_code_is_detected_and_logged(
    tmp_path, monkeypatch, caplog
):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", stderr="network already exists", return_code=17)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    _stub_no_leftover_resources(monkeypatch)

    with caplog.at_level("WARNING"):
        asyncio.run(env._teardown())

    assert any("17" in record.message for record in caplog.records)
    assert env._torn_down is True


def test_teardown_can_be_retried_after_an_incomplete_cleanup(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    down_calls: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        if command == DOWN:
            down_calls.append(list(command))
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    state = {"stuck": True}

    async def flaky_containers(project, cli="docker"):
        return ["stuck-container"] if state["stuck"] else []

    async def no_networks(project, cli="docker"):
        return []

    async def noop_remove(*args, **kwargs):
        return None

    monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", flaky_containers)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", no_networks)
    monkeypatch.setattr(f"{GVISOR_MODULE}.remove_containers", noop_remove)

    with pytest.raises(RuntimeError, match="could not fully clean up"):
        asyncio.run(env._teardown())
    assert env._torn_down is False

    # The stuck resource is gone now; a retried teardown must do real work
    # again, not silently no-op because a previous attempt already ran.
    state["stuck"] = False
    asyncio.run(env._teardown())

    assert env._torn_down is True
    assert len(down_calls) == 2


def test_teardown_does_not_mark_complete_while_a_network_remains(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    async def no_containers(project, cli="docker"):
        return []

    async def stuck_networks(project, cli="docker"):
        return ["stuck-network"]

    async def noop_remove(*args, **kwargs):
        return None

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", no_containers)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", stuck_networks)
    monkeypatch.setattr(f"{GVISOR_MODULE}.remove_networks", noop_remove)

    with pytest.raises(RuntimeError, match="could not fully clean up"):
        asyncio.run(env._teardown())

    assert env._torn_down is False


def test_teardown_does_not_mark_complete_when_discovery_query_fails(
    tmp_path, monkeypatch
):
    """A daemon/query failure must never be read as 'nothing left to clean up'.

    If ``project_container_ids`` cannot even answer the question (daemon
    unreachable, docker missing, non-zero exit), _teardown must propagate that
    failure rather than treating the empty result as verified-clean.
    """
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    async def failing_containers(project, cli="docker"):
        raise RuntimeError(
            "could not list containers for Compose project: docker daemon unreachable"
        )

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", failing_containers)

    with pytest.raises(RuntimeError, match="daemon unreachable"):
        asyncio.run(env._teardown())

    assert env._torn_down is False


def test_teardown_does_not_mark_complete_when_final_verification_query_fails(
    tmp_path, monkeypatch
):
    """The final post-removal query failing must also block ``_torn_down``.

    Discovery and removal can all succeed while the *final* confirmation query
    (run after removal, to prove nothing remains) itself fails to answer --
    that must not be treated as "confirmed empty" either.
    """
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    calls = {"n": 0}

    async def containers_then_failure(project, cli="docker"):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # initial discovery: nothing to remove
        raise RuntimeError(
            "could not list containers for Compose project: query failed"
        )

    async def no_networks(project, cli="docker"):
        return []

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(
        f"{GVISOR_MODULE}.project_container_ids", containers_then_failure
    )
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", no_networks)

    with pytest.raises(RuntimeError, match="query failed"):
        asyncio.run(env._teardown())

    assert env._torn_down is False


def test_project_container_ids_filters_by_the_exact_project_label(monkeypatch):
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return f"{MAIN_ID}\n".encode(), b""

    async def fake_exec(*argv, **kwargs):
        calls.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(gvisor_runtime.project_container_ids("hello-world__abc123"))

    assert result == [MAIN_ID]
    assert calls == [
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=hello-world__abc123",
        ]
    ]


def test_project_network_ids_filters_by_the_exact_project_label(monkeypatch):
    calls: list[list[str]] = []
    network_id = "d" * 12

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return f"{network_id}\n".encode(), b""

    async def fake_exec(*argv, **kwargs):
        calls.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(gvisor_runtime.project_network_ids("hello-world__abc123"))

    assert result == [network_id]
    assert calls == [
        [
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=hello-world__abc123",
        ]
    ]


def test_project_container_ids_raises_on_nonzero_docker_query(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"Cannot connect to the Docker daemon"

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="could not list containers") as excinfo:
        asyncio.run(gvisor_runtime.project_container_ids("hello-world__abc123"))

    message = str(excinfo.value)
    assert "hello-world__abc123" in message
    assert "1" in message
    assert "Cannot connect to the Docker daemon" in message


def test_project_network_ids_raises_on_nonzero_docker_query(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"Cannot connect to the Docker daemon"

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="could not list networks") as excinfo:
        asyncio.run(gvisor_runtime.project_network_ids("hello-world__abc123"))

    message = str(excinfo.value)
    assert "hello-world__abc123" in message
    assert "1" in message
    assert "Cannot connect to the Docker daemon" in message


def test_remove_containers_raises_on_nonzero_docker_rm(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"No such container"

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="could not remove container") as excinfo:
        asyncio.run(gvisor_runtime.remove_containers([MAIN_ID]))

    assert "No such container" in str(excinfo.value)


def test_remove_networks_raises_on_nonzero_docker_network_rm(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"has active endpoints"

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="could not remove network") as excinfo:
        asyncio.run(gvisor_runtime.remove_networks(["net-1"]))

    assert "has active endpoints" in str(excinfo.value)


def test_remove_containers_and_networks_are_no_ops_for_empty_input(monkeypatch):
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        calls.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(gvisor_runtime.remove_containers([]))
    asyncio.run(gvisor_runtime.remove_networks([]))

    # Never invoked 'docker rm' / 'docker network rm' with no IDs -- a project
    # with nothing left over must never touch the docker CLI at all here.
    assert calls == []


# ---------------------------------------------------------------------------
# Startup failure, cancellation, and retry-safe lifecycle state
# ---------------------------------------------------------------------------


def test_start_failure_invokes_teardown(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    stub_registered_runtime(monkeypatch)

    class Boom(RuntimeError):
        pass

    async def failing_start(self, force_build):
        raise Boom("compose up failed")

    monkeypatch.setattr(DockerEnvironment, "start", failing_start)

    teardown_calls: list[bool] = []
    original_teardown = env._teardown

    async def spy_teardown():
        teardown_calls.append(True)
        return await original_teardown()

    monkeypatch.setattr(env, "_teardown", spy_teardown)
    _stub_no_leftover_resources(monkeypatch)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    with pytest.raises(Boom):
        asyncio.run(env.start(force_build=False))

    assert teardown_calls == [True]


def test_cancellation_during_start_invokes_teardown(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    stub_registered_runtime(monkeypatch)

    async def cancelled_start(self, force_build):
        raise asyncio.CancelledError()

    monkeypatch.setattr(DockerEnvironment, "start", cancelled_start)

    teardown_calls: list[bool] = []
    original_teardown = env._teardown

    async def spy_teardown():
        teardown_calls.append(True)
        return await original_teardown()

    monkeypatch.setattr(env, "_teardown", spy_teardown)
    _stub_no_leftover_resources(monkeypatch)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(env.start(force_build=False))

    assert teardown_calls == [True]


def test_start_reraises_the_original_exception_when_teardown_succeeds(
    tmp_path, monkeypatch
):
    env = make_gvisor_env(tmp_path)
    stub_registered_runtime(monkeypatch)

    class DistinctiveError(RuntimeError):
        pass

    async def failing_start(self, force_build):
        raise DistinctiveError("original startup failure")

    monkeypatch.setattr(DockerEnvironment, "start", failing_start)
    _stub_no_leftover_resources(monkeypatch)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    with pytest.raises(DistinctiveError, match="original startup failure"):
        asyncio.run(env.start(force_build=False))


def test_start_keeps_the_original_exception_primary_when_cleanup_also_fails(
    tmp_path, monkeypatch
):
    env = make_gvisor_env(tmp_path)
    stub_registered_runtime(monkeypatch)

    class DistinctiveError(RuntimeError):
        pass

    async def failing_start(self, force_build):
        raise DistinctiveError("original startup failure")

    monkeypatch.setattr(DockerEnvironment, "start", failing_start)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    async def stuck_containers(project, cli="docker"):
        return ["stuck-container"]

    async def no_networks(project, cli="docker"):
        return []

    async def noop_remove(*args, **kwargs):
        return None

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", stuck_containers)
    monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", no_networks)
    monkeypatch.setattr(f"{GVISOR_MODULE}.remove_containers", noop_remove)

    with pytest.raises(DistinctiveError, match="original startup failure") as excinfo:
        asyncio.run(env.start(force_build=False))

    # The original startup failure must remain the top-level exception, not
    # be replaced by a wrapper around the cleanup failure.
    assert isinstance(excinfo.value, DistinctiveError)
    notes = getattr(excinfo.value, "__notes__", [])
    assert any(env.project_name in note for note in notes)
    assert any("manual cleanup" in note.lower() for note in notes)


def test_reset_for_new_start_attempt_clears_stale_failure_and_teardown_state(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._state = VerificationState.FAILED
    env._failure = RuntimeError("boom")
    env._torn_down = True
    env._stopping = True
    stale_event = env._verified_event
    stale_event.set()

    env._reset_for_new_start_attempt()

    assert env.verification_state is VerificationState.NOT_STARTED
    assert env._failure is None
    assert env._torn_down is False
    assert env._stopping is False
    assert env._verified_event is not stale_event
    assert not env._verified_event.is_set()


def test_reset_for_new_start_attempt_resets_stopping_from_a_previous_stop(tmp_path):
    """A genuinely new start() must not inherit a prior stop()'s stopping state.

    Without this reset, a start() called after stop() on the same instance
    would find ``_stopping`` still True and have its own verification refuse
    to run (see _ensure_verified's stopping guard).
    """
    env = make_gvisor_env(tmp_path)
    env._stopping = True

    env._reset_for_new_start_attempt()

    assert env._stopping is False


def test_retried_start_on_same_instance_reverifies_instead_of_reusing_failed_state(
    tmp_path, monkeypatch
):
    """Mirrors Pier's own retry: TrialExecution.start_environment() calls
    ``start()`` again on the *same* environment instance after a timeout.

    The first attempt's ``super().start()`` (Compose ``up``) succeeds, but
    runtime verification fails, leaving ``verification_state`` at ``FAILED``.
    Without the fix, that terminal state would make the retry's
    ``_ensure_verified()`` refuse to even look at the second attempt's freshly
    started, correctly-sandboxed container.
    """
    env = make_gvisor_env(tmp_path)
    stub_registered_runtime(monkeypatch)
    monkeypatch.setattr(env, "_validate_daemon_mode", lambda: None)

    async def no_image_check(image_name):
        return None

    monkeypatch.setattr(env, "_validate_image_os", no_image_check)
    _stub_no_leftover_resources(monkeypatch)

    async def always_succeeds(self, force_build):
        return None

    monkeypatch.setattr(DockerEnvironment, "start", always_succeeds)

    async def fake_compose(command, check=True, timeout_sec=None):
        if command[:2] == ["ps", "--quiet"]:
            return ExecResult(stdout=MAIN_ID, return_code=0)
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    attempt = {"n": 0}

    async def flaky_runtime(container_id, cli="docker"):
        attempt["n"] += 1
        # First attempt: the daemon (implausibly) placed 'main' under runc.
        # Second attempt: it is correctly under runsc.
        return "runc" if attempt["n"] == 1 else "runsc"

    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", flaky_runtime)

    with pytest.raises(RuntimeError, match="Refusing to run untrusted code"):
        asyncio.run(env.start(force_build=False))
    assert env.verification_state is VerificationState.FAILED

    # Retried on the SAME instance. A stale FAILED state must not short-circuit
    # this attempt's own, fresh verification.
    asyncio.run(env.start(force_build=False))

    assert env.verification_state is VerificationState.READY


# ---------------------------------------------------------------------------
# Egress proxy addressing
# ---------------------------------------------------------------------------


def test_plain_docker_proxy_env_uses_service_name(tmp_path):
    env = make_plain_env(tmp_path)
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()

    assert "pier-egress-proxy" in env.agent_process_env(None)["HTTP_PROXY"]


def test_gvisor_proxy_env_uses_literal_ipv4(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()
    assert "pier-egress-proxy" in env.agent_process_env(None)["HTTP_PROXY"]
    token_before = env._proxy_token()
    assert token_before

    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runsc" if cid == MAIN_ID else "runc")
    _stub_networks(
        monkeypatch,
        lambda cid: (
            {
                "p_default": {"IPAddress": "172.31.0.9"},
                "p_pier-egress-internal": {"IPAddress": "172.30.0.5"},
            }
            if cid == PROXY_ID
            else {"p_pier-egress-internal": {"IPAddress": "172.30.0.4"}}
        ),
    )

    asyncio.run(env._ensure_verified())

    proxy_url = env.agent_process_env(None)["HTTP_PROXY"]
    assert "@172.30.0.5:8080" in proxy_url
    assert "pier-egress-proxy" not in proxy_url
    # The token survives the re-addressing: it is recovered from the URL the
    # Docker path already built, so docker.py needs no gVisor-specific field.
    assert env._proxy_token() == token_before


def test_unresolvable_proxy_address_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()

    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)
    _stub_runtime(monkeypatch, lambda cid: "runsc" if cid == MAIN_ID else "runc")
    _stub_networks(
        monkeypatch,
        lambda cid: {} if cid == PROXY_ID else {"x": {"IPAddress": "10.0.0.2"}},
    )

    with pytest.raises(RuntimeError, match="egress proxy's address"):
        asyncio.run(env._ensure_verified())

    assert DOWN in commands


def test_shared_network_ipv4_prefers_a_shared_network():
    peer = {"a": {"IPAddress": "10.0.0.1"}, "b": {"IPAddress": "10.0.1.1"}}
    own = {"b": {"IPAddress": "10.0.1.2"}}

    assert gvisor_runtime.shared_network_ipv4(peer, own) == "10.0.1.1"
    assert gvisor_runtime.shared_network_ipv4(peer, {"z": {}}) is None


def test_parse_container_ids_ignores_compose_noise():
    output = f"WARN[0000] something\n{MAIN_ID}\n\n"
    assert gvisor_runtime.parse_container_ids(output) == [MAIN_ID]
    assert gvisor_runtime.parse_container_ids(None) == []


# ---------------------------------------------------------------------------
# Staging transfers
# ---------------------------------------------------------------------------


def test_platform_ops_are_gvisor_specific(tmp_path):
    assert isinstance(make_gvisor_env(tmp_path)._platform, GVisorUnixOps)


def test_all_four_transfers_never_use_compose_cp(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env._state = VerificationState.READY  # transfers are not the gate under test
    recorder = RecordingExec(stage_root=env.stage_out)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    source = tmp_path / "src"
    source.mkdir()
    (source / "result.txt").write_text("A")

    async def drive():
        await env.upload_file(source / "result.txt", "/work/result.txt")
        await env.upload_dir(source, "/work")
        await env.download_file("/work/result.txt", tmp_path / "got.txt")
        await env.download_dir("/work", tmp_path / "got")

    asyncio.run(drive())

    assert len(recorder.calls) == 4, "expected one compose exec per transfer"
    for call in recorder.calls:
        assert "cp" not in call, f"compose cp used in {call}"
        assert "exec" in call


def _upload_dir_fixture(tmp_path):
    source = tmp_path / "tests"
    (source / "nested").mkdir(parents=True)
    (source / "test.sh").write_text("#!/bin/sh\n")
    (source / "nested" / "helper.sh").write_text("#!/bin/sh\n")
    (source / ".hidden").write_text("hidden\n")
    (source / "with space.txt").write_text("spaced\n")
    (source / "link").symlink_to("test.sh")
    os.chmod(source, 0o777)
    return source


def test_upload_dir_copies_contents_not_the_directory(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = ExecutingSandbox(env, tmp_path)
    env.exec = sandbox

    source = _upload_dir_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()

    asyncio.run(env.upload_dir(source, str(target)))

    # Contents, never an extra containing directory (which would produce the
    # /tests/tests/test.sh regression that silently breaks the verifier).
    assert (target / "test.sh").is_file()
    assert not (target / "tests").exists()
    assert sandbox.users[-1] == "root"


def test_upload_dir_preserves_target_directory_metadata(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = ExecutingSandbox(env, tmp_path)

    source = _upload_dir_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "pre-existing.txt").write_text("keep\n")
    os.chmod(target, 0o2750)
    before = stat.S_IMODE(target.stat().st_mode)

    asyncio.run(env.upload_dir(source, str(target)))

    # `cp -a "$S"/. "$T"/` would stamp the staging directory's 0o777 onto the
    # target here; copying entries individually leaves the target alone.
    assert stat.S_IMODE(target.stat().st_mode) == before == 0o2750
    assert (target / "pre-existing.txt").read_text() == "keep\n"


def test_upload_dir_handles_nested_dotfiles_spaces_and_symlinks(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = ExecutingSandbox(env, tmp_path)

    source = _upload_dir_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()

    asyncio.run(env.upload_dir(source, str(target)))

    assert (target / "nested" / "helper.sh").read_text() == "#!/bin/sh\n"
    assert (target / ".hidden").read_text() == "hidden\n"
    assert (target / "with space.txt").read_text() == "spaced\n"
    assert (target / "link").is_symlink()
    assert os.readlink(target / "link") == "test.sh"


def test_upload_dir_chowns_only_copied_entries(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = ExecutingSandbox(env, tmp_path)
    env.exec = sandbox

    source = _upload_dir_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "pre-existing.txt").write_text("keep\n")

    asyncio.run(env.upload_dir(source, str(target)))

    chowned = " ".join(sandbox.chowned())
    assert "pre-existing.txt" not in chowned
    assert str(target) not in [line.split()[-1] for line in sandbox.chowned()]
    assert "test.sh" in chowned


def test_upload_file_to_existing_directory_uses_basename(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = FakeSandbox(env)
    env.exec = sandbox

    source = tmp_path / "auth.json"
    source.write_text("{}")

    asyncio.run(env.upload_file(source, "/root/.codex"))

    command = sandbox.commands[-1]
    assert 'if [ -d "$dest" ]; then dest="$dest/"auth.json; fi' in command
    assert "chown -h 0:0" in command


def test_upload_staging_directories_are_removed(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = FakeSandbox(env)

    source = tmp_path / "a.txt"
    source.write_text("A")
    asyncio.run(env.upload_file(source, "/work/a.txt"))

    assert list(env.stage_in.iterdir()) == []


def test_download_file_stages_and_chowns_only_the_copy(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = FakeSandbox(env, export={"result.txt": "hello"})
    env.exec = sandbox

    target = tmp_path / "out" / "result.txt"
    asyncio.run(env.download_file("/work/result.txt", target))

    assert target.read_text() == "hello"
    command = sandbox.commands[-1]
    assert "chown -Rh" in command
    # The chown applies to the staging directory, never the in-container source.
    assert '"$D"' in command.split("chown -Rh")[1]
    assert "/work/result.txt" not in command.split("chown -Rh")[1]
    assert list(env.stage_out.iterdir()) == []


def test_download_dir_copies_contents_and_overwrites(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = FakeSandbox(env, export={"a.txt": "A", "sub/b.txt": "B"})
    env.exec = sandbox

    target = tmp_path / "out"
    target.mkdir()
    (target / "a.txt").write_text("stale")

    asyncio.run(env.download_dir("/logs/artifacts", target))

    assert (target / "a.txt").read_text() == "A"
    assert (target / "sub" / "b.txt").read_text() == "B"
    assert "/logs/artifacts/." in sandbox.commands[-1]


def test_download_never_chowns_the_source(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = FakeSandbox(env, export={"a.txt": "A"})
    env.exec = sandbox

    asyncio.run(env.download_dir("/logs/artifacts", tmp_path / "out"))

    command = sandbox.commands[-1]
    chowns = re.findall(r"chown\s+-\S+\s+\S+\s+(\S+)", command)
    # Exactly one chown, and it names the staging directory variable, never the
    # in-container source: chowning sources breaks the next step of a
    # multi-step task when the agent runs as a non-root user.
    assert chowns == ['"$D"']
    assert "/logs/artifacts" not in command[command.index("chown") :]


def test_staging_operation_paths_are_unique(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    sandbox = FakeSandbox(env, export={"a.txt": "A"})
    env.exec = sandbox

    source = tmp_path / "a.txt"
    source.write_text("A")

    async def drive():
        await env.upload_file(source, "/work/a.txt")
        await env.upload_file(source, "/work/a.txt")
        await env.download_file("/work/a.txt", tmp_path / "one.txt")
        await env.download_file("/work/a.txt", tmp_path / "two.txt")

    asyncio.run(drive())

    op_dirs = sandbox.op_dirs()
    assert len(op_dirs) == 4
    assert len(set(op_dirs)) == 4


def test_transfer_failure_raises_with_output(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = FakeSandbox(env, return_code=1)

    source = tmp_path / "a.txt"
    source.write_text("A")

    with pytest.raises(RuntimeError, match="gVisor staging transfer failed"):
        asyncio.run(env.upload_file(source, "/work/a.txt"))


def test_stop_cleans_staging_directories(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    (env.stage_out / "leftover").mkdir(parents=True)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(env, "prepare_logs_for_host", lambda: asyncio.sleep(0))

    asyncio.run(env.stop(delete=False))

    assert not env.stage_in.exists()
    assert not env.stage_out.exists()


# ---------------------------------------------------------------------------
# Symlink-safe host-side placement
# ---------------------------------------------------------------------------


def test_download_file_replaces_destination_symlink_pointing_outside(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = FakeSandbox(env, export={"result.txt": "exported"})

    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "result.txt"
    target.symlink_to(outside)

    asyncio.run(env.download_file("/work/result.txt", target))

    # The planted link is replaced, and the file it pointed at is left alone.
    assert not target.is_symlink()
    assert target.read_text() == "exported"
    assert outside.read_text() == "untouched"


def test_download_dir_replaces_nested_destination_symlink_pointing_outside(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = FakeSandbox(env, export={"sub/b.txt": "B", "a.txt": "A"})

    outside = tmp_path / "escape"
    outside.mkdir()
    (outside / "b.txt").write_text("untouched")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "sub").symlink_to(outside, target_is_directory=True)

    asyncio.run(env.download_dir("/logs/artifacts", out_dir))

    assert not (out_dir / "sub").is_symlink()
    assert (out_dir / "sub" / "b.txt").read_text() == "B"
    assert (out_dir / "a.txt").read_text() == "A"
    # Nothing was written through the link.
    assert (outside / "b.txt").read_text() == "untouched"
    assert sorted(p.name for p in outside.iterdir()) == ["b.txt"]


def test_download_dir_preserves_a_dangling_source_symlink(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    class DanglingExport(FakeSandbox):
        async def __call__(self, command, **kwargs):
            result = await super().__call__(command, **kwargs)
            match = self._OUT_RE.search(command)
            if match:
                host_dir = self._env.stage_out / match.group(0).rsplit("/", 1)[1]
                (host_dir / "broken").symlink_to("/nonexistent/target")
            return result

    env.exec = DanglingExport(env, export={"real.txt": "R"})
    out_dir = tmp_path / "out"

    asyncio.run(env.download_dir("/logs/artifacts", out_dir))

    assert (out_dir / "broken").is_symlink()
    assert os.readlink(out_dir / "broken") == "/nonexistent/target"
    assert not (out_dir / "broken").exists()  # still dangling, as exported
    assert (out_dir / "real.txt").read_text() == "R"


def test_download_file_accepts_a_dangling_staged_symlink(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    class DanglingExport(FakeSandbox):
        async def __call__(self, command, **kwargs):
            result = await super().__call__(command, **kwargs)
            match = self._OUT_RE.search(command)
            if match:
                host_dir = self._env.stage_out / match.group(0).rsplit("/", 1)[1]
                host_dir.mkdir(parents=True, exist_ok=True)
                (host_dir / "link.txt").symlink_to("/nonexistent/target")
            return result

    env.exec = DanglingExport(env)
    target = tmp_path / "out" / "link.txt"

    # Path.exists() is False for a dangling link; lexists is what must be used.
    asyncio.run(env.download_file("/work/link.txt", target))

    assert target.is_symlink()
    assert os.readlink(target) == "/nonexistent/target"


def test_safe_copy_tree_rejects_symlinked_staging_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not copy")
    staged = tmp_path / "staged"
    staged.symlink_to(outside, target_is_directory=True)
    destination = tmp_path / "destination"

    with pytest.raises(RuntimeError, match="unsafe directory component"):
        safe_copy_tree(staged, destination)

    assert not (destination / "secret.txt").exists()


def test_safe_place_file_rejects_symlinked_destination_parent(tmp_path):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    staged = staged_dir / "result.txt"
    staged.write_text("exported")

    outside = tmp_path / "outside"
    outside.mkdir()
    destination_parent = tmp_path / "destination-parent"
    destination_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="unsafe directory component"):
        safe_place_file(staged, destination_parent / "result.txt")

    assert not (outside / "result.txt").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires Unix")
def test_safe_place_file_rejects_fifo_without_blocking(tmp_path):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    fifo = staged_dir / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(RuntimeError, match="only regular files and symlinks"):
        safe_place_file(fifo, tmp_path / "out" / "pipe")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires Unix")
def test_safe_copy_tree_rejects_a_staged_fifo(tmp_path):
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    os.mkfifo(staged_dir / "pipe")

    with pytest.raises(RuntimeError, match="only regular files, directories"):
        safe_copy_tree(staged_dir, tmp_path / "out")


def test_safe_copy_tree_rejects_a_staged_socket(tmp_path):
    import socket

    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(staged_dir / "sock"))
        with pytest.raises(RuntimeError, match="only regular files, directories"):
            safe_copy_tree(staged_dir, tmp_path / "out")
    finally:
        sock.close()


def test_download_dir_handles_ordinary_files_and_directories(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env.exec = FakeSandbox(
        env, export={"a.txt": "A", "deep/nested/c.txt": "C", ".hidden": "H"}
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stale.txt").write_text("stale")
    (out_dir / "a.txt").write_text("old")

    asyncio.run(env.download_dir("/logs/artifacts", out_dir))

    assert (out_dir / "a.txt").read_text() == "A"
    assert (out_dir / "deep" / "nested" / "c.txt").read_text() == "C"
    assert (out_dir / ".hidden").read_text() == "H"
    assert (out_dir / "stale.txt").read_text() == "stale"


# ---------------------------------------------------------------------------
# Mount validation
# ---------------------------------------------------------------------------


def _mount(source: str, target: str = "/mnt/x"):
    return {"type": "bind", "source": source, "target": target}


@pytest.mark.parametrize(
    "source",
    [
        "/var/run/docker.sock",
        "/run/podman/podman.sock",
        "/",
        os.path.expanduser("~"),
        "/etc",
    ],
)
def test_gvisor_rejects_mounts_outside_the_trial_directory(tmp_path, source):
    with pytest.raises(ValueError, match="refuses the bind mount"):
        make_gvisor_env(tmp_path, mounts_json=[_mount(source)])


def test_gvisor_allows_default_log_mounts(tmp_path):
    env = make_gvisor_env(tmp_path)

    sources = [m["source"] for m in env._mounts_json]
    assert sources
    for source in sources:
        assert Path(source).is_relative_to(env.trial_paths.trial_dir.resolve())


def test_gvisor_allows_the_separate_verifier_mount(tmp_path):
    trial_dir = tmp_path / "trial"
    env = make_gvisor_env(
        tmp_path,
        mounts_json=[_mount(str(trial_dir / "verifier"), "/logs/verifier")],
    )

    assert env._mounts_json[0]["source"] == str(trial_dir / "verifier")


def test_gvisor_allows_non_bind_mounts(tmp_path):
    env = make_gvisor_env(
        tmp_path,
        mounts_json=[{"type": "volume", "source": "cache", "target": "/cache"}],
    )

    assert env._mounts_json[0]["type"] == "volume"


def test_plain_docker_still_allows_host_mounts(tmp_path):
    env = make_plain_env(tmp_path, mounts_json=[_mount("/var/run/docker.sock")])

    assert env._mounts_json[0]["source"] == "/var/run/docker.sock"


# ---------------------------------------------------------------------------
# Runtime evidence -- post-hoc audit of which runtime each trial ran under
# ---------------------------------------------------------------------------


def test_runtime_evidence_records_both_services(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._record_runtime_evidence("main", "runsc")
    env._record_runtime_evidence("pier-egress-proxy", "runc")

    evidence = json.loads(
        (env.trial_paths.trial_dir / "runtime-verification.json").read_text()
    )
    assert evidence["engine"] == "docker"
    assert evidence["expected_runtime"] == "runsc"
    assert evidence["services"]["main"]["reported"] == "runsc"
    assert evidence["services"]["pier-egress-proxy"]["reported"] == "runc"
    assert evidence["services"]["main"]["verified_at"]


def test_runtime_evidence_write_failure_is_not_fatal(tmp_path, monkeypatch):
    # Bookkeeping, not a gate: a full disk must not kill a verified trial.
    env = make_gvisor_env(tmp_path)
    monkeypatch.setattr(
        Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    env._record_runtime_evidence("main", "runsc")  # must not raise


# ---------------------------------------------------------------------------
# Image-source policy -- source-first over third-party prebuilts
# ---------------------------------------------------------------------------


def test_prebuilt_is_ignored_when_the_task_ships_a_dockerfile(tmp_path):
    # The prebuilt is typically a mutable-tag build cache of that same
    # Dockerfile on a third-party registry account; the Dockerfile is the
    # auditable input.
    env = make_plain_env(
        tmp_path,
        task_config=TaskEnvironmentConfig(
            allow_internet=False, docker_image="alexgshaw/build-pmars:20251031"
        ),
    )
    assert env._effective_docker_image is None
    assert env._env_vars.prebuilt_image_name is None


def test_prebuilt_opt_in_restores_upstream_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("PIER_IMAGE_SOURCE", "prebuilt")
    env = make_plain_env(
        tmp_path,
        task_config=TaskEnvironmentConfig(
            allow_internet=False, docker_image="alexgshaw/build-pmars:20251031"
        ),
    )
    assert env._effective_docker_image == "alexgshaw/build-pmars:20251031"


def test_image_only_tasks_keep_their_prebuilt(tmp_path):
    env = make_plain_env(
        tmp_path,
        task_config=TaskEnvironmentConfig(
            allow_internet=False, docker_image="ghcr.io/org/task-img:v1"
        ),
    )
    (env.environment_dir / "Dockerfile").unlink()  # image-only task
    assert env._effective_docker_image == "ghcr.io/org/task-img:v1"
