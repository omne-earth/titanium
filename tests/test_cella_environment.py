"""Contract tests for ``CellaEnvironment`` -- the Titanium side of ``--env cella``.

Nothing here boots a machine or needs ``/dev/kvm``: every ``cella`` invocation
is either projected to argv and asserted, or intercepted. What is pinned is the
seam -- what Titanium refuses, what it projects, and what it runs -- not Cella's
own behaviour, which Cella's suite owns.
"""

import subprocess
import types
from pathlib import Path

import pytest

from titanium.environments.cella.environment import (
    CellaEnvironment,
    operator_cella_home,
)
from titanium.environments.cella.spec import build_machine_spec
from titanium.environments.factory import EnvironmentFactory, _load_environment_class
from titanium.models.environment_type import EnvironmentType
from titanium.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from titanium.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from titanium.models.trial.paths import TrialPaths

CELLA_TOML = """
[cella]
kernel = "canonical"
rootfs = "titanium-smoke"
"""


def make_environment_dir(tmp_path: Path, contents: str | None = CELLA_TOML) -> Path:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir(parents=True, exist_ok=True)
    if contents is not None:
        (environment_dir / "cella.toml").write_text(contents)
    return environment_dir


def make_trial_paths(tmp_path: Path) -> TrialPaths:
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return trial_paths


def make_environment(
    tmp_path: Path, *, definition=CELLA_TOML, **task_env
) -> CellaEnvironment:
    """A constructed CellaEnvironment, the way the factory builds one."""
    task_env.setdefault("allow_internet", False)
    return CellaEnvironment(
        environment_dir=make_environment_dir(tmp_path, definition),
        environment_name="verify-cella-env",
        session_id="verify-cella-env__AbCdEfG",
        trial_paths=make_trial_paths(tmp_path),
        task_env_config=TaskEnvironmentConfig(**task_env),
    )


