"""Network-policy separation, DNS selection, and fail-closed probing.

Three policies must stay distinct and must never borrow each other's mechanism:

* no-network      -- allow_internet=false, no agent allowlist
* allowlist       -- allow_internet=false, non-empty agent allowlist
* unrestricted    -- allow_internet=true

The resolv.conf repair belongs to the unrestricted path alone. In allowlist mode
the trusted proxy resolves hostnames, so giving the sandbox a working resolver
would create direct DNS egress the policy does not grant.
"""

import asyncio
import json
import sys

import pytest

from pier.environments.base import ExecResult
from pier.environments.gvisor import network
from pier.environments.gvisor.environment import GVisorEnvironment, VerificationState
from pier.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from pier.models.trial.paths import TrialPaths

GVISOR_MODULE = "pier.environments.gvisor.environment"
MAIN_ID = "a" * 64
PROXY_ID = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the gVisor environment requires a Linux host"
)


def make_env(tmp_path, *, allow_internet: bool, **kwargs) -> GVisorEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    return GVisorEnvironment(
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=allow_internet),
        **kwargs,
    )


class Sandbox:
    """Drives verification with a scripted sandbox filesystem and network."""

    def __init__(
        self,
        env,
        *,
        resolv: str = "nameserver 127.0.0.11\nsearch corp.internal\noptions ndots:1\n",
        probe_ok: bool = True,
        write_ok: bool = True,
        runtime: str = "runsc",
    ):
        self.env = env
        self.resolv = resolv
        self.probe_ok = probe_ok
        self.write_ok = write_ok
        self.runtime = runtime
        self.compose: list[list[str]] = []
        self.inner: list[str] = []

    async def compose_command(self, command, check=True, timeout_sec=None):
        self.compose.append(list(command))
        if command[:2] == ["ps", "--quiet"]:
            return ExecResult(
                stdout=MAIN_ID if command[2] == "main" else PROXY_ID,
                return_code=0,
            )
        if command[0] == "exec":
            return await self._exec(command[-1])
        return ExecResult(stdout="", return_code=0)

    async def _exec(self, inner: str) -> ExecResult:
        self.inner.append(inner)
        if inner.startswith("cat "):
            return ExecResult(stdout=self.resolv, return_code=0)
        if inner.startswith("printf %s "):
            if not self.write_ok:
                return ExecResult(stdout="read-only file system", return_code=1)
            # Simulate the truncate-in-place write landing.
            self.resolv = self._written_content(inner)
            return ExecResult(stdout="", return_code=0)
        if not self.probe_ok:
            return ExecResult(stdout="", return_code=1)
        return ExecResult(stdout=f"{network.PROBE_OK_MARKER}\n", return_code=0)

    @staticmethod
    def _written_content(inner: str) -> str:
        import shlex

        parts = shlex.split(inner)
        # printf %s <content> > /etc/resolv.conf
        return parts[2]

    async def runtime_of(self, container_id, cli="docker"):
        return self.runtime

    def install(self, monkeypatch):
        monkeypatch.setattr(
            self.env, "_run_docker_compose_command", self.compose_command
        )
        monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", self.runtime_of)

        # Teardown's fail-closed queries raise when the engine CLI is absent;
        # report a clean project so the tests don't need docker on the host.
        async def no_resources(project, cli="docker"):
            return []

        monkeypatch.setattr(f"{GVISOR_MODULE}.project_container_ids", no_resources)
        monkeypatch.setattr(f"{GVISOR_MODULE}.project_network_ids", no_resources)
        return self


# ---------------------------------------------------------------------------
# Resolver address validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.11",  # Docker's embedded resolver
        "127.0.0.53",  # systemd-resolved stub
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "0.0.0.0",
        "::",
        "not-an-address",
        "",
    ],
)
def test_unusable_resolvers_are_rejected(address):
    assert network.is_usable_resolver(address) is False


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "10.0.0.53", "2606:4700::1"])
def test_routable_resolvers_are_accepted(address):
    assert network.is_usable_resolver(address) is True


def test_explicit_loopback_dns_is_rejected_not_silently_dropped():
    with pytest.raises(ValueError, match="cannot be reached from inside"):
        network.parse_explicit_dns("127.0.0.11")


def test_explicit_dns_accepts_a_comma_list_and_a_sequence():
    assert network.parse_explicit_dns("1.1.1.1, 9.9.9.9") == ["1.1.1.1", "9.9.9.9"]
    assert network.parse_explicit_dns(["1.1.1.1"]) == ["1.1.1.1"]
    assert network.parse_explicit_dns(None) == []


# ---------------------------------------------------------------------------
# Resolver selection
# ---------------------------------------------------------------------------


def test_explicit_dns_wins_over_host_configuration(tmp_path):
    host = tmp_path / "resolv.conf"
    host.write_text("nameserver 10.1.2.3\n")

    assert network.select_resolvers("1.1.1.1", sources=(host,)) == ["1.1.1.1"]


