"""Unit tests for image-reference qualification in build preparation.

Short names in the Dockerfile dialect mean Docker Hub; Titanium makes that
explicit at the byte level so no engine ever consults host-global
search-registry configuration for Titanium-prepared builds (PODMAN.md §2.1/§4).
"""

from titanium.environments.agent_setup import (
    qualify_dockerfile_froms,
    qualify_image_reference,
    write_agent_dockerfile,
)
from titanium.models.agent.install import AgentInstallSpec, InstallStep


def test_short_names_gain_dockers_implied_registry():
    assert qualify_image_reference("ubuntu:24.04") == "docker.io/library/ubuntu:24.04"
    assert qualify_image_reference("ubuntu") == "docker.io/library/ubuntu"
    assert (
        qualify_image_reference("alexgshaw/build-pmars")
        == "docker.io/alexgshaw/build-pmars"
    )


def test_qualified_references_pass_through():
    for ref in (
        "docker.io/library/ubuntu:24.04",
        "quay.io/podman/stable",
        "ghcr.io/org/img:v1",
        "localhost/fix-git-offline__abc_main:latest",
        "registry:5000/img",
    ):
        assert qualify_image_reference(ref) == ref


def test_scratch_and_variables_pass_through():
    assert qualify_image_reference("scratch") == "scratch"
    assert qualify_image_reference("${BASE_IMAGE}") == "${BASE_IMAGE}"
    assert qualify_image_reference("$BASE") == "$BASE"


def test_dockerfile_froms_are_qualified_in_place():
    text = "FROM ubuntu:24.04\nRUN true\n"
    assert (
        qualify_dockerfile_froms(text) == "FROM docker.io/library/ubuntu:24.04\nRUN true\n"
    )


def test_multi_stage_references_are_never_qualified():
    text = (
        "FROM golang:1.22 AS builder\n"
        "RUN build\n"
        "FROM builder\n"
        "FROM BUILDER AS again\n"
        "FROM ubuntu:24.04\n"
    )
    out = qualify_dockerfile_froms(text)
    assert "FROM docker.io/library/golang:1.22 AS builder" in out
    assert "\nFROM builder\n" in out  # stage ref untouched
    assert "\nFROM BUILDER AS again\n" in out  # case-insensitive stage match
    assert "FROM docker.io/library/ubuntu:24.04" in out


def test_platform_flag_and_casing_are_preserved():
    out = qualify_dockerfile_froms("from --platform=linux/amd64 alpine:3.20 as base\n")
    assert out == "from --platform=linux/amd64 docker.io/library/alpine:3.20 as base\n"


def test_agent_dockerfile_embeds_qualified_task_dockerfile(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN task-setup\n")
    install = AgentInstallSpec(
        agent_name="test-agent", steps=[InstallStep(run="true", user="root")]
    )
    path = write_agent_dockerfile(
        build_dir=tmp_path / "build",
        source_environment_dir=tmp_path,
        prebuilt_image_name=None,
        install=install,
        user=None,
    )
    text = path.read_text()
    assert "FROM docker.io/library/ubuntu:24.04" in text
    assert "RUN task-setup" in text


def test_agent_dockerfile_qualifies_prebuilt_image(tmp_path):
    install = AgentInstallSpec(
        agent_name="test-agent", steps=[InstallStep(run="true", user="root")]
    )
    path = write_agent_dockerfile(
        build_dir=tmp_path / "build",
        source_environment_dir=tmp_path,
        prebuilt_image_name="alexgshaw/build-pmars",
        install=install,
        user=None,
    )
    assert "FROM docker.io/alexgshaw/build-pmars" in path.read_text()
