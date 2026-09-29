"""Offline unit gate for the cella-runner bake driver (scripts/cella_runner_convert.py).

The driver is a script, not a package module, so it is loaded by path. These
cover its pure decisions -- the staged file set, the reflexive Dockerfile, the
run-on-boot script, the boot layer, and the inner-environment seam -- without
a podman build or a VM.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from titanium.environments.cella.boot_layer import (
    GuestFile,
    GuestSymlink,
    validate_boot_layer,
)

_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "cella_runner_convert.py"


def _load():
    spec = importlib.util.spec_from_file_location("cella_runner_convert", _DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: a dataclass under `from __future__ import
    # annotations` resolves its field types through sys.modules.
    sys.modules[spec.name] = module
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


def test_inner_env_lookup_by_name():
    assert driver.INNER_ENVS["docker"] is driver.DOCKER
    assert driver.INNER_ENVS["cella"] is driver.CELLA
    assert set(driver.INNER_ENVS) == {"docker", "cella"}


ENVS = pytest.mark.parametrize("env", [driver.DOCKER, driver.CELLA], ids=lambda e: e.name)


@ENVS
def test_reflexive_dockerfile_has_the_common_skeleton(env):
    text = driver.reflexive_dockerfile(env)
    assert f"FROM {env.base_image}\n" in text
    assert "uv sync" in text  # titanium's >=3.12 floor via uv
    assert f"COPY . {driver.GUEST_WORKSPACE}" in text


def test_docker_dockerfile_carries_the_daemon():
    assert "docker.io" in driver.reflexive_dockerfile(driver.DOCKER)


@ENVS
def test_run_script_names_the_task_and_ends_in_reset(env):
    task = f"examples/smoke/cella-runner-{env.name}"
    text = driver.run_script(task, env=env, agent="oracle")
    assert f"--env {env.name}" in text
    assert f"{driver.GUEST_WORKSPACE}/{task}" in text
    assert "reboot -f" in text  # the completion signal the host observes
    assert driver.RESULT_ROOT in text  # payload under the titanium result root
    assert "guest-dmesg.txt" in text  # the evidence a seccomp kill leaves


def test_docker_run_script_ignores_the_container_limits():
    text = driver.run_script("t", env=driver.DOCKER)
    assert "--cpus ignore --memory ignore" in text  # the VM is the resource boundary


def test_docker_stanzas_carry_no_cella_home():
    # docker's guest has no inner cella: nothing of its home or its user leaks in.
    assert "CELLA_HOME" not in driver.reflexive_dockerfile(driver.DOCKER)
    text = driver.run_script("t", env=driver.DOCKER)
    assert driver.CELLA_GUEST_HOME not in text and "runuser" not in text


def test_each_env_bases_on_its_own_image():
    assert "FROM debian:12\n" in driver.reflexive_dockerfile(driver.DOCKER)
    assert "FROM debian:13\n" in driver.reflexive_dockerfile(driver.CELLA)


def test_cella_dockerfile_carries_the_field_install_and_no_daemon():
    text = driver.reflexive_dockerfile(driver.CELLA)
    assert f"useradd -m -u {driver.CELLA_GUEST_UID} {driver.CELLA_GUEST_USER}" in text
    assert "/etc/subuid" in text and "uidmap" in text and "acl" in text  # the rootless jail's needs
    assert f"chmod 0711 /home/{driver.CELLA_GUEST_USER}" in text  # the sub-uid traverses the home
    assert f"{driver.CELLA_GUEST_HOME}/rootfs/terminator" in text  # the goldens linked into the user's home
    # install -d owns only what it is named: the parents must be named too.
    assert f"-g {driver.CELLA_GUEST_USER} {driver.CELLA_GUEST_HOME} " in text
    assert f"/home/{driver.CELLA_GUEST_USER}/.config /home/{driver.CELLA_GUEST_USER}/.config/containers" in text
    assert "UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python" in text  # the venv's interpreter, outside /root
    assert 'driver = "vfs"' in text  # no overlayfs in the cella kernel
    assert f"/home/{driver.CELLA_GUEST_USER}/.config/containers/storage.conf" in text  # rootless podman's own
    assert 'cgroup_manager = "cgroupfs"' in text  # no dbus in the guest
    assert 'cgroups = "disabled"' in text  # no CGROUP_BPF in the cella kernel
    assert "bubblewrap" in text and "podman" in text  # the jail, the ext4 builder
    assert f"COPY {driver.CELLA_SEED_SUBDIR}/bin/ /usr/local/bin/" in text
    assert "docker.io" not in text and "daemon.json" not in text


def test_cella_run_script_adopts_the_seeded_rootfs_and_runs_no_dockerd():
    text = driver.run_script("t", env=driver.CELLA, agent="oracle")
    assert "--env cella" in text and "--cpus ignore" in text
    assert f"runuser -u {driver.CELLA_GUEST_USER} -p -- env HOME=/home/{driver.CELLA_GUEST_USER} USER={driver.CELLA_GUEST_USER}" in text
    assert "chmod 0666 /dev/kvm" in text and f'chown -R {driver.CELLA_GUEST_USER}:{driver.CELLA_GUEST_USER} "$R/jobs"' in text
    assert "--memory ignore" not in text  # --mem-mb is the task's real limit
    assert "podman load" in text and "cella doctor gate" in text
    assert "TITANIUM_CELLA_ROOTFS_TAR=" in text and "TITANIUM_CELLA_IMAGE_CONFIG=" in text
    assert f"export CELLA_HOME={driver.CELLA_GUEST_HOME}" in text  # the oneshot has no HOME
    assert "dockerd" not in text and "masquerade" not in text
    assert "reboot -f" in text


def test_cella_seed_plan_names_no_container_images():
    assert driver.CELLA.seed_plan(Path("t")) == ([], False)


def test_world_hosts_is_the_inference_line_plus_the_task_policy(tmp_path):
    # The outer appliance grants exactly what the inner run needs: the
    # inference line, and the names the inner task's own policy releases.
    _write_task(tmp_path / "t", "[environment]\n")
    assert driver.world_hosts(tmp_path / "t") == driver.INFERENCE_HOSTS
    (tmp_path / "t" / "environment" / "cella.policy").write_text(
        "release outgoing deb.debian.org:80/tcp (keep_open=60m)\n"
        "release incoming deb.debian.org:80/tcp\n"
        "refuse outgoing other.example:443/tcp\n"
    )
    assert driver.world_hosts(tmp_path / "t") == sorted([*driver.INFERENCE_HOSTS, "deb.debian.org"])


def test_cella_run_script_names_the_inner_pair():
    text = driver.run_script("t", env=driver.CELLA, agent="mini-swe-agent")
    assert f"export TITANIUM_CELLA_PAIR={driver.CELLA_INNER_PAIR}" in text
    assert f"export TITANIUM_CELLA_UPSTREAM_DNS={driver.APPLIANCE_WIRE_ADDRESS}" in text
    assert f"export TITANIUM_CELLA_BOOT_MARGIN_SEC={driver.CELLA_INNER_BOOT_MARGIN_SEC}" in text
    assert f"export CELLA_EXTRACT_MIB_PER_SEC={driver.CELLA_INNER_EXTRACT_MIB_PER_SEC}" in text
    assert "--agent mini-swe-agent" in text  # no oracle pin


def test_cella_stage_seeds_the_install_the_builder_and_the_rootfs(tmp_path, monkeypatch):
    # The host side of the bake, with cella's home and every build stubbed:
    # what lands in the seed is the contract the run script reads.
    home = tmp_path / "home"
    (home / ".cella" / "bin").mkdir(parents=True)
    (home / ".cella" / "bin" / "cella").write_bytes(b"#!/bin/sh\n")
    (home / ".cella" / "bin" / "cella-engine").write_bytes(b"#!/bin/sh\n")
    for axis, flavor in driver.CELLA_GOLDENS:
        d = home / ".cella" / axis / flavor
        d.mkdir(parents=True)
        (d / "golden.json").write_text("{}")
    monkeypatch.setattr(driver.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(driver, "ensure_rootfs_builder_image", lambda: driver.ROOTFS_BUILDER_IMAGE)
    monkeypatch.setattr(driver.subprocess, "run", lambda argv, **kw: (Path(argv[3]).write_bytes(b"tar"), None)[1])
    nested = []
    monkeypatch.setattr(driver, "nest_terminator_golden", lambda golden_dir, outer_ca: nested.append((golden_dir, outer_ca)))
    contexts = []
    monkeypatch.setattr(driver, "prepare_build_context", lambda **kw: (contexts.append(kw), type("C", (), {"context_dir": kw["context_dir"], "build_file": kw["context_dir"] / "Dockerfile"})())[1])
    monkeypatch.setattr(driver, "build_image", lambda **kw: None)
    monkeypatch.setattr(driver, "inspect_image", lambda tag: {"Id": "sha256:abc", "Config": {"Env": ["A=1"], "WorkingDir": "/app"}})
    monkeypatch.setattr(driver, "export_rootfs_tar", lambda **kw: kw["dest_tar"].write_bytes(b"src"))
    monkeypatch.setattr(driver, "prepare_systemd_rootfs", lambda **kw: type("P", (), {"rootfs_tar": kw["source_rootfs_tar"]})())
    monkeypatch.setattr(driver, "untag_image", lambda tag: None)

    _write_task(tmp_path / "t", '[agent]\nuser = "titanium"\n[environment]\n')
    assert driver.CELLA.stage(tmp_path / "t", tmp_path / "ctx", "oracle") is None

    seed = tmp_path / "ctx" / driver.CELLA_SEED_SUBDIR
    assert sorted(p.name for p in (seed / "bin").iterdir()) == ["cella", "cella-engine"]
    for axis, flavor in driver.CELLA_GOLDENS:
        assert (seed / "home" / axis / flavor / "golden.json").is_file()
    assert (seed / driver.CELLA_BUILDER_TAR).read_bytes() == b"tar"
    assert sorted(p.name for p in (seed / "rootfs").iterdir()) == sorted(
        [driver.CELLA_ROOTFS_TAR, driver.CELLA_IMAGE_CONFIG]
    )
    assert json.loads((seed / "rootfs" / driver.CELLA_IMAGE_CONFIG).read_text())["WorkingDir"] == "/app"
    # The seeded terminator copy is nested against the host's outer CA.
    assert nested == [(seed / "home" / "rootfs" / "terminator", home / ".cella" / "rootfs" / "terminator" / "ca.pem")]
    assert contexts[-1]["agent_install_spec"] is None  # the oracle installs nothing

    # A model agent: its install rides into the inner rootfs, built here.
    shutil.rmtree(tmp_path / "ctx")
    assert driver.CELLA.stage(tmp_path / "t", tmp_path / "ctx", "mini-swe-agent") is None
    assert contexts[-1]["agent_install_spec"] is not None


@ENVS
def test_boot_layer_is_valid_and_enables_the_unit(env, tmp_path, monkeypatch):
    # The layer bakes the host's pair CA; the test brings its own so it
    # never depends on the host's goldens being present.
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"PAIR-CA\n")
    monkeypatch.setattr(driver, "pair_ca_path", lambda home: ca)
    layer = driver.boot_layer(f"examples/smoke/cella-runner-{env.name}", env=env, agent="oracle")
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
    text = driver.run_script("examples/smoke/cella-runner-docker", env=driver.DOCKER)
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


def test_docker_prebuilt_run_script_sets_image_source():
    # docker's knob alone: cella's prebuilt is the adopted rootfs tar.
    assert "TITANIUM_IMAGE_SOURCE=prebuilt" in driver.run_script("t", prebuilt=True, env=driver.DOCKER)
    assert "TITANIUM_IMAGE_SOURCE=prebuilt" not in driver.run_script("t", prebuilt=False, env=driver.DOCKER)


@ENVS
def test_stage_context_rejects_a_missing_task(tmp_path, env):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        driver.stage_context(tmp_path, "no/such/task", tmp_path / "ctx", env=env)


def test_run_script_points_titanium_at_the_seeded_proxy_image():
    text = driver.run_script("t", env=driver.DOCKER)
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
    assert driver.stage_context(tmp_path, "t", tmp_path / "ctx", agent="mini-swe-agent", env=driver.DOCKER) == driver.AGENT_IMAGE
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
    assert driver.stage_context(tmp_path, "t", tmp_path / "ctx2", agent="oracle", env=driver.DOCKER) is None
    verbs = [c[1] for c in calls if c[0] == "podman"]
    assert "pull" in verbs and verbs.count("build") == 1
