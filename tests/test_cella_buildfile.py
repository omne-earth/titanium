"""Build-file discovery and build-context staging for the Cella converter.

Discovery is a refusal-first contract: exactly one of Dockerfile/Containerfile
is a task, and anything else names what it found instead of picking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from titanium.environments.cella.buildfile import (
    STAGED_BUILD_FILE_NAME,
    BuildFileError,
    discover_build_file,
    prepare_build_context,
)
from titanium.models.agent.install import AgentInstallSpec, InstallStep

DOCKERFILE = "FROM ubuntu:24.04\nWORKDIR /app\n"


def _environment(tmp_path, name: str, text: str = DOCKERFILE):
    env = tmp_path / "environment"
    env.mkdir(exist_ok=True)
    (env / name).write_text(text)
    return env


def test_dockerfile_only_is_selected(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    assert discover_build_file(env).name == "Dockerfile"


def test_containerfile_only_is_selected(tmp_path):
    env = _environment(tmp_path, "Containerfile")
    assert discover_build_file(env).name == "Containerfile"


def test_both_build_files_is_refused_and_names_both(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    (env / "Containerfile").write_text(DOCKERFILE)
    with pytest.raises(BuildFileError) as excinfo:
        discover_build_file(env)
    message = str(excinfo.value)
    assert "Dockerfile" in message and "Containerfile" in message


def test_no_build_file_is_refused_and_names_what_was_expected(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    with pytest.raises(BuildFileError) as excinfo:
        discover_build_file(env)
    message = str(excinfo.value)
    assert "Dockerfile" in message and "Containerfile" in message


def test_a_directory_named_dockerfile_is_not_a_build_file(tmp_path):
    env = tmp_path / "environment"
    (env / "Dockerfile").mkdir(parents=True)
    with pytest.raises(BuildFileError):
        discover_build_file(env)


def test_staging_qualifies_from_lines(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    staged = prepared.build_file.read_text()
    assert staged.startswith("FROM docker.io/library/ubuntu:24.04")
    # PODMAN.md section 2.1's residual: a task built from its own Dockerfile
    # with no agent install used to bypass qualification entirely.
    assert prepared.source_build_file_bytes == DOCKERFILE.encode()


def test_containerfile_is_staged_under_the_dockerfile_name(tmp_path):
    env = _environment(tmp_path, "Containerfile")
    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    assert prepared.build_file.name == STAGED_BUILD_FILE_NAME
    assert prepared.source_build_file_name == "Containerfile"
    # Exactly one build file in the staged context, whatever the task called it.
    assert not (prepared.context_dir / "Containerfile").exists()


def test_staging_carries_the_rest_of_the_environment(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    (env / "seed.txt").write_text("payload")
    (env / "nested").mkdir()
    (env / "nested" / "inner.txt").write_text("inner")
    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    assert (prepared.context_dir / "seed.txt").read_text() == "payload"
    assert (prepared.context_dir / "nested" / "inner.txt").read_text() == "inner"


def test_staging_refuses_an_existing_context_directory(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    context = tmp_path / "ctx"
    context.mkdir()
    with pytest.raises(BuildFileError):
        prepare_build_context(environment_dir=env, context_dir=context)


def test_agent_install_is_baked_into_the_staged_build_file(tmp_path):
    env = _environment(tmp_path, "Containerfile")
    spec = AgentInstallSpec(
        agent_name="probe",
        steps=[InstallStep(run="echo installed", user="root")],
    )
    prepared = prepare_build_context(
        environment_dir=env,
        context_dir=tmp_path / "ctx",
        agent_install_spec=spec,
        agent_user="agent",
    )
    staged = prepared.staged_build_file_bytes.decode()
    assert "echo installed" in staged
    assert spec.fingerprint() in staged
    # Qualification survives the agent rewrite.
    assert "FROM docker.io/library/ubuntu:24.04" in staged
    # The source is still the task's own bytes, unrewritten.
    assert prepared.source_build_file_bytes == DOCKERFILE.encode()
    assert prepared.staged_build_file_bytes != prepared.source_build_file_bytes


# ------------------------------------------------------------------- symlinks


def test_an_ordinary_symlink_stays_a_symlink(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    (env / "real.txt").write_text("real")
    (env / "link.txt").symlink_to("real.txt")

    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    staged = prepared.context_dir / "link.txt"
    assert staged.is_symlink()
    assert staged.readlink() == Path("real.txt")


def test_a_dangling_symlink_survives_staging_without_failing_it(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    (env / "dangling").symlink_to("nowhere-at-all")

    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    staged = prepared.context_dir / "dangling"
    assert staged.is_symlink()
    assert not staged.exists()  # still dangling, not resolved
    assert staged.readlink() == Path("nowhere-at-all")


def test_a_symlink_out_of_the_environment_is_not_dereferenced(tmp_path):
    """The staged context must not acquire host file contents.

    copytree's default (symlinks=False) would read through this link and copy
    the secret's *bytes* into the build context, where the build would bake
    them into the image.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "host-secret"
    secret.write_text("SHOULD-NEVER-BE-STAGED")

    env = _environment(tmp_path, "Dockerfile")
    (env / "escape").symlink_to(secret)

    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    staged = prepared.context_dir / "escape"
    assert staged.is_symlink()
    assert staged.readlink() == secret
    assert "SHOULD-NEVER-BE-STAGED" not in (
        b"".join(
            path.read_bytes()
            for path in prepared.context_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        ).decode(errors="replace")
    )


def test_a_symlinked_build_file_is_replaced_not_written_through(tmp_path):
    """Writing the prepared file must not follow a link back into the task."""
    env = tmp_path / "environment"
    env.mkdir()
    (env / "real-dockerfile").write_text(DOCKERFILE)
    (env / "Dockerfile").symlink_to("real-dockerfile")

    prepared = prepare_build_context(environment_dir=env, context_dir=tmp_path / "ctx")
    assert not prepared.build_file.is_symlink()
    assert prepared.build_file.read_text().startswith("FROM docker.io/library/")
    # The link's target in the staged context is untouched.
    assert (prepared.context_dir / "real-dockerfile").read_text() == DOCKERFILE


# ----------------------------------------------------- install contamination flag


def test_agent_install_applied_reports_whether_user_directives_ran(tmp_path):
    env = _environment(tmp_path, "Dockerfile")
    plain = prepare_build_context(
        environment_dir=env, context_dir=tmp_path / "ctx-plain"
    )
    assert plain.agent_install_applied is False

    spec = AgentInstallSpec(
        agent_name="probe", steps=[InstallStep(run="true", user="root")]
    )
    baked = prepare_build_context(
        environment_dir=env,
        context_dir=tmp_path / "ctx-baked",
        agent_install_spec=spec,
    )
    assert baked.agent_install_applied is True
    # The reason the flag exists: the last USER wins and becomes Config.User.
    assert baked.staged_build_file_bytes.decode().rstrip().splitlines()[-2] == (
        "USER root"
    )
