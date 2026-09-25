"""Export-based archive behavior for the rootless Podman family.

Every engine call is a double: these run without a real container engine.
What they pin is the contract -- which engine binary is invoked, which
container is exported, that the tar is validated before it is published,
that the container is only reclaimed after a successful export, and that
a failure is raised rather than reported as a completed archive.

Docker retains shared export mechanics for Podman reuse, but Docker itself
does not expose environment archiving.
"""

import asyncio
import json
import tarfile

import pytest

from titanium.environments.base import ArchiveError, ExecResult
from titanium.environments.docker.docker import (
    ARCHIVE_METADATA_NAME,
    ARCHIVE_TAR_NAME,
    DockerEnvironment,
)
from titanium.environments.gvisor.environment import GVisorEnvironment
from titanium.environments.podman.podman import PodmanEnvironment
from titanium.models.trial.paths import TrialPaths


MAIN_ID = "a" * 64


def _tar_bytes(names=("app/result.txt",)) -> bytes:
    """A minimal readable tar, standing in for an engine export."""
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in names:
            data = b"guest-written\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _environment(tmp_path, **overrides):
    """A PodmanEnvironment with only what the archive path touches."""
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    env = object.__new__(PodmanEnvironment)
    env.trial_paths = trial_paths
    env.session_id = "task__abc1234"
    env._keep_containers = False
    env.podman_bin = "podman"
    env.logger = __import__("logging").getLogger("test")

    class _EnvVars:
        main_image_name = "hb__task"

        def to_env_dict(self, include_os_env=True):
            return {}

    env._env_vars = _EnvVars()

    for name, value in overrides.items():
        setattr(env, name, value)

    return env


def _stub(
    env,
    monkeypatch,
    *,
    tar=None,
    export_rc=0,
    status="exited",
    main=MAIN_ID,
    rootless=True,
):
    """Record compose calls; answer engine calls from the host side."""
    calls = {"compose": [], "engine": []}

    async def fake_compose(command, check=True, timeout_sec=None):
        calls["compose"].append(list(command))
        return ExecResult(stdout="", return_code=0)

    async def fake_engine(args, timeout_sec=None):
        calls["engine"].append(list(args))

        if args and args[0] == "info":
            return ExecResult(
                stdout="true\n" if rootless else "false\n",
                return_code=0,
            )

        if args and args[0] == "inspect":
            return ExecResult(stdout=status, return_code=0)

        if args and args[0] == "export":
            if export_rc == 0 and tar is not None:
                # -o <path> <container>
                __import__("pathlib").Path(args[2]).write_bytes(tar)

            return ExecResult(
                stdout="",
                stderr="export refused",
                return_code=export_rc,
            )

        return ExecResult(stdout="", return_code=0)

    async def fake_main_id():
        return main

    async def noop():
        return None

    monkeypatch.setattr(
        env,
        "_run_docker_compose_command",
        fake_compose,
    )
    monkeypatch.setattr(
        env,
        "_run_engine_command",
        fake_engine,
    )
    monkeypatch.setattr(
        env,
        "_archive_container_id",
        fake_main_id,
    )
    monkeypatch.setattr(
        env,
        "prepare_logs_for_host",
        lambda: noop(),
    )
    monkeypatch.setattr(
        env,
        "_cleanup_resources_compose_file",
        lambda: None,
    )

    return calls


# ---------------------------------------------------------------------------
# Engine selection and container selection
# ---------------------------------------------------------------------------


def test_engine_names_for_shared_archive_helpers():
    assert DockerEnvironment._engine_command_name(
        object.__new__(DockerEnvironment)
    ) == "docker"

    podman = object.__new__(PodmanEnvironment)
    podman.podman_bin = "/usr/bin/podman"
    assert podman._engine_command_name() == "/usr/bin/podman"

    gvisor = object.__new__(GVisorEnvironment)
    gvisor._engine_cli = "podman"
    assert gvisor._engine_command_name() == "podman"


def test_docker_archive_is_refused_directly():
    env = object.__new__(DockerEnvironment)

    with pytest.raises(
        NotImplementedError,
        match="restricted to rootless Podman-family environments",
    ):
        asyncio.run(env.archive(delete=True))


