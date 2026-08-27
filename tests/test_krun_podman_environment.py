"""Unit tests for the first-class krun-on-Podman environment
(``--env krun-podman``).

Only the deltas against the runsc flavor are tested here; the shared Podman
driving, discovery, and teardown machinery is covered by
``test_gvisor_podman_environment.py``. Every Podman interaction is mocked:
these tests must pass on a host with no krun installed and no Podman
available.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from titanium.environments.gvisor.podman import GVisorPodmanEnvironment
from titanium.environments.gvisor.podman_runtime import assert_runtime_digest
from titanium.environments.krun.podman import (
    KRUN_DIGEST_PIN,
    KrunPodmanEnvironment,
)
from titanium.environments.podman.podman import PodmanEnvironment
from titanium.models.environment_type import EnvironmentType
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.paths import TrialPaths

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the krun environment requires a Linux host"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_env(tmp_path, **kwargs) -> KrunPodmanEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\n")

    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    return KrunPodmanEnvironment(
        environment_dir=environment_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123",
        trial_paths=trial_paths,
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
        **kwargs,
    )


def _pin(tmp_path, content: bytes) -> tuple:
    binary = tmp_path / "krun"
    binary.write_bytes(content)
    pin = tmp_path / "krun.sha3-512"
    pin.write_text(f"{hashlib.sha3_512(content).hexdigest()}  {binary}\n")
    return pin, binary


# ---------------------------------------------------------------------------
# Identity, composition, and construction
# ---------------------------------------------------------------------------


def test_reports_its_own_type(tmp_path):
    env = _make_env(tmp_path)
    assert env.type() is EnvironmentType.KRUN_PODMAN
    assert KrunPodmanEnvironment.type() is EnvironmentType.KRUN_PODMAN


def test_mro_extends_the_gvisor_podman_lineage():
    names = [cls.__name__ for cls in KrunPodmanEnvironment.__mro__]
    assert names[0] == "KrunPodmanEnvironment"
    assert names.index("GVisorPodmanEnvironment") < names.index("GVisorEnvironment")
    assert names.index("GVisorEnvironment") < names.index("PodmanEnvironment")
    assert names.index("PodmanEnvironment") < names.index("DockerEnvironment")


def test_defaults_to_podman_and_the_krun_runtime(tmp_path):
    env = _make_env(tmp_path)
    assert env.engine == "podman"
    assert env.runtime == "krun"


def test_engine_docker_fails_without_a_gvisor_redirect(tmp_path):
    # The inherited redirect points at --env gvisor, a different sandbox
    # technology. The krun flavor must state its own constraint instead.
    with pytest.raises(ValueError, match=r"krun-podman.*only drives") as excinfo:
        _make_env(tmp_path, engine="docker")
    assert "--env gvisor" not in str(excinfo.value)


def test_any_non_podman_engine_fails_the_same_way(tmp_path):
    with pytest.raises(ValueError, match=r"only drives"):
        _make_env(tmp_path, engine="containerd")


# ---------------------------------------------------------------------------
# Runtime verification -- the krun name, both spellings
# ---------------------------------------------------------------------------


def test_runtime_match_accepts_name_and_path_spellings(tmp_path):
    env = _make_env(tmp_path)
    assert env._runtime_matches("krun")
    assert env._runtime_matches("/usr/bin/krun")
    assert not env._runtime_matches("runsc")
    assert not env._runtime_matches("crun")
    assert not env._runtime_matches("oci")
    assert not env._runtime_matches(None)


# ---------------------------------------------------------------------------
# Compose override -- runtime krun, SELinux process label kept
# ---------------------------------------------------------------------------


def test_override_puts_main_under_krun(tmp_path):
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    data = json.loads(Path(env._compose_override_path).read_text())
    assert data["services"]["main"]["runtime"] == "krun"


def test_override_keeps_the_selinux_process_label(tmp_path):
    # runsc rejects a labeled spec, so the runsc flavor sends label=disable.
    # crun supports SELinux; the krun flavor must not weaken confinement.
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    data = json.loads(Path(env._compose_override_path).read_text())
    assert data["services"]["main"]["security_opt"] == ["no-new-privileges:true"]


def test_runsc_flavor_still_disables_the_process_label():
    # The seam's default must stay the runsc value: the krun subclass
    # narrows it, never the other way around.
    assert GVisorPodmanEnvironment._DISABLE_PROCESS_LABEL is True
    assert KrunPodmanEnvironment._DISABLE_PROCESS_LABEL is False


def test_override_still_stamps_the_selinux_relabel(tmp_path):
    # The bind-mount relabel is a Podman property, not a runtime property;
    # it must survive the krun subclassing unchanged.
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    data = json.loads(Path(env._compose_override_path).read_text())
    binds = data["services"]["main"]["volumes"]
    assert len(binds) == 2
    assert all(bind["bind"] == {"selinux": "z"} for bind in binds)


def test_override_path_is_appended_last_to_compose_paths(tmp_path):
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    assert env._docker_compose_paths[-1] == env._compose_override_path


# ---------------------------------------------------------------------------
# Preflight -- podman, then krun resolution, then the krun digest pin
# ---------------------------------------------------------------------------


def test_preflight_probes_krun_then_its_own_pin(monkeypatch):
    order = []

    monkeypatch.setattr(
        PodmanEnvironment,
        "preflight",
        classmethod(lambda cls: order.append("podman")),
    )
    monkeypatch.setattr(
        "titanium.environments.gvisor.podman.assert_runtime_resolvable",
        lambda runtime, cli="podman", timeout_sec=30: order.append(
            ("runtime", runtime)
        ),
    )
    monkeypatch.setattr(
        "titanium.environments.krun.podman.assert_runtime_digest",
        lambda pin, init_script=None: order.append(("digest", pin, init_script)),
    )

    KrunPodmanEnvironment.preflight()

    assert order == [
        "podman",
        ("runtime", "krun"),
        ("digest", KRUN_DIGEST_PIN, "scripts/init/krun-podman.sh"),
    ]


def test_preflight_never_touches_the_docker_daemon(monkeypatch):
    monkeypatch.setattr(PodmanEnvironment, "preflight", classmethod(lambda cls: None))
    monkeypatch.setattr(
        "titanium.environments.gvisor.podman.assert_runtime_resolvable",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "titanium.environments.krun.podman.assert_runtime_digest",
        lambda *a, **k: None,
    )

    def boom(*args, **kwargs):  # pragma: no cover - the point is it never runs
        raise AssertionError("docker daemon consulted during krun preflight")

    monkeypatch.setattr("titanium.environments.gvisor.runtime.engine_runtimes", boom)

    KrunPodmanEnvironment.preflight()


# ---------------------------------------------------------------------------
# Digest pin -- krun's own pin, independent from the runsc pin
# ---------------------------------------------------------------------------


def test_krun_pin_fails_closed_and_names_its_init_script(tmp_path, monkeypatch):
    pin, binary = _pin(tmp_path, b"microvm")
    binary.write_bytes(b"impostor")
    monkeypatch.setenv("TITANIUM_KRUN_DIGEST_PIN", str(pin))
    with pytest.raises(RuntimeError, match=r"krun-podman\.sh"):
        KrunPodmanEnvironment._assert_runtime_digest()


def test_krun_pin_ignores_the_runsc_pin(tmp_path, monkeypatch):
    # Each runtime is blessed on its own: a tampered runsc must not block
    # krun trials, and the other way around.
    runsc_pin, runsc_binary = _pin(tmp_path, b"sentry")
    runsc_binary.write_bytes(b"impostor")
    monkeypatch.setenv("TITANIUM_RUNSC_DIGEST_PIN", str(runsc_pin))
    monkeypatch.setenv("TITANIUM_KRUN_DIGEST_PIN", str(tmp_path / "absent"))

    KrunPodmanEnvironment._assert_runtime_digest()  # must not raise

    with pytest.raises(RuntimeError, match=r"runsc-podman\.sh"):
        GVisorPodmanEnvironment._assert_runtime_digest()


def test_missing_krun_pin_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TITANIUM_KRUN_DIGEST_PIN", str(tmp_path / "absent"))
    KrunPodmanEnvironment._assert_runtime_digest()


def test_digest_helper_names_the_given_init_script(tmp_path):
    pin, binary = _pin(tmp_path, b"microvm")
    binary.write_bytes(b"impostor")
    with pytest.raises(RuntimeError, match=r"scripts/init/krun-podman\.sh"):
        assert_runtime_digest(pin, init_script="scripts/init/krun-podman.sh")


# ---------------------------------------------------------------------------
# Mailbox exec -- the file protocol that replaces the missing handler exec
# ---------------------------------------------------------------------------

import asyncio

from titanium.environments.gvisor.environment import VerificationState
from titanium.environments.krun.podman import (
    MAILBOX_ALIVE_TIMEOUT_ENV,
    MAILBOX_CMD_DIR,
    MAILBOX_DIR_NAME,
    MAILBOX_RUNNER_NAME,
    MAILBOX_RUNNER_SCRIPT,
)


def _ready_env(tmp_path, **kwargs):
    """A prepared environment with verification forced READY.

    The mailbox tests exercise the exec protocol, not the verification
    gate; the gate has its own tests in the gvisor suites.
    """
    env = _make_env(tmp_path, **kwargs)
    env._prepare_gvisor()
    env._state = VerificationState.READY
    return env


async def _fake_runner(env, replies=None):
    """Serve the host-side mailbox dirs the way the guest runner does.

    Handles exactly one command file, then returns its rendered script so
    tests can assert on the file contents.
    """
    cmd_dir = env._stage_in / MAILBOX_DIR_NAME
    res_dir = env._stage_out / MAILBOX_DIR_NAME
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / ".runner-alive").touch()
    for _ in range(200):
        scripts = sorted(cmd_dir.glob("cmd-*.sh"))
        if scripts:
            script = scripts[-1]
            cmd_id = script.stem
            reply = replies or {"exit": "0", "out": "mailbox-ok\n"}
            (res_dir / f"{cmd_id}.out").write_text(reply["out"])
            (res_dir / f"{cmd_id}.exit").write_text(reply["exit"])
            return script.read_text()
        await asyncio.sleep(0.01)
    raise AssertionError("no command file appeared in the mailbox")


def test_override_installs_the_mailbox_runner_as_main_command(tmp_path):
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    data = json.loads(Path(env._compose_override_path).read_text())
    assert data["services"]["main"]["command"] == [
        "sh",
        str(MAILBOX_CMD_DIR / MAILBOX_RUNNER_NAME),
    ]
    # Root, so transfers' user="root" commands work and `su` can drop.
    assert data["services"]["main"]["user"] == "0"


def test_runsc_flavor_override_gains_no_command_or_user():
    # The seam defaults must keep the runsc flavor byte-identical.
    assert GVisorPodmanEnvironment._main_command(object()) is None
    assert GVisorPodmanEnvironment._main_user(object()) is None


def test_prepare_writes_the_runner_onto_the_staging_mount(tmp_path):
    env = _make_env(tmp_path)
    env._prepare_gvisor()
    runner = env._stage_in / MAILBOX_DIR_NAME / MAILBOX_RUNNER_NAME
    assert runner.read_text() == MAILBOX_RUNNER_SCRIPT
    assert (env._stage_out / MAILBOX_DIR_NAME).is_dir()


def test_exec_round_trips_through_the_mailbox(tmp_path):
    async def run():
        env = _ready_env(tmp_path)
        runner = asyncio.ensure_future(_fake_runner(env))
        result = await env.exec("echo hi")
        script = await runner
        return result, script

    result, script = asyncio.run(run())
    assert result.return_code == 0
    assert result.stdout == "mailbox-ok\n"
    assert "bash -c 'echo hi'" in script


def test_exec_renders_cwd_env_user_and_timeout(tmp_path):
    async def run():
        env = _ready_env(tmp_path)
        runner = asyncio.ensure_future(_fake_runner(env))
        await env.exec(
            "id",
            cwd="/work dir",
            env={"A_KEY": "a value"},
            timeout_sec=7,
            user="agent",
        )
        return await runner

    script = asyncio.run(run())
    assert "export A_KEY='a value'" in script
    assert "cd '/work dir'" in script
    assert "timeout 7 " in script
    assert "su -p -s /bin/sh agent -c" in script


def test_exec_as_root_uses_no_su(tmp_path):
    async def run():
        env = _ready_env(tmp_path)
        runner = asyncio.ensure_future(_fake_runner(env))
        await env.exec("id", user="root")
        return await runner

    script = asyncio.run(run())
    assert "su " not in script


def test_exec_returns_nonzero_exit_codes(tmp_path):
    async def run():
        env = _ready_env(tmp_path)
        runner = asyncio.ensure_future(
            _fake_runner(env, replies={"exit": "3", "out": "boom\n"})
        )
        result = await env.exec("false")
        await runner
        return result

    result = asyncio.run(run())
    assert result.return_code == 3
    assert result.stdout == "boom\n"


def test_exec_raises_on_guest_side_timeout(tmp_path):
    # The guest `timeout` wrapper exits 124; mirror the inherited exec,
    # which raises instead of returning a silent partial result.
    async def run():
        env = _ready_env(tmp_path)
        runner = asyncio.ensure_future(
            _fake_runner(env, replies={"exit": "124", "out": ""})
        )
        try:
            with pytest.raises(RuntimeError, match="timed out after 9"):
                await env.exec("sleep 99", timeout_sec=9)
        finally:
            await runner

    asyncio.run(run())


def test_exec_fails_fast_when_the_runner_never_came_up(tmp_path, monkeypatch):
    monkeypatch.setenv(MAILBOX_ALIVE_TIMEOUT_ENV, "0.2")

    async def run():
        env = _ready_env(tmp_path)
        with pytest.raises(RuntimeError, match="runner did not come up"):
            await env.exec("echo hi")

    asyncio.run(run())


def test_attach_is_refused(tmp_path):
    env = _make_env(tmp_path)
    with pytest.raises(NotImplementedError, match="batch-only"):
        asyncio.run(env.attach())
