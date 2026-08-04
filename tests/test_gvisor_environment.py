"""Unit tests for the optional gVisor mode of the Docker environment.

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
from pier.environments.docker import gvisor
from pier.environments.docker.docker import DockerEnvironment
from pier.environments.docker.docker_gvisor_unix import (
    GvisorUnixOps,
    safe_copy_tree,
    safe_place_file,
)
from pier.environments.docker.docker_unix import UnixOps
from pier.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from pier.models.task.config import TaskOS
from pier.models.trial.paths import TrialPaths

DOCKER_MODULE = "pier.environments.docker.docker"

MAIN_ID = "a" * 64
PROXY_ID = "b" * 64


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
            host_dir = self._env.gvisor_stage_in / upload.group(0).rsplit("/", 1)[1]
            self.staged_uploads.append(
                sorted(
                    p.relative_to(host_dir).as_posix()
                    for p in host_dir.rglob("*")
                    if host_dir.exists()
                )
            )

        download = self._OUT_RE.search(command)
        if download and self.return_code == 0:
            host_dir = self._env.gvisor_stage_out / download.group(0).rsplit("/", 1)[1]
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
            str(gvisor.GVISOR_STAGE_IN), str(self._env.gvisor_stage_in)
        ).replace(str(gvisor.GVISOR_STAGE_OUT), str(self._env.gvisor_stage_out))
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


def _make_env(tmp_path, *, gvisor=False, task_config=None, **kwargs):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    if task_config is None:
        task_config = TaskEnvironmentConfig(allow_internet=False)

    return DockerEnvironment(
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=task_config,
        gvisor=gvisor,
        **kwargs,
    )


def make_gvisor_env(tmp_path, **kwargs) -> DockerEnvironment:
    return _make_env(tmp_path, gvisor=True, **kwargs)


def make_plain_env(tmp_path, **kwargs) -> DockerEnvironment:
    return _make_env(tmp_path, gvisor=False, **kwargs)


def rendered_main(env) -> dict:
    return json.loads(env._gvisor_compose_path.read_text())["services"]["main"]


# ---------------------------------------------------------------------------
# Compose override
# ---------------------------------------------------------------------------


def test_gvisor_false_leaves_compose_paths_unchanged(tmp_path):
    plain = make_plain_env(tmp_path / "plain")
    sandboxed = make_gvisor_env(tmp_path / "sandboxed")
    sandboxed._prepare_gvisor()

    assert plain._gvisor_compose_path is None
    plain_names = [p.name for p in plain._docker_compose_paths]
    sandboxed_names = [p.name for p in sandboxed._docker_compose_paths]

    # Identical but for the single appended override.
    assert sandboxed_names == plain_names + [gvisor.GVISOR_COMPOSE_NAME]
    assert not any("gvisor" in name for name in plain_names)


def test_gvisor_override_is_appended_last(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    env._mounts_compose_path = env._write_mounts_compose_file()

    paths = env._docker_compose_paths
    assert paths[-1] == env._gvisor_compose_path


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


def test_gvisor_override_renders_both_staging_mounts(tmp_path):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()

    volumes = rendered_main(env)["volumes"]
    assert volumes == [
        {
            "type": "bind",
            "source": str(env.gvisor_stage_in.resolve()),
            "target": "/.pier-stage/in",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(env.gvisor_stage_out.resolve()),
            "target": "/.pier-stage/out",
        },
    ]
    assert env.gvisor_stage_in.is_dir()
    assert env.gvisor_stage_out.is_dir()


def test_custom_gvisor_runtime_is_rendered(tmp_path):
    env = make_gvisor_env(tmp_path, gvisor_runtime="runsc-custom")
    env._prepare_gvisor()

    assert rendered_main(env)["runtime"] == "runsc-custom"


def test_different_trial_dirs_never_share_staging_paths(tmp_path):
    first = make_gvisor_env(tmp_path / "one")
    second = make_gvisor_env(tmp_path / "two")

    assert first.gvisor_stage_in != second.gvisor_stage_in
    assert first.gvisor_stage_out != second.gvisor_stage_out
    assert first.gvisor_stage_in.is_relative_to(first.trial_paths.trial_dir)
    assert second.gvisor_stage_out.is_relative_to(second.trial_paths.trial_dir)


# ---------------------------------------------------------------------------
# Fail-closed configuration checks
# ---------------------------------------------------------------------------


def test_task_compose_file_is_rejected(tmp_path):
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")
    (environment_dir / "docker-compose.yaml").write_text("services:\n  main: {}\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    with pytest.raises(ValueError, match="not docker-compose tasks"):
        DockerEnvironment(
            environment_dir=environment_dir,
            environment_name="hello-world",
            session_id="hello-world__abc123",
            trial_paths=trial_paths,
            task_env_config=TaskEnvironmentConfig(allow_internet=False),
            gvisor=True,
        )


def test_windows_task_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="does not support Windows tasks"):
        make_gvisor_env(
            tmp_path,
            task_config=TaskEnvironmentConfig(allow_internet=False, os=TaskOS.WINDOWS),
        )


def test_non_linux_host_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(RuntimeError, match="requires a Linux host"):
        make_gvisor_env(tmp_path)


def test_allow_internet_true_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="allow_internet"):
        make_gvisor_env(
            tmp_path, task_config=TaskEnvironmentConfig(allow_internet=True)
        )


def test_plain_docker_still_accepts_allow_internet_and_compose(tmp_path):
    env = make_plain_env(
        tmp_path, task_config=TaskEnvironmentConfig(allow_internet=True)
    )
    assert env.gvisor is False
    assert isinstance(env._platform, UnixOps)
    assert not isinstance(env._platform, GvisorUnixOps)


def test_unregistered_runtime_is_rejected(monkeypatch):
    monkeypatch.setattr(gvisor, "docker_runtimes", lambda *a, **k: {"runc"})

    with pytest.raises(RuntimeError, match="requires the 'runsc' runtime"):
        gvisor.assert_runtime_registered("runsc")


def test_unqueryable_daemon_is_rejected(monkeypatch):
    monkeypatch.setattr(gvisor, "docker_runtimes", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="could not query the Docker daemon"):
        gvisor.assert_runtime_registered("runsc")


def test_registered_runtime_passes(monkeypatch):
    monkeypatch.setattr(gvisor, "docker_runtimes", lambda *a, **k: {"runc", "runsc"})

    gvisor.assert_runtime_registered("runsc")


def test_start_checks_runtime_before_building(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    recorder = RecordingExec()
    monkeypatch.setattr(gvisor, "docker_runtimes", lambda *a, **k: {"runc"})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

    with pytest.raises(RuntimeError, match="requires the 'runsc' runtime"):
        asyncio.run(env.start(force_build=False))

    assert recorder.calls == []


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


def test_main_runtime_mismatch_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runc"

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)

    with pytest.raises(RuntimeError, match="Refusing to run untrusted code"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


def test_proxy_runtime_mismatch_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._egress_proxy_compose_path = tmp_path / "proxy.json"
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runsc"  # both main and proxy -- the proxy must not be sandboxed

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)

    with pytest.raises(RuntimeError, match="egress proxy is running under"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


def test_verification_failure_runs_compose_down(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []

    async def fake_compose(command, check=True, timeout_sec=None):
        commands.append(list(command))
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)

    # No container ID resolvable -> verification cannot succeed.
    with pytest.raises(RuntimeError, match="could not resolve the 'main' container"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


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

    async def fake_runtime(container_id):
        return "runsc"

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)

    with pytest.raises(RuntimeError, match="could not resolve the 'pier-egress-proxy'"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


def test_uninspectable_proxy_runtime_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._egress_proxy_compose_path = tmp_path / "proxy.json"
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runsc" if container_id == MAIN_ID else None

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)

    with pytest.raises(RuntimeError, match="could not determine the runtime"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


def test_successful_verification_does_not_tear_down(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runsc"

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)

    asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] not in commands


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

    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runsc" if container_id == MAIN_ID else "runc"

    async def fake_networks(container_id):
        if container_id == PROXY_ID:
            return {
                "p_default": {"IPAddress": "172.31.0.9"},
                "p_pier-egress-internal": {"IPAddress": "172.30.0.5"},
            }
        return {"p_pier-egress-internal": {"IPAddress": "172.30.0.4"}}

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)
    monkeypatch.setattr(f"{DOCKER_MODULE}.container_networks", fake_networks)

    asyncio.run(env._verify_gvisor_after_start())

    proxy_url = env.agent_process_env(None)["HTTP_PROXY"]
    assert "@172.30.0.5:8080" in proxy_url
    assert "pier-egress-proxy" not in proxy_url


def test_unresolvable_proxy_address_fails_closed(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()

    commands: list[list[str]] = []
    _stub_compose(env, monkeypatch, commands)

    async def fake_runtime(container_id):
        return "runsc" if container_id == MAIN_ID else "runc"

    async def fake_networks(container_id):
        return {} if container_id == PROXY_ID else {"x": {"IPAddress": "10.0.0.2"}}

    monkeypatch.setattr(f"{DOCKER_MODULE}.container_runtime", fake_runtime)
    monkeypatch.setattr(f"{DOCKER_MODULE}.container_networks", fake_networks)

    with pytest.raises(RuntimeError, match="egress proxy's address"):
        asyncio.run(env._verify_gvisor_after_start())

    assert ["down"] in commands


def test_shared_network_ipv4_prefers_a_shared_network():
    peer = {"a": {"IPAddress": "10.0.0.1"}, "b": {"IPAddress": "10.0.1.1"}}
    own = {"b": {"IPAddress": "10.0.1.2"}}

    assert gvisor.shared_network_ipv4(peer, own) == "10.0.1.1"
    assert gvisor.shared_network_ipv4(peer, {"z": {}}) is None


def test_parse_container_ids_ignores_compose_noise():
    output = f"WARN[0000] something\n{MAIN_ID}\n\n"
    assert gvisor.parse_container_ids(output) == [MAIN_ID]
    assert gvisor.parse_container_ids(None) == []


# ---------------------------------------------------------------------------
# Staging transfers
# ---------------------------------------------------------------------------


def test_platform_ops_are_gvisor_specific(tmp_path):
    assert isinstance(make_gvisor_env(tmp_path)._platform, GvisorUnixOps)


def test_all_four_transfers_never_use_compose_cp(tmp_path, monkeypatch):
    env = make_gvisor_env(tmp_path)
    env._prepare_gvisor()
    recorder = RecordingExec(stage_root=env.gvisor_stage_out)
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

    assert list(env.gvisor_stage_in.iterdir()) == []


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
    assert list(env.gvisor_stage_out.iterdir()) == []


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
    (env.gvisor_stage_out / "leftover").mkdir(parents=True)

    async def fake_compose(command, check=True, timeout_sec=None):
        return ExecResult(stdout="", return_code=0)

    monkeypatch.setattr(env, "_run_docker_compose_command", fake_compose)
    monkeypatch.setattr(env, "prepare_logs_for_host", lambda: asyncio.sleep(0))

    asyncio.run(env.stop(delete=False))

    assert not env.gvisor_stage_in.exists()
    assert not env.gvisor_stage_out.exists()


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
                host_dir = self._env.gvisor_stage_out / match.group(0).rsplit("/", 1)[1]
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
                host_dir = self._env.gvisor_stage_out / match.group(0).rsplit("/", 1)[1]
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