def test_docker_helper_resolves_main_including_stopped(tmp_path, monkeypatch):
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()

    env = object.__new__(DockerEnvironment)
    env.trial_paths = trial_paths
    env.session_id = "task__abc1234"

    seen = []

    async def fake_compose(command, check=True, timeout_sec=None):
        seen.append(list(command))
        return ExecResult(stdout=f"{MAIN_ID}\n", return_code=0)

    monkeypatch.setattr(
        env,
        "_run_docker_compose_command",
        fake_compose,
    )

    assert asyncio.run(env._archive_container_id()) == MAIN_ID
    assert seen == [["ps", "--all", "--quiet", "main"]]


def test_podman_resolves_main_by_label(monkeypatch):
    env = object.__new__(PodmanEnvironment)
    asked = []

    async def fake_resolve(service="main"):
        asked.append(service)
        return MAIN_ID

    monkeypatch.setattr(
        env,
        "resolve_container",
        fake_resolve,
    )

    assert asyncio.run(env._archive_container_id()) == MAIN_ID
    assert asked == ["main"]


# ---------------------------------------------------------------------------
# Rootless Podman export
# ---------------------------------------------------------------------------


def test_archive_stops_then_exports_then_reclaims(tmp_path, monkeypatch):
    env = _environment(tmp_path)
    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(env.archive(delete=True))

    # Execution stops before the snapshot; the container is reclaimed after.
    assert calls["compose"][0] == ["stop"]
    assert calls["compose"][-1] == [
        "down",
        "--rmi",
        "all",
        "--volumes",
        "--remove-orphans",
    ]

    export = [
        call
        for call in calls["engine"]
        if call and call[0] == "export"
    ]

    assert len(export) == 1
    assert export[0][-1] == MAIN_ID
    assert calls["compose"].index(["stop"]) == 0


def test_archive_publishes_a_readable_tar(tmp_path, monkeypatch):
    env = _environment(tmp_path)

    _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(("app/result.txt",)),
    )

    asyncio.run(env.archive(delete=True))

    tar_path = (
        env.trial_paths.archive_dir
        / ARCHIVE_TAR_NAME
    )

    assert tar_path.is_file()

    with tarfile.open(tar_path) as tar:
        assert "app/result.txt" in tar.getnames()

    # No partial file is left next to it.
    assert not (
        env.trial_paths.archive_dir
        / f"{ARCHIVE_TAR_NAME}.partial"
    ).exists()


def test_archive_writes_metadata(tmp_path, monkeypatch):
    env = _environment(tmp_path)

    _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(env.archive(delete=True))

    metadata = json.loads(
        (
            env.trial_paths.archive_dir
            / ARCHIVE_METADATA_NAME
        ).read_text()
    )

    assert metadata["trial"] == "task__abc1234"
    assert metadata["environment_type"] == "podman"
    assert metadata["engine"] == "podman"
    assert metadata["image"] == "hb__task"
    assert metadata["container_id"] == MAIN_ID
    assert metadata["archive"] == ARCHIVE_TAR_NAME
    assert metadata["entries"] >= 1
    assert metadata["size_bytes"] > 0


