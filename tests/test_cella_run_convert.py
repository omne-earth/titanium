"""Offline unit gate for the cella-run bake driver (scripts/cella_run_convert.py).

The driver is a script, not a package module, so it is loaded by path. These
cover its pure decisions -- the staged file set, the reflexive Dockerfile, the
run-on-boot script, and the boot layer -- without a podman build or a VM.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from titanium.environments.cella.boot_layer import (
    GuestFile,
    GuestSymlink,
    validate_boot_layer,
)

_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "cella_run_convert.py"


def _load():
    spec = importlib.util.spec_from_file_location("cella_run_convert", _DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


driver = _load()


def test_tracked_files_excludes_git_and_is_sorted(tmp_path):
    # A real tiny git tree, so the git plumbing is exercised, not mocked.
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "c.txt").write_text("c")

    names = driver.tracked_files(tmp_path)

    assert names == sorted(names)
    assert "a.txt" in names and "sub/b.txt" in names
    assert not any(n.startswith(".git/") for n in names)
    assert "ignored/c.txt" not in names  # gitignored, not baked


def test_reflexive_dockerfile_has_docker_uv_and_copy():
    text = driver.reflexive_dockerfile()
    assert "docker.io" in text  # a daemon for the inner --env docker run
    assert "uv sync" in text  # titanium's >=3.12 floor via uv
    assert f"COPY . {driver.GUEST_WORKSPACE}" in text


def test_run_script_names_the_task_and_ends_in_reset():
    text = driver.run_script("examples/smoke/cella-run")
    assert "--env docker" in text
    assert f"{driver.GUEST_WORKSPACE}/examples/smoke/cella-run" in text
    assert "reboot -f" in text  # the completion signal the host observes
    assert driver.RESULT_ROOT in text  # payload under the titanium result root


def test_boot_layer_is_valid_and_enables_the_unit():
    layer = driver.boot_layer("examples/smoke/cella-run")
    validate_boot_layer(layer)  # raises if not installable

    files = {e.path: e for e in layer.entries if isinstance(e, GuestFile)}
    links = {e.path: e for e in layer.entries if isinstance(e, GuestSymlink)}

    assert driver.RUN_SCRIPT_PATH in files
    assert f"/etc/systemd/system/{driver.RUN_UNIT_NAME}" in files
    # The wants symlink is how systemd enablement is spelled.
    wants = f"/etc/systemd/system/multi-user.target.wants/{driver.RUN_UNIT_NAME}"
    assert wants in links
    assert links[wants].target == f"../{driver.RUN_UNIT_NAME}"


def test_inner_base_images_skips_stages_and_scratch(tmp_path):
    env = tmp_path / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text(
        "FROM debian:12 AS build\n"
        "RUN echo hi\n"
        "FROM build\n"          # an earlier stage, not an image to seed
        "FROM scratch\n"        # never seeded
        "FROM python:3.13-slim-bookworm\n"
    )
    assert driver.inner_base_images(env) == ["debian:12", "python:3.13-slim-bookworm"]


def test_run_script_loads_seeded_images():
    text = driver.run_script("examples/smoke/cella-run")
    assert f"{driver.GUEST_WORKSPACE}/{driver.SEED_SUBDIR}/load.sh" in text


def _write_task(task_dir, toml_body, dockerfile="FROM python:3.13-slim\n"):
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(toml_body)
    env = task_dir / "environment"
    env.mkdir(exist_ok=True)
    (env / "Dockerfile").write_text(dockerfile)


def test_seed_plan_prebuilt_when_docker_image_declared(tmp_path):
    _write_task(tmp_path / "t", '[environment]\ndocker_image = "acme/foo:1"\n')
    images, prebuilt = driver.inner_seed_plan(tmp_path / "t")
    assert images == ["acme/foo:1"] and prebuilt is True


def test_seed_plan_builds_from_base_without_docker_image(tmp_path):
    _write_task(tmp_path / "t", "[environment]\nallow_internet = false\n")
    images, prebuilt = driver.inner_seed_plan(tmp_path / "t")
    assert images == ["python:3.13-slim"] and prebuilt is False


def test_prebuilt_run_script_sets_image_source():
    assert "TITANIUM_IMAGE_SOURCE=prebuilt" in driver.run_script("t", prebuilt=True)
    assert "TITANIUM_IMAGE_SOURCE=prebuilt" not in driver.run_script("t", prebuilt=False)


def test_stage_context_rejects_a_missing_task(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        driver.stage_context(tmp_path, "no/such/task", tmp_path / "ctx")


def test_run_script_points_titanium_at_the_seeded_proxy_image():
    text = driver.run_script("t")
    assert f"TITANIUM_EGRESS_PROXY_IMAGE={driver.EGRESS_PROXY_IMAGE}" in text


def test_seed_images_builds_and_exports_the_proxy(tmp_path, monkeypatch):
    # The engine is stubbed: record the argv, produce an empty inspect config.
    import subprocess as sp

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        out = "[{}]" if argv[1] == "inspect" else "cid"
        return sp.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    driver.seed_images(["python:3.13-slim"], tmp_path / "seed", engine="eng")

    verbs = [c[1] for c in calls]
    assert verbs.count("pull") == 1 and verbs.count("build") == 1
    assert (tmp_path / "seed" / "egress-proxy" / "Dockerfile").is_file()
    load = (tmp_path / "seed" / "load.sh").read_text()
    assert "docker import" in load
    assert load.strip().endswith(driver.EGRESS_PROXY_IMAGE)
    assert "python:3.13-slim" in load


def test_stage_context_bakes_the_agent_image_for_an_installed_agent(tmp_path, monkeypatch):
    import subprocess as sp

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write_task(tmp_path / "t", '[environment]\ndocker_image = "acme/foo:1"\n')
    calls = []
    real_run = sp.run

    def fake_run(argv, **kwargs):
        if argv[0] == "git":
            return real_run(argv, **kwargs)  # the tracked-file listing is real
        calls.append(list(argv))
        out = "[{}]" if argv[1] == "inspect" else "cid"
        return sp.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(driver.subprocess, "run", fake_run)

    # An installed agent: the task+agent image is built here, no base pulled.
    assert driver.stage_context(tmp_path, "t", tmp_path / "ctx", agent="mini-swe-agent") == driver.AGENT_IMAGE
    verbs = [c[1] for c in calls if c[0] == "podman"]
    assert verbs.count("build") == 2 and "pull" not in verbs
    dockerfile = (tmp_path / "ctx" / "seed" / "agent" / "Dockerfile").read_text()
    assert dockerfile.startswith("FROM docker.io/acme/foo:1")
    # The pair CA is consented into the agent image: the inference line
    # terminates at the appliance on a leaf minted from it.
    assert "COPY pair-ca.crt" in dockerfile and "REQUESTS_CA_BUNDLE=" in dockerfile
    assert (tmp_path / "ctx" / "seed" / "agent" / "pair-ca.crt").stat().st_size > 0
    assert driver.AGENT_IMAGE in (tmp_path / "ctx" / "seed" / "load.sh").read_text()

    # The oracle has no install: the base is pulled for an in-guest build.
    calls.clear()
    assert driver.stage_context(tmp_path, "t", tmp_path / "ctx2", agent="oracle") is None
    verbs = [c[1] for c in calls if c[0] == "podman"]
    assert "pull" in verbs and verbs.count("build") == 1