class RecordingCella(CellaEnvironment):
    """CellaEnvironment with the subprocess boundary replaced by a recorder."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[list[str]] = []
        self.return_codes: dict[str, int] = {}

    async def _run_cella(self, args, *, timeout_sec=None):
        self.calls.append(list(args))
        return self.return_codes.get(args[0], 0), ""


def make_recording_environment(tmp_path: Path, **task_env) -> RecordingCella:
    task_env.setdefault("allow_internet", False)
    return RecordingCella(
        environment_dir=make_environment_dir(tmp_path),
        environment_name="verify-cella-env",
        session_id="verify-cella-env__AbCdEfG",
        trial_paths=make_trial_paths(tmp_path),
        task_env_config=TaskEnvironmentConfig(**task_env),
    )


# ---------------------------------------------------------------------------
# Environment type and factory registration
# ---------------------------------------------------------------------------


def test_cella_is_a_first_class_environment_type():
    assert EnvironmentType.CELLA.value == "cella"
    assert EnvironmentType("cella") is EnvironmentType.CELLA


def test_cella_environment_reports_its_type():
    assert CellaEnvironment.type() is EnvironmentType.CELLA


def test_cella_is_registered_in_the_factory():
    assert _load_environment_class(EnvironmentType.CELLA) is CellaEnvironment


def test_cella_needs_no_optional_extra():
    from titanium.environments.factory import _ENVIRONMENT_REGISTRY

    assert _ENVIRONMENT_REGISTRY[EnvironmentType.CELLA].pip_extra is None


def test_env_cella_resolves_through_the_factory(tmp_path: Path):
    environment = EnvironmentFactory.create_environment_from_config(
        TrialEnvironmentConfig(type=EnvironmentType.CELLA),
        environment_dir=make_environment_dir(tmp_path),
        environment_name="verify-cella-env",
        session_id="verify-cella-env__AbCdEfG",
        trial_paths=make_trial_paths(tmp_path),
        task_env_config=TaskEnvironmentConfig(allow_internet=False),
    )
    assert isinstance(environment, CellaEnvironment)


# ---------------------------------------------------------------------------
# Construction and the machine-spec projection
# ---------------------------------------------------------------------------


def test_environment_constructs_for_a_valid_cella_task(tmp_path: Path):
    environment = make_environment(tmp_path)

    assert environment.definition.kernel == "canonical"
    assert environment.definition.rootfs == "titanium-smoke"


def test_environment_delegates_identity_to_build_machine_spec(tmp_path: Path):
    environment = make_environment(tmp_path)

    expected = build_machine_spec(
        session_id=environment.session_id,
        environment_name=environment.environment_name,
        task_env_config=environment.task_env_config,
        definition=environment.definition,
        trial_paths=environment.trial_paths,
    )
    assert environment.spec == expected


def test_capabilities_claim_only_network_isolation(tmp_path: Path):
    capabilities = make_environment(tmp_path).capabilities

    assert capabilities.disable_internet is True
    assert capabilities.gpus is False
    assert capabilities.windows is False
    assert capabilities.preinstall_agents is False
    assert capabilities.docker_compose is False
    assert capabilities.mounted is False


def test_memory_is_the_only_enforceable_resource():
    resource_capabilities = CellaEnvironment.resource_capabilities()

    assert resource_capabilities.memory_limit is True
    assert resource_capabilities.cpu_limit is False
    assert resource_capabilities.cpu_request is False


# ---------------------------------------------------------------------------
# CELLA_HOME
# ---------------------------------------------------------------------------


def test_cella_home_is_the_spec_value(tmp_path: Path):
    environment = make_environment(tmp_path)

    assert environment.cella_home == environment.trial_paths.trial_dir / ".cella"


def test_cella_home_is_exported_to_every_invocation(tmp_path: Path):
    environment = make_environment(tmp_path)

    process_env = environment.cella_process_env()

    assert process_env["CELLA_HOME"] == str(environment.cella_home)


def test_goldens_are_linked_into_the_trial_registry(tmp_path: Path, monkeypatch):
    # CELLA_HOME relocates the golden store as well as the machine registry,
    # so a trial-private home must still see the operator's goldens.
    operator_home = tmp_path / "operator-cella"
    (operator_home / "kernel").mkdir(parents=True)
    (operator_home / "rootfs").mkdir(parents=True)
    monkeypatch.setenv("CELLA_HOME", str(operator_home))

    environment = make_environment(tmp_path)
    environment._prepare_cella_home()

    assert (environment.cella_home / "kernel").resolve() == operator_home / "kernel"
    assert (environment.cella_home / "rootfs").resolve() == operator_home / "rootfs"
    # The machine registry itself stays trial-private.
    assert not (environment.cella_home / "machines").is_symlink()


def test_operator_cella_home_mirrors_cellas_own_rule(monkeypatch):
    monkeypatch.setenv("CELLA_HOME", "/somewhere/else")
    assert operator_cella_home() == "/somewhere/else"

    monkeypatch.delenv("CELLA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/someone")
    assert operator_cella_home() == "/home/someone/.cella"


# ---------------------------------------------------------------------------
# `cella create` projection
# ---------------------------------------------------------------------------


def test_create_carries_name_flavors_and_net(tmp_path: Path):
    environment = make_environment(tmp_path)

    args = environment._create_args()

    assert args[0] == "create"
    assert args[1] == environment.spec.name
    assert args[args.index("--kernel") + 1] == "canonical"
    assert args[args.index("--rootfs") + 1] == "titanium-smoke"
    assert args[args.index("--net") + 1] == "none"


def test_unset_memory_omits_the_flag(tmp_path: Path):
    # Cella's own default (256 MiB) then applies; Titanium never restates it.
    environment = make_environment(tmp_path)

    assert environment.spec.mem_mb is None
    assert "--mem-mb" not in environment._create_args()


def test_declared_memory_is_passed_exactly(tmp_path: Path):
    environment = make_environment(tmp_path, memory_mb=1024)

    args = environment._create_args()

    assert args[args.index("--mem-mb") + 1] == "1024"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_creates_then_starts_the_machine(tmp_path: Path):
    environment = make_recording_environment(tmp_path)

    await environment.start(force_build=False)

    assert environment.calls[0] == environment._create_args()
    assert environment.calls[1] == ["start", environment.spec.name]


@pytest.mark.asyncio
async def test_start_never_builds_a_golden(tmp_path: Path):
    # `cella build` is the only thing that makes a golden, and Titanium is not
    # allowed to call it: the supply chain is Cella's.
    environment = make_recording_environment(tmp_path)

    await environment.start(force_build=True)

    assert [call[0] for call in environment.calls] == ["create", "start"]


@pytest.mark.asyncio
async def test_start_fails_loudly_when_create_fails(tmp_path: Path):
    environment = make_recording_environment(tmp_path)
    environment.return_codes["create"] = 1

    with pytest.raises(RuntimeError):
        await environment.start(force_build=False)

    assert [call[0] for call in environment.calls] == ["create"]


@pytest.mark.asyncio
async def test_stop_without_delete_keeps_the_machine(tmp_path: Path):
    environment = make_recording_environment(tmp_path)

    await environment.stop(delete=False)

    assert environment.calls == [["stop", environment.spec.name]]


@pytest.mark.asyncio
async def test_stop_with_delete_destroys_the_machine(tmp_path: Path):
    environment = make_recording_environment(tmp_path)

    await environment.stop(delete=True)

    assert environment.calls == [
        ["stop", environment.spec.name],
        ["destroy", environment.spec.name],
    ]


@pytest.mark.asyncio
async def test_stop_is_best_effort(tmp_path: Path):
    # Cleanup also runs after a failed start, where there may be no machine to
    # stop; raising there would mask the original failure.
    environment = make_recording_environment(tmp_path)
    environment.return_codes["stop"] = 1

    await environment.stop(delete=True)

    assert [call[0] for call in environment.calls] == ["stop", "destroy"]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_refuses_a_missing_cella_binary(monkeypatch):
    monkeypatch.setattr(
        "titanium.environments.cella.environment.shutil.which", lambda _: None
    )

    with pytest.raises(SystemExit):
        CellaEnvironment.preflight()


def test_preflight_runs_cella_doctor_check(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "titanium.environments.cella.environment.shutil.which",
        lambda _: "/usr/bin/cella",
    )

    def fake_run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    CellaEnvironment.preflight()

    assert calls == [["cella", "doctor", "check"]]


def test_preflight_refuses_a_failing_host(monkeypatch):
    monkeypatch.setattr(
        "titanium.environments.cella.environment.shutil.which",
        lambda _: "/usr/bin/cella",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="FAIL /dev/kvm", stderr=""
        ),
    )

    with pytest.raises(SystemExit):
        CellaEnvironment.preflight()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_declaration_is_refused(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        make_environment(tmp_path, definition=None)


def test_declaration_without_a_cella_table_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match=r"\[cella\] table"):
        make_environment(tmp_path, definition='[other]\nkernel = "canonical"\n')


def test_declaration_missing_a_flavor_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="valid cella declaration"):
        make_environment(tmp_path, definition='[cella]\nkernel = "canonical"\n')


def test_unverifiable_rootfs_pin_is_refused(tmp_path: Path):
    # Accepting a pin nothing checks is worse than refusing it.
    definition = CELLA_TOML + 'rootfs_digest = "sha3-256:9f2c"\n'

    with pytest.raises(ValueError, match="rootfs_digest"):
        make_environment(tmp_path, definition=definition)


def test_a_task_wanting_internet_is_refused(tmp_path: Path):
    # Not covered centrally: the base validator only refuses allow_internet
    # = false on environments that cannot isolate. Cella is the other way
    # round -- it only has `--net none`.
    with pytest.raises(ValueError, match="net none"):
        make_environment(tmp_path, allow_internet=True)


def test_gpu_tasks_are_refused_by_the_central_validator(tmp_path: Path):
    with pytest.raises(RuntimeError, match="GPU"):
        make_environment(tmp_path, gpus=1)


def test_windows_tasks_are_refused_by_the_central_validator(tmp_path: Path):
    with pytest.raises(RuntimeError, match="windows"):
        make_environment(tmp_path, os="windows")


# ---------------------------------------------------------------------------
# The sealed guest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda env: env.exec("echo hi"),
        lambda env: env.upload_file("/host/file", "/guest/file"),
        lambda env: env.upload_dir("/host/dir", "/guest/dir"),
        lambda env: env.download_file("/guest/file", "/host/file"),
        lambda env: env.download_dir("/guest/dir", "/host/dir"),
    ],
)
async def test_host_driven_verbs_refuse_rather_than_pretend(tmp_path: Path, call):
    environment = make_environment(tmp_path)

    with pytest.raises(NotImplementedError, match="guest-sealed"):
        await call(environment)