def test_archive_metadata_records_no_environment_variables(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(env.archive(delete=True))

    metadata = json.loads(
        (
            env.trial_paths.archive_dir
            / ARCHIVE_METADATA_NAME
        ).read_text()
    )

    # Identity and provenance only: no env, credentials or host paths.
    assert set(metadata) == {
        "trial",
        "environment_type",
        "engine",
        "runtime",
        "image",
        "service",
        "container_id",
        "archive",
        "entries",
        "size_bytes",
        "created_at",
    }

    assert "/home/" not in json.dumps(metadata)


def test_archive_is_stored_outside_the_mounted_directories(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(env.archive(delete=True))

    archive_dir = env.trial_paths.archive_dir

    # The guest binds agent/verifier/artifacts; the archive is reachable from
    # none of them.
    for mounted in (
        env.trial_paths.agent_dir,
        env.trial_paths.verifier_dir,
        env.trial_paths.artifacts_dir,
    ):
        assert mounted not in archive_dir.parents
        assert archive_dir != mounted


# ---------------------------------------------------------------------------
# Failure: never report a completed archive, never destroy the last copy
# ---------------------------------------------------------------------------


def test_export_failure_raises_and_keeps_the_container(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=None,
        export_rc=1,
    )

    with pytest.raises(
        ArchiveError,
        match="export exited 1",
    ):
        asyncio.run(env.archive(delete=True))

    assert not any(
        call and call[0] == "down"
        for call in calls["compose"]
    )

    assert not (
        env.trial_paths.archive_dir
        / ARCHIVE_TAR_NAME
    ).exists()

    assert not (
        env.trial_paths.archive_dir
        / f"{ARCHIVE_TAR_NAME}.partial"
    ).exists()


def test_corrupt_tar_raises_and_keeps_the_container(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=b"not a tar at all",
    )

    with pytest.raises(
        ArchiveError,
        match="could not be read back",
    ):
        asyncio.run(env.archive(delete=True))

    assert not any(
        call and call[0] == "down"
        for call in calls["compose"]
    )

    assert not (
        env.trial_paths.archive_dir
        / ARCHIVE_TAR_NAME
    ).exists()


def test_empty_tar_is_rejected(tmp_path, monkeypatch):
    import io

    buffer = io.BytesIO()

    with tarfile.open(
        fileobj=buffer,
        mode="w",
    ):
        pass

    env = _environment(tmp_path)

    _stub(
        env,
        monkeypatch,
        tar=buffer.getvalue(),
    )

    with pytest.raises(
        ArchiveError,
        match="could not be read back",
    ):
        asyncio.run(env.archive(delete=True))


def test_missing_main_container_raises(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
        main=None,
    )

    with pytest.raises(
        ArchiveError,
        match="no 'main' container",
    ):
        asyncio.run(env.archive(delete=True))

    assert not any(
        call and call[0] == "down"
        for call in calls["compose"]
    )


def test_still_running_container_is_not_exported(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
        status="running",
    )

    with pytest.raises(
        ArchiveError,
        match="still running",
    ):
        asyncio.run(env.archive(delete=True))

    assert not any(
        call and call[0] == "export"
        for call in calls["engine"]
    )

    assert not any(
        call and call[0] == "down"
        for call in calls["compose"]
    )


# ---------------------------------------------------------------------------
# Retention and ordinary teardown
# ---------------------------------------------------------------------------


def test_keep_containers_keeps_the_container_and_exports_nothing(
    tmp_path,
    monkeypatch,
):
    env = _environment(
        tmp_path,
        _keep_containers=True,
    )

    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(env.stop(delete=True))

    assert calls["compose"] == [["stop"]]
    assert calls["engine"] == []
    assert not env.trial_paths.archive_dir.exists()


@pytest.mark.parametrize(
    ("delete", "expected"),
    [
        (
            True,
            [
                "down",
                "--rmi",
                "all",
                "--volumes",
                "--remove-orphans",
            ],
        ),
        (
            False,
            ["down"],
        ),
    ],
)
def test_ordinary_teardown_is_unchanged(
    tmp_path,
    monkeypatch,
    delete,
    expected,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
    )

    asyncio.run(
        env.stop(delete=delete)
    )

    assert calls["compose"] == [expected]
    assert calls["engine"] == []
    assert not env.trial_paths.archive_dir.exists()


def test_rootful_podman_archive_is_refused_before_export(
    tmp_path,
    monkeypatch,
):
    env = _environment(tmp_path)

    calls = _stub(
        env,
        monkeypatch,
        tar=_tar_bytes(),
        rootless=False,
    )

    with pytest.raises(
        ArchiveError,
        match="requires rootless Podman",
    ):
        asyncio.run(env.archive(delete=True))

    assert calls["compose"] == []
    assert not any(
        call and call[0] == "export"
        for call in calls["engine"]
    )
    assert not env.trial_paths.archive_dir.exists()