def test_host_resolvers_are_inherited_when_usable(tmp_path):
    host = tmp_path / "resolv.conf"
    host.write_text("nameserver 10.1.2.3\nnameserver 10.1.2.4\nsearch corp\n")

    assert network.select_resolvers(None, sources=(host,)) == ["10.1.2.3", "10.1.2.4"]


def test_loopback_only_host_falls_through_to_the_next_source(tmp_path):
    stub = tmp_path / "resolv.conf"
    stub.write_text("nameserver 127.0.0.53\n")
    upstream = tmp_path / "systemd-resolv.conf"
    upstream.write_text("nameserver 10.9.9.9\n")

    assert network.select_resolvers(None, sources=(stub, upstream)) == ["10.9.9.9"]


def test_no_usable_resolver_fails_closed_with_no_public_default(tmp_path):
    stub = tmp_path / "resolv.conf"
    stub.write_text("nameserver 127.0.0.53\n")

    with pytest.raises(RuntimeError) as excinfo:
        network.select_resolvers(None, sources=(stub,))

    message = str(excinfo.value)
    assert "--ek dns=" in message
    # No public resolver may be silently substituted.
    assert "8.8.8.8" not in message.replace("--ek dns=1.1.1.1", "")


def test_missing_host_files_fail_closed(tmp_path):
    with pytest.raises(RuntimeError, match="could not find a usable DNS resolver"):
        network.select_resolvers(None, sources=(tmp_path / "nope",))


# ---------------------------------------------------------------------------
# resolv.conf rendering and rewrite command
# ---------------------------------------------------------------------------


def test_needs_normalization_only_when_every_nameserver_is_unusable():
    assert network.needs_normalization("nameserver 127.0.0.11\n") is True
    assert network.needs_normalization("search corp\n") is True
    assert network.needs_normalization("") is True
    assert network.needs_normalization("nameserver 1.1.1.1\n") is False
    assert (
        network.needs_normalization("nameserver 127.0.0.11\nnameserver 10.0.0.1\n")
        is False
    )


def test_render_preserves_search_and_options():
    _, preserved = network.parse_resolv_conf(
        "nameserver 127.0.0.11\nsearch corp.internal\noptions ndots:1\n"
    )
    rendered = network.render_resolv_conf(["1.1.1.1"], preserved)

    assert rendered.splitlines() == [
        "nameserver 1.1.1.1",
        "search corp.internal",
        "options ndots:1",
    ]


def test_write_command_truncates_in_place_and_never_renames():
    command = network.resolv_conf_write_command("nameserver 1.1.1.1\n")

    assert command.startswith("printf %s ")
    assert "> /etc/resolv.conf" in command
    # A rename across the bind mount fails; these must never appear.
    assert "sed -i" not in command
    assert " mv " not in command


def test_probe_command_resolves_and_connects_with_a_timeout():
    command = network.probe_command("example.com", 443, 7)

    assert "timeout 7 bash -c" in command
    assert "/dev/tcp/example.com/443" in command
    assert network.PROBE_OK_MARKER in command
    # No tool beyond bash is assumed.
    for tool in ("curl", "wget", "dig", "nslookup", "getent", "python3"):
        assert tool not in command


# ---------------------------------------------------------------------------
# Policy separation
# ---------------------------------------------------------------------------


def test_no_network_mode_never_rewrites_resolv_conf(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=False)
    sandbox = Sandbox(env).install(monkeypatch)

    asyncio.run(env._ensure_verified())

    assert sandbox.inner == [], "no command may touch the sandbox's resolver"
    assert env.verification_state is VerificationState.READY


def test_allowlist_mode_never_rewrites_resolv_conf(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=False)
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()

    sandbox = Sandbox(env)

    async def runtime_of(container_id, cli="docker"):
        return "runsc" if container_id == MAIN_ID else "runc"

    async def networks_of(container_id, cli="docker"):
        return {"shared": {"IPAddress": "172.30.0.5"}}

    monkeypatch.setattr(env, "_run_docker_compose_command", sandbox.compose_command)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_runtime", runtime_of)
    monkeypatch.setattr(f"{GVISOR_MODULE}.container_networks", networks_of)

    asyncio.run(env._ensure_verified())

    assert sandbox.inner == [], "the proxy resolves hostnames, not the sandbox"
    assert env.verification_state is VerificationState.READY


def test_no_network_mode_selects_no_resolvers(tmp_path):
    env = make_env(tmp_path, allow_internet=False)
    env._prepare_gvisor()

    assert env.resolvers == []
    main = json.loads(env._compose_override_path.read_text())["services"]["main"]
    assert "dns" not in main


def test_unrestricted_mode_never_creates_a_proxy(tmp_path):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env.network_allowlist.domains.append("api.anthropic.com")
    env._prepare_egress_proxy_compose()

    # DockerEnvironment refuses to build a proxy when internet is unrestricted,
    # and gVisor must not add one behind its back.
    assert env._egress_proxy_compose_path is None
    assert env.agent_process_env(None) is None


def test_gvisor_never_emits_host_or_bridge_networking(tmp_path):
    for allow_internet, kwargs in ((False, {}), (True, {"dns": "1.1.1.1"})):
        env = make_env(
            tmp_path / f"n{allow_internet}", allow_internet=allow_internet, **kwargs
        )
        env._prepare_gvisor()
        rendered = env._compose_override_path.read_text()

        assert "network_mode" not in rendered
        assert '"host"' not in rendered
        assert '"bridge"' not in rendered


# ---------------------------------------------------------------------------
# Unrestricted-internet mode
# ---------------------------------------------------------------------------


def test_unrestricted_mode_selects_resolvers_before_start(tmp_path):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1,9.9.9.9")
    env._prepare_gvisor()

    assert env.resolvers == ["1.1.1.1", "9.9.9.9"]
    main = json.loads(env._compose_override_path.read_text())["services"]["main"]
    assert main["dns"] == ["1.1.1.1", "9.9.9.9"]


def test_unrestricted_mode_with_no_usable_resolver_fails_before_build(
    tmp_path, monkeypatch
):
    stub = tmp_path / "resolv.conf"
    stub.write_text("nameserver 127.0.0.53\n")
    monkeypatch.setattr(network, "HOST_RESOLV_SOURCES", (stub,))

    env = make_env(tmp_path, allow_internet=True)

    with pytest.raises(RuntimeError, match="could not find a usable DNS resolver"):
        env._prepare_gvisor()


def test_unrestricted_mode_repairs_an_unusable_resolver(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env).install(monkeypatch)

    asyncio.run(env._ensure_verified())

    assert any(inner.startswith("printf %s ") for inner in sandbox.inner)
    assert "nameserver 1.1.1.1" in sandbox.resolv
    # The task image's own search path survives the repair.
    assert "search corp.internal" in sandbox.resolv
    assert env.verification_state is VerificationState.READY


def test_unrestricted_mode_leaves_a_working_resolver_alone(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env, resolv="nameserver 10.0.0.53\n").install(monkeypatch)

    asyncio.run(env._ensure_verified())

    assert not any(inner.startswith("printf %s ") for inner in sandbox.inner)
    assert sandbox.resolv == "nameserver 10.0.0.53\n"


def test_unrestricted_mode_probes_dns_and_connectivity(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env).install(monkeypatch)

    asyncio.run(env._ensure_verified())

    assert any("/dev/tcp/example.com/443" in inner for inner in sandbox.inner)


def test_probe_target_is_configurable(tmp_path, monkeypatch):
    env = make_env(
        tmp_path,
        allow_internet=True,
        dns="1.1.1.1",
        probe_host="pypi.org",
        probe_port=80,
    )
    env._prepare_gvisor()
    sandbox = Sandbox(env).install(monkeypatch)

    asyncio.run(env._ensure_verified())

    assert any("/dev/tcp/pypi.org/80" in inner for inner in sandbox.inner)


def test_probe_failure_tears_down_and_never_downgrades(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env, probe_ok=False).install(monkeypatch)

    with pytest.raises(RuntimeError, match="could not reach example.com:443"):
        asyncio.run(env._ensure_verified())

    assert ["down", "--remove-orphans"] in sandbox.compose
    assert env.verification_state is VerificationState.FAILED
    # No fallback to a weaker policy.
    assert env._egress_proxy_compose_path is None
    assert not env.stage_in.exists()


def test_failed_rewrite_fails_closed(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env, write_ok=False).install(monkeypatch)

    with pytest.raises(RuntimeError, match="could not rewrite"):
        asyncio.run(env._ensure_verified())

    assert ["down", "--remove-orphans"] in sandbox.compose


def test_still_unusable_after_rewrite_fails_closed(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()

    sandbox = Sandbox(env)

    async def stubborn_exec(inner: str) -> ExecResult:
        sandbox.inner.append(inner)
        if inner.startswith("cat "):
            return ExecResult(stdout="nameserver 127.0.0.11\n", return_code=0)
        return ExecResult(stdout="", return_code=0)

    sandbox._exec = stubborn_exec
    sandbox.install(monkeypatch)

    with pytest.raises(RuntimeError, match="still reports no usable nameserver"):
        asyncio.run(env._ensure_verified())

    assert ["down", "--remove-orphans"] in sandbox.compose


def test_unreadable_resolv_conf_fails_closed(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()

    sandbox = Sandbox(env)

    async def unreadable(inner: str) -> ExecResult:
        sandbox.inner.append(inner)
        return ExecResult(stdout="no such file", return_code=1)

    sandbox._exec = unreadable
    sandbox.install(monkeypatch)

    with pytest.raises(RuntimeError, match="could not read /etc/resolv.conf"):
        asyncio.run(env._ensure_verified())


def test_dns_repair_never_runs_when_the_runtime_is_wrong(tmp_path, monkeypatch):
    env = make_env(tmp_path, allow_internet=True, dns="1.1.1.1")
    env._prepare_gvisor()
    sandbox = Sandbox(env, runtime="runc").install(monkeypatch)

    with pytest.raises(RuntimeError, match="Refusing to run untrusted code"):
        asyncio.run(env._ensure_verified())

    assert sandbox.inner == [], "nothing may run in an unverified sandbox"
