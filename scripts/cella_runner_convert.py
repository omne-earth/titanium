#!/usr/bin/env python3
"""Bake the workspace into a reflexive Cella rootfs for `cella-runner`.

`cella-runner` is the reflexive runner (docs/runners/CELLA-RUNNER.md): it bakes
the whole tracked workspace into one Cella micro-VM, boots it so a systemd
oneshot runs `titanium run --env <inner-env>` against a task *inside* the
sealed guest, and extracts the payload. An escape from the inner environment
lands in the guest, never on the host.

The runner is parametric over that inner environment (`InnerEnv`): the guest
skeleton -- base image, uv, the baked tree, the boot oneshot, the member
prelude, the phases, the reset -- is common, and each environment supplies
only its own part of it: `docker`, the container boundary, and
`cella`, cella hosting cella, which runs the oracle only (its stage hook
refuses a model agent by name; docs/runners/CELLA-RUNNER.md §5.2).

This driver is the bake half. It exists as Python rather than shell for the
same reason `scripts/smoke/cella_rootfs_convert.py` does:
`convert_task_to_rootfs_flavor` takes two of its decisions as injected
callables, so it cannot be invoked from a shell.

The two injected decisions here are real, not scaffolding:

    render_boot_layer        the run-on-boot oneshot that starts the inner
                             `titanium run` -- the whole point of the runner
    compute_flavor_identity  a per-run name, so one run's rootfs never
                             collides with another's

`plan_systemd_provisioning` is the package's own, unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from titanium.agents.factory import AgentFactory
from titanium.agents.installed.base import BaseInstalledAgent
from titanium.environments.agent_setup import (
    write_agent_dockerfile,
    write_egress_proxy_build_context,
)
from titanium.environments.cella.boot_layer import BootLayer, GuestFile, GuestSymlink
from titanium.environments.cella.constants import (
    ROOTFS_BUILDER_IMAGE,
    MEMBER_CA_PATH,
    REPLY_PORT_HIGH,
    REPLY_PORT_LOW,
    RUNNER_DIR,
    SYSTEM_CA_BUNDLE,
)
from titanium.environments.cella.buildfile import prepare_build_context
from titanium.environments.cella.converter import (
    BuildFacts,
    FlavorIdentity,
    convert_task_to_rootfs_flavor,
)
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    build_image,
    export_rootfs_tar,
    inspect_image,
    new_build_tag,
    untag_image,
)
from titanium.environments.cella.rootfs import ensure_rootfs_builder_image
from titanium.environments.cella.systemd_boot import (
    plan_systemd_provisioning,
    prepare_systemd_rootfs,
)
from titanium.models.agent.name import AgentName
from titanium.environments.cella.terminator import (
    appliance_border_policy_text,
    member_policy_text,
    member_prelude,
    member_trust_entries,
    pair_ca_path,
)

# The one world host the membrane grants: the agent's inference line. The
# runner measures the model's own capability in isolation, so nothing else
# crosses (docs/runners/CELLA-RUNNER.md §1). appliance_border_policy_text gives
# this grant its membrane memory (keep_open + skip_freeze) -- and only it.
INFERENCE_HOSTS = ["openrouter.ai"]


def write_policies(out_dir: Path) -> tuple[Path, Path]:
    """The pair's two borders, composed by the same helpers the cella
    environment uses: the member's fixed wire grants, and the appliance's
    world leg judged by name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    member = out_dir / "member.policy"
    appliance = out_dir / "appliance.policy"
    member.write_text(member_policy_text())
    appliance.write_text(appliance_border_policy_text(INFERENCE_HOSTS))
    return member, appliance

# Where the baked workspace lands in the guest, and where the inner titanium
# lives once `uv sync` has run against it. The result contract is titanium's
# own RUNNER_DIR, so the payload never masquerades as guest task state and
# `cella extract <name> /titanium` pulls exactly it.
GUEST_WORKSPACE = "/workspace"
RESULT_ROOT = f"{RUNNER_DIR}/result"
RUN_SCRIPT_PATH = f"{RUNNER_DIR}/cella-runner.sh"
RUN_UNIT_NAME = "cella-runner.service"
JOBS_SUBDIR = "jobs"

# The inner container's base images are seeded into the airgapped guest by the
# route the operator chose: `docker export` on the host, tar, bake, `docker
# import` in the guest (docs/runners/CELLA-RUNNER.md). titanium builds without
# --pull, so a base present in the guest's docker is used with no network. The
# seed tars and their loader ride the build context under this subdir.
SEED_SUBDIR = "seed"

# titanium's docker environment puts the agent behind a squid egress sidecar
# whenever the run has an inference allowlist and no open internet. That
# sidecar is built from alpine with `apk add`, which the guest cannot do: the
# membrane grants the inference line only. So the bake builds the sidecar on
# the host, seeds it, and the inner run uses it through
# TITANIUM_EGRESS_PROXY_IMAGE instead of building.
EGRESS_PROXY_IMAGE = "titanium-egress-proxy:cella-runner"

# The agent scaffold (mini-swe-agent and kin) is installed into the task image
# at build time: curl astral.sh, uv tool install, PyPI. None of that crosses
# the membrane, so for an agent with an install spec the bake builds the
# task+agent image on the host from titanium's own agent Dockerfile, seeds it,
# and the inner run uses it whole through TITANIUM_AGENT_IMAGE. The policy
# stays at the inference line only.
AGENT_IMAGE = "titanium-agent:cella-runner"


def inner_base_images(env_dir: Path) -> list[str]:
    """The base images the inner task's build file pulls, in first-seen order.

    Every ``FROM <image>`` that is not an earlier build stage and not
    ``scratch``. A stage alias (``FROM x AS build``) is recorded so a later
    ``FROM build`` is not mistaken for an image to seed.
    """
    build_file = env_dir / "Dockerfile"
    if not build_file.is_file():
        build_file = env_dir / "Containerfile"
    if not build_file.is_file():
        return []
    stages: set[str] = set()
    images: list[str] = []
    for line in build_file.read_text().splitlines():
        match = re.match(r"\s*FROM\s+(\S+)(?:\s+[Aa][Ss]\s+(\S+))?", line)
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if image != "scratch" and image not in stages and image not in images:
            images.append(image)
        if alias:
            stages.add(alias)
    return images


def agent_install_spec(agent: str, model: str | None = None):
    """The agent's build-time install steps, or None for an agent that has
    none (oracle, nop): those run against the task image as-is. Only an
    installed agent (`BaseInstalledAgent`) has a spec; it is the same class
    the inner titanium instantiates, so the baked image matches its
    fingerprint."""
    try:
        name = AgentName(agent)
    except ValueError:
        raise SystemExit(f"unknown agent {agent!r}") from None
    agent_class = AgentFactory._AGENT_MAP[name]
    if not issubclass(agent_class, BaseInstalledAgent):
        return None
    instance = agent_class(logs_dir=Path("/nonexistent"), model_name=model)
    return instance.install_spec()


def inner_seed_plan(task_dir: Path) -> tuple[list[str], bool]:
    """What to seed into the guest for this inner task, and whether the inner
    run uses the seeded image directly (prebuilt) or builds from it.

    The airgapped guest cannot build an image whose Dockerfile fetches over the
    network (apt, git clone). So when the task declares a prebuilt
    ``docker_image``, seed *that* runnable image and run the inner titanium in
    prebuilt mode -- the build happens on the host, where the network is, and
    the guest only imports and runs. A task with no ``docker_image`` is built
    in-guest from its seeded ``FROM`` base, which works only when its Dockerfile
    needs no network.

    Returns ``(images_to_seed, prebuilt)``.
    """
    config = tomllib.loads((task_dir / "task.toml").read_text())
    docker_image = (config.get("environment") or {}).get("docker_image")
    if docker_image:
        return [docker_image], True
    return inner_base_images(task_dir / "environment"), False


def _is_registry_qualified(image: str) -> bool:
    """True when *image* already names its registry, so it needs no docker.io.

    The reference is qualified when the part before the first ``/`` looks like a
    host: it contains a ``.`` or ``:`` or is ``localhost``. ``alexgshaw/fix-git``
    and ``python`` are not qualified; ``docker.io/x``, ``ghcr.io/x``,
    ``localhost/x`` are.
    """
    head = image.split("/", 1)[0]
    return "/" in image and ("." in head or ":" in head or head == "localhost")


def _export_image(engine: str, ref: str, tar: Path) -> list[str]:
    """Flatten *ref*'s filesystem into *tar* and return the ``--change``
    directives that restore its essential config (env, workdir, entrypoint,
    cmd) on ``docker import``, so the seeded image is faithful to the one a
    live pull or build would give."""
    cid = subprocess.run(
        [engine, "create", ref], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        with tar.open("wb") as handle:
            subprocess.run([engine, "export", cid], check=True, stdout=handle)
    finally:
        subprocess.run([engine, "rm", cid], check=False, capture_output=True)

    config = json.loads(
        subprocess.run(
            [engine, "inspect", ref], check=True, capture_output=True, text=True
        ).stdout
    )[0].get("Config") or {}
    changes: list[str] = []
    for entry in config.get("Env") or []:
        changes += ["--change", f"ENV {entry}"]
    if config.get("WorkingDir"):
        changes += ["--change", f"WORKDIR {config['WorkingDir']}"]
    if config.get("Entrypoint"):
        changes += ["--change", "ENTRYPOINT " + json.dumps(config["Entrypoint"])]
    if config.get("Cmd"):
        changes += ["--change", "CMD " + json.dumps(config["Cmd"])]
    return changes


def seed_images(
    images: list[str],
    seed_dir: Path,
    *,
    engine: str = "podman",
    proxy: bool = True,
    agent_context: Path | None = None,
) -> None:
    """Obtain each image on the host (pull the task's bases; build the
    egress-proxy sidecar; build the task+agent image from *agent_context*
    when given), export its filesystem, and write a guest-side loader that
    imports each one back under its original name."""
    seed_dir.mkdir(parents=True, exist_ok=True)
    load_lines = ["#!/bin/bash", "set -uo pipefail"]

    def seed(image: str, host_ref: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", image)
        tar = seed_dir / f"{safe}.tar"
        changes = _export_image(engine, host_ref, tar)
        guest_tar = f"{GUEST_WORKSPACE}/{SEED_SUBDIR}/{safe}.tar"
        cmd = ["docker", "import", *changes, guest_tar, image]
        load_lines.append(" ".join(shlex.quote(part) for part in cmd))

    for image in images:
        # podman refuses an unqualified short name without a TTY to prompt on,
        # so pull/create/inspect with an explicit docker.io. The guest import
        # keeps the original name (docker resolves it the same), so titanium's
        # own reference to the image matches.
        pull_ref = image if _is_registry_qualified(image) else f"docker.io/{image}"
        subprocess.run([engine, "pull", pull_ref], check=True)
        seed(image, pull_ref)

    if proxy:
        # The sidecar, built here from titanium's own build context so the
        # seeded image is exactly what the inner titanium would have built.
        context = write_egress_proxy_build_context(seed_dir / "egress-proxy")
        host_ref = f"localhost/{EGRESS_PROXY_IMAGE}"
        subprocess.run([engine, "build", "-t", host_ref, str(context)], check=True)
        seed(EGRESS_PROXY_IMAGE, host_ref)

    if agent_context is not None:
        host_ref = f"localhost/{AGENT_IMAGE}"
        subprocess.run([engine, "build", "-t", host_ref, str(agent_context)], check=True)
        seed(AGENT_IMAGE, host_ref)

    (seed_dir / "load.sh").write_text("\n".join(load_lines) + "\n")


def write_agent_build_context(task_dir: Path, install, context: Path) -> Path:
    """The task+agent build context, exactly as the inner titanium would
    write it (`write_agent_dockerfile`): FROM the task's prebuilt image, or
    the task's own Dockerfile, then the agent's install steps."""
    config = tomllib.loads((task_dir / "task.toml").read_text())
    env_cfg = config.get("environment") or {}
    docker_image = env_cfg.get("docker_image")
    if context.exists():
        shutil.rmtree(context)
    if docker_image:
        context.mkdir(parents=True)
    else:
        shutil.copytree(task_dir / "environment", context)
    dockerfile = write_agent_dockerfile(
        build_dir=context,
        source_environment_dir=context,
        prebuilt_image_name=docker_image,
        install=install,
        user=(config.get("agent") or {}).get("user"),
    )
    # The agent's inference line ends at the terminator appliance, which
    # terminates TLS on a leaf minted from the pair CA. The member prelude
    # trusts that CA in the guest, but the agent runs in a container with its
    # own trust store, so the CA is consented here, at build, the same way:
    # into the system bundle, with the Python clients pointed at it (they
    # verify against certifi's bundle otherwise). Image ENV reaches every
    # exec'd process, which is where the inference client runs.
    (context / "pair-ca.crt").write_bytes(pair_ca_path(Path.home()).read_bytes())
    dockerfile.write_text(
        dockerfile.read_text()
        + f"""
USER root
COPY pair-ca.crt {MEMBER_CA_PATH}
RUN mkdir -p {Path(SYSTEM_CA_BUNDLE).parent} && cat {MEMBER_CA_PATH} >> {SYSTEM_CA_BUNDLE}
ENV SSL_CERT_FILE={SYSTEM_CA_BUNDLE} REQUESTS_CA_BUNDLE={SYSTEM_CA_BUNDLE} CURL_CA_BUNDLE={SYSTEM_CA_BUNDLE}
"""
    )
    return context


def tracked_files(workspace: Path) -> list[str]:
    """The working tree minus the gitignored files, as repo-relative paths.

    Tracked files and untracked files that git does not ignore, both. This is
    the reflexive content: what the runner bakes is the tree the operator sees,
    not only what is committed. The `.git` directory is never a member.
    """
    out = subprocess.run(
        ["git", "-C", str(workspace), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    names = [name for name in out.stdout.decode().split("\0") if name]
    return sorted(name for name in names if not name.startswith(".git/"))


def _docker_dockerfile_stanza() -> str:
    """docker's part of the reflexive image: the compose plugin, the daemon
    config, and the masks that keep the distro from starting a second daemon.
    """
    return f"""\
# The Compose v2 plugin. Debian's docker.io ships the docker CLI but not the
# `docker compose` plugin, and titanium's docker environment drives builds with
# `docker compose build`. Fetched here (the bake has egress; the VM will not)
# and dropped at docker's system cli-plugins path, pinned by version.
RUN mkdir -p /usr/local/lib/docker/cli-plugins \\
 && curl -SL \\
      https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 \\
      -o /usr/local/lib/docker/cli-plugins/docker-compose \\
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# vfs storage: simple and portable, no overlay dependency. Networking keeps
# docker's bridge and iptables: titanium's egress sidecar puts the agent on an
# internal network and reaches the world from a compose bridge. Docker
# masquerades each such network with a rule of its own that preserves the
# container's source port -- outside the reply window the appliance grants --
# and (at 20.10) offers no daemon-wide switch for it. So the run script owns
# source NAT in an nftables chain that runs ahead of docker's (see run_script).
RUN mkdir -p /etc/docker \\
 && printf '%s\\n' '{{' \\
      '  "storage-driver": "vfs"' \\
      '}}' > /etc/docker/daemon.json

# The run script owns the docker daemon's lifecycle (see the boot layer), so
# the distro's own units must not also start it. Disabling is not enough --
# docker.socket still socket-activates docker.service on the first CLI call,
# and the two dockerds race on /var/run/docker.pid. Mask both so only the
# script's daemon runs; the docker CLI connects to the socket it creates.
# Spelled as the symlinks `systemctl mask` would write: systemd is not yet in
# this image (the converter provisions it after this build), so systemctl
# is not here to call.
RUN ln -sf /dev/null /etc/systemd/system/docker.service \\
 && ln -sf /dev/null /etc/systemd/system/docker.socket
"""


def _docker_run_prep() -> str:
    """docker's part of the run script: the reply-window masquerade, the
    daemon, and the seeded images. The masquerade exists only because a
    docker container has its own network namespace, outside the sysctl the
    member prelude pinned for the guest.
    """
    return f"""\
# The daemon the inner `--env docker` run drives. This script owns its
# lifecycle rather than a systemd `Requires=`, so its log is always captured
# under the result root and a failure is visible in the extracted payload
# instead of vanishing into a dependency-failed unit. Clear any stale pid a
# prior boot left, then start it. Retry once: a first-boot flake (e.g. a
# boltdb open racing a slow tmpfs mount) should not sink the whole run.
# Container egress leaves the guest through the wire nic. Docker masquerades
# every compose network with its own iptables-nft rule (priority srcnat), which
# keeps the container's source port -- outside the reply window the appliance
# grants. A connection is source-NATed once, by the first nat chain that binds
# it, so this chain at priority srcnat-1 wins: every flow out of the wire nic
# is masqueraded into the reply window the member prelude pinned for the guest
# itself, and the appliance's answers are named by a granted port.
echo 1 > /proc/sys/net/ipv4/ip_forward
nft -f - <<NFT || echo "could not install the reply-window masquerade" >> "$R/dockerd.log"
table ip cellarun {{
  chain post {{
    type nat hook postrouting priority srcnat - 1; policy accept;
    oifname "eth0" ip protocol tcp masquerade to :{REPLY_PORT_LOW}-{REPLY_PORT_HIGH}
    oifname "eth0" ip protocol udp masquerade to :{REPLY_PORT_LOW}-{REPLY_PORT_HIGH}
  }}
}}
NFT
phase "masquerade: $(nft list chain ip cellarun post 2>/dev/null | grep -c masquerade) rules"
start_dockerd() {{
  rm -f /var/run/docker.pid
  dockerd >> "$R/dockerd.log" 2>&1 &
  DPID=$!
  for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && return 0
    kill -0 "$DPID" 2>/dev/null || return 1
    sleep 1
  done
  return 1
}}
if ! start_dockerd; then
  echo "dockerd did not come up; retrying once" >> "$R/dockerd.log"
  pkill dockerd 2>/dev/null; sleep 2; rm -f /var/run/docker.pid
  start_dockerd || true
fi
docker info > "$R/docker-info.txt" 2>&1 \\
  && phase "dockerd ready" \\
  || {{ echo "dockerd never became ready" >> "$R/dockerd.log"; phase "dockerd NOT ready"; }}

# Import the baked base images (docker export -> tar -> docker import), so the
# inner build finds them locally and needs no pull. Airgapped by design.
if [ -f {GUEST_WORKSPACE}/{SEED_SUBDIR}/load.sh ]; then
  phase "seed: importing images"
  bash {GUEST_WORKSPACE}/{SEED_SUBDIR}/load.sh > "$R/seed.log" 2>&1
  phase "seed: done ($(docker images -q | wc -l) images)"
fi
"""


def _docker_run_env_exports(prebuilt: bool, agent_image: str | None) -> str:
    """docker's part of the inner run's environment: the seeded sidecar, the
    seeded task+agent image, and prebuilt mode for a host-built image."""
    return f"""\
# The egress sidecar was built on the host and seeded (load.sh above).
export TITANIUM_EGRESS_PROXY_IMAGE={EGRESS_PROXY_IMAGE}
{f'# The task+agent image, built on the host and seeded: the inner run builds nothing.' if agent_image else '# No agent install: the inner run builds from the seeded base.'}
{f'export TITANIUM_AGENT_IMAGE={agent_image}' if agent_image else ''}
# Use the seeded image as-is when it was prebuilt on the host: the guest is
# airgapped and cannot run the Dockerfile's network build steps (apt, clone).
{'export TITANIUM_IMAGE_SOURCE=prebuilt' if prebuilt else '# built in-guest from the seeded base'}
"""


def _docker_stage(task_dir: Path, context: Path, agent: str) -> str | None:
    """docker's part of the staged context: the seed. An agent with install
    steps gets the whole task+agent image built here and seeded, and the
    inner run then builds nothing; otherwise the task's own base (or prebuilt
    image) is seeded for an in-guest build. Returns the agent image name the
    inner run must use, or None."""
    install = agent_install_spec(agent)
    if install is not None:
        agent_context = write_agent_build_context(
            task_dir, install, context / SEED_SUBDIR / "agent"
        )
        seed_images([], context / SEED_SUBDIR, agent_context=agent_context)
        return AGENT_IMAGE
    images, _prebuilt = inner_seed_plan(task_dir)
    seed_images(images, context / SEED_SUBDIR)
    return None


# The cella inner environment: a KVM VM inside the KVM guest. The guest
# carries the field cella install (the persona binaries are dynamic against
# glibc 2.34, and debian 12 is 2.36), bwrap for the jail, podman for the one
# thing the inner environment builds per machine -- its ext4, `mkfs.ext4 -d`
# in the builder container -- and the goldens. Everything that needs a
# network is done here on the host and seeded (docs/runners/CELLA-RUNNER.md
# §5.2): the builder image, and the task's systemd-provisioned rootfs tar.
CELLA_SEED_SUBDIR = f"{SEED_SUBDIR}/cella"
CELLA_GUEST_SEED = f"{GUEST_WORKSPACE}/{CELLA_SEED_SUBDIR}"
# The inner titanium runs as this user, not root: a failed inner boundary
# (the thing the cella leg exists to test) lands as an unprivileged user in
# this guest, the same shape as titanium-run one layer in. cella's jail is
# rootless by nature (bwrap, a sub-uid range, newuidmap), and so is podman.
CELLA_GUEST_USER = "titanium"
CELLA_GUEST_UID = 1000
CELLA_GUEST_HOME = f"/home/{CELLA_GUEST_USER}/.cella"
CELLA_GUEST_RUNTIME_DIR = f"/run/user/{CELLA_GUEST_UID}"
# How the root oneshot runs a command as that user: the environment kept
# (the exports above it, the baked secrets), and the identity variables
# rewritten -- rootless podman reads USER to find its sub-id range, and a
# preserved USER=root sends it to root's.
CELLA_AS_USER = (
    f"runuser -u {CELLA_GUEST_USER} -p -- env HOME=/home/{CELLA_GUEST_USER} "
    f"USER={CELLA_GUEST_USER} LOGNAME={CELLA_GUEST_USER} "
    f"XDG_RUNTIME_DIR={CELLA_GUEST_RUNTIME_DIR}"
)
CELLA_ROOTFS_TAR = "state-0000.tar"
CELLA_IMAGE_CONFIG = "image-config.json"
CELLA_BUILDER_TAR = "builder.tar"
CELLA_GOLDENS = (("kernel", "canonical"), ("rootfs", "cella"), ("rootfs", "terminator"))
# cella's terminator verifies the world against webpki's roots only. An inner
# appliance dialing through the outer appliance would meet a leaf minted from
# the outer pair CA and refuse it, so no inference line can chain through two
# membranes until the terminator takes an extra root. The oracle needs none.
CELLA_ORACLE_ONLY = (
    "inner environment 'cella' runs the oracle only: cella's terminator "
    "trusts webpki's roots alone, so an inner appliance cannot chain through "
    "the outer membrane (docs/runners/CELLA-RUNNER.md §5.2)"
)


def _cella_dockerfile_stanza() -> str:
    return f"""\
# The field cella install, copied from the host: the persona set on the path.
COPY {CELLA_SEED_SUBDIR}/bin/ /usr/local/bin/
# This base ships no python, so uv fetches an interpreter for the venv. Its
# default home is /root's, which the unprivileged user cannot traverse; put
# it where every user reads it.
ENV UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python
# The unprivileged user the inner titanium runs as, with the sub-id range
# cella's jail and rootless podman map their namespaces from.
RUN useradd -m -u {CELLA_GUEST_UID} {CELLA_GUEST_USER} \\
 && echo '{CELLA_GUEST_USER}:100000:65536' > /etc/subuid \\
 && echo '{CELLA_GUEST_USER}:100000:65536' > /etc/subgid \\
 && chmod 0711 /home/{CELLA_GUEST_USER}
# The last line: each machine's VMM runs as its own sub-uid and must
# traverse the home to reach the machine dir; a 0700 home refuses it before
# cella's own ACLs on .cella and machines are reached. The host grants this
# with setfacl (scripts/init/cella.sh); here a mode, because the bake's
# export-and-mkfs path keeps modes and drops ACL xattrs. Execute-only opens
# no read on the home.
# The user's CELLA_HOME: its own kernel, rootfs and machines directories (the
# inner titanium publishes flavors and creates machines there), with the
# seeded goldens linked in by name. The seed is root-owned and read-only; one
# copy in the image, and the machine's sub-uid reads it through the link.
RUN install -d -o {CELLA_GUEST_USER} -g {CELLA_GUEST_USER} {CELLA_GUEST_HOME} \\
      {CELLA_GUEST_HOME}/kernel {CELLA_GUEST_HOME}/rootfs {CELLA_GUEST_HOME}/machines \\
 && ln -s {CELLA_GUEST_SEED}/home/kernel/canonical {CELLA_GUEST_HOME}/kernel/canonical \\
 && ln -s {CELLA_GUEST_SEED}/home/rootfs/cella {CELLA_GUEST_HOME}/rootfs/cella \\
 && ln -s {CELLA_GUEST_SEED}/home/rootfs/terminator {CELLA_GUEST_HOME}/rootfs/terminator \\
 && chown -h {CELLA_GUEST_USER}:{CELLA_GUEST_USER} {CELLA_GUEST_HOME}/kernel/canonical \\
      {CELLA_GUEST_HOME}/rootfs/cella {CELLA_GUEST_HOME}/rootfs/terminator
# podman runs one thing here, the ext4 builder, rootless, and its default
# overlay driver needs overlayfs, which this guest's kernel does not carry.
# vfs copies the one alpine layer and asks the kernel for nothing. The
# user's own storage.conf, so podman keeps its rootless roots.
RUN install -d -o {CELLA_GUEST_USER} -g {CELLA_GUEST_USER} \\
      /home/{CELLA_GUEST_USER}/.config /home/{CELLA_GUEST_USER}/.config/containers \\
 && printf '%s\\n' '[storage]' 'driver = "vfs"' \\
      > /home/{CELLA_GUEST_USER}/.config/containers/storage.conf \\
 && chown {CELLA_GUEST_USER}:{CELLA_GUEST_USER} /home/{CELLA_GUEST_USER}/.config/containers/storage.conf
# That one container is the trusted ext4 builder, so it gets no cgroup: crun
# would otherwise install a BPF device program, which needs CONFIG_CGROUP_BPF,
# a docker config this kernel does not carry. cgroupfs asks no D-Bus of a
# guest that runs no dbus daemon.
RUN printf '%s\\n' '[containers]' 'cgroups = "disabled"' \\
      '[engine]' 'cgroup_manager = "cgroupfs"' 'events_logger = "file"' \\
      > /etc/containers/containers.conf
"""


def _cella_run_prep() -> str:
    return f"""\
# What the unprivileged user needs from root, once: the KVM device open to
# every uid -- each machine's VMM opens it itself, jailed as its own sub-uid,
# which is in no group; 0666 is what udev sets on the host -- a runtime dir
# (no logind session grants one), and the jobs dir it writes.
chmod 0666 /dev/kvm
install -d -m 0700 -o {CELLA_GUEST_USER} -g {CELLA_GUEST_USER} {CELLA_GUEST_RUNTIME_DIR}
chown -R {CELLA_GUEST_USER}:{CELLA_GUEST_USER} "$R/{JOBS_SUBDIR}"
# The ext4 builder the inner environment runs `mkfs.ext4 -d` in, seeded by
# tag into the user's store so the inner run finds it present and builds
# nothing.
phase "seed: loading the rootfs builder"
{CELLA_AS_USER} podman load -i {CELLA_GUEST_SEED}/{CELLA_BUILDER_TAR} > "$R/seed.log" 2>&1 \\
  || phase "seed: podman load FAILED"
# cella's own preflight, one level down and as that user: KVM in this guest,
# the jail, the goldens. Recorded, not fatal here: the inner titanium refuses
# on its own terms, and the record says why.
{{ ls -la /dev/kvm; {CELLA_AS_USER} CELLA_HOME={CELLA_GUEST_HOME} cella doctor gate kvm bwrap golden:kernel:canonical golden:rootfs:cella golden:rootfs:terminator; }} \\
  > "$R/cella-doctor.txt" 2>&1 && phase "cella doctor gate: ok" || phase "cella doctor gate: FAILED"
"""


def _cella_run_env_exports(prebuilt: bool, agent_image: str | None) -> str:
    return f"""\
# The boot oneshot runs with no HOME, and cella resolves its home as
# CELLA_HOME, else HOME/.cella, else ./.cella -- so name it.
export CELLA_HOME={CELLA_GUEST_HOME}
# The task's rootfs, provisioned to boot systemd on the host and seeded: the
# inner run adopts it and builds no base.
export TITANIUM_CELLA_ROOTFS_TAR={CELLA_GUEST_SEED}/rootfs/{CELLA_ROOTFS_TAR}
export TITANIUM_CELLA_IMAGE_CONFIG={CELLA_GUEST_SEED}/rootfs/{CELLA_IMAGE_CONFIG}
"""


def _cella_seed_plan(task_dir: Path) -> tuple[list[str], bool]:
    return [], False


def _cella_stage(task_dir: Path, context: Path, agent: str) -> str | None:
    """cella's part of the staged context: the field install, the goldens,
    the builder image, and the task's provisioned rootfs tar with the image
    config the inner environment reads beside it."""
    if agent != AgentName.ORACLE.value:
        raise SystemExit(CELLA_ORACLE_ONLY)
    seed = context / CELLA_SEED_SUBDIR
    home = Path.home() / ".cella"
    bins = seed / "bin"
    bins.mkdir(parents=True, exist_ok=True)
    for binary in sorted((home / "bin").glob("cella*")):
        shutil.copy2(binary, bins / binary.name)
    for axis, flavor in CELLA_GOLDENS:
        shutil.copytree(home / axis / flavor, seed / "home" / axis / flavor)

    ensure_rootfs_builder_image()
    subprocess.run(
        ["podman", "save", "-o", str(seed / CELLA_BUILDER_TAR), ROOTFS_BUILDER_IMAGE],
        check=True,
    )

    # The same sequence the inner environment's start runs, here where the
    # package index is reachable.
    config = tomllib.loads((task_dir / "task.toml").read_text())
    work = seed / "rootfs"
    work.mkdir(parents=True)
    tag = new_build_tag("titanium-cella-runner")
    try:
        prepared_context = prepare_build_context(
            environment_dir=task_dir / "environment",
            context_dir=work / "context",
            agent_install_spec=None,
            agent_user=(config.get("agent") or {}).get("user"),
        )
        build_image(
            context_dir=prepared_context.context_dir,
            build_file=prepared_context.build_file,
            tag=tag,
        )
        record = parse_image_record(inspect_image(tag))
        source_tar = work / "rootfs-source.tar"
        export_rootfs_tar(image=tag, dest_tar=source_tar)
        prepared = prepare_systemd_rootfs(
            source_tag=tag,
            source_image_id=record.image_id,
            source_rootfs_tar=source_tar,
            work_dir=work,
            plan_provisioning=plan_systemd_provisioning,
        )
        if prepared.rootfs_tar != work / CELLA_ROOTFS_TAR:
            shutil.copyfile(prepared.rootfs_tar, work / CELLA_ROOTFS_TAR)
        (work / CELLA_IMAGE_CONFIG).write_text(json.dumps(dict(record.config)))
    finally:
        untag_image(tag)
    # Only the two files the inner run reads ride the bake.
    for stale in work.iterdir():
        if stale.name not in (CELLA_ROOTFS_TAR, CELLA_IMAGE_CONFIG):
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
    return None


@dataclass(frozen=True)
class InnerEnv:
    """One inner environment the guest can host. Each hook is that
    environment's own part of the common skeleton; an environment not yet
    onboarded has every hook refuse by name, so the runner never bakes a
    guest it cannot run."""

    name: str
    base_image: str
    guest_mem_mb: int
    apt_packages: tuple[str, ...]
    dockerfile_stanza: Callable[[], str]
    run_prep: Callable[[], str]
    run_env_exports: Callable[[bool, str | None], str]
    titanium_flags: Callable[[], list[str]]
    # The command prefix that runs the inner titanium as the guest's
    # unprivileged user, or "" for an environment whose run is root.
    run_as: str
    seed_plan: Callable[[Path], tuple[list[str], bool]]
    stage: Callable[[Path, Path, str], str | None]


DOCKER = InnerEnv(
    name="docker",
    base_image="debian:12",
    guest_mem_mb=4096,
    apt_packages=("docker.io", "nftables"),
    dockerfile_stanza=_docker_dockerfile_stanza,
    run_prep=_docker_run_prep,
    run_env_exports=_docker_run_env_exports,
    # The cella VM is the resource boundary (one vCPU, a real --mem-mb
    # ceiling), so the inner container enforces neither cpu nor memory.
    titanium_flags=lambda: ["--env", "docker", "--cpus", "ignore", "--memory", "ignore"],
    # Root: driving dockerd needs the docker group, which is root-equivalent
    # (the reason titanium-run never wraps the docker family either).
    run_as="",
    seed_plan=inner_seed_plan,
    stage=_docker_stage,
)

CELLA = InnerEnv(
    name="cella",
    # The field cella personas are dynamic against glibc; cella-machine and
    # cella-doctor need 2.39, above debian 12's 2.36.
    base_image="debian:13",
    # The inner machines' --mem-mb (the task's, then the verifier's twin) and
    # the builder container all come out of this ceiling.
    guest_mem_mb=6144,
    # uidmap: the setuid newuidmap/newgidmap the rootless jail and podman
    # map with; acl: cella's traversal grants on the home for the sub-uid.
    apt_packages=("bubblewrap", "podman", "uidmap", "acl"),
    dockerfile_stanza=_cella_dockerfile_stanza,
    run_prep=_cella_run_prep,
    run_env_exports=_cella_run_env_exports,
    # One vCPU is cella's own rule; --mem-mb is the task's real limit.
    titanium_flags=lambda: ["--env", "cella", "--cpus", "ignore"],
    run_as=CELLA_AS_USER,
    seed_plan=_cella_seed_plan,
    stage=_cella_stage,
)

INNER_ENVS = {e.name: e for e in (DOCKER, CELLA)}


def reflexive_dockerfile(env: InnerEnv) -> str:
    """The build file for the reflexive image.

    `uv` so titanium's own Python floor (>=3.12) is met without depending on
    the base distro's interpreter; `uv sync` to provision titanium from the
    baked tree; between them, the inner environment's own layers.
    """
    packages = " ".join(("ca-certificates", "curl", "git", "iproute2", *env.apt_packages))
    return f"""\
# Generated by scripts/cella_runner_convert.py for `cella-runner`. Do not edit;
# regenerate. The reflexive image: the inner environment ({env.name}) for the
# inner run to drive, uv for titanium's Python floor, and titanium provisioned
# from the baked tree.
FROM {env.base_image}

RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      {packages} \\
 && rm -rf /var/lib/apt/lists/*

# uv, pinned by the installer's own checksum, placed on PATH for every stage.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
ENV PATH=/usr/local/bin:$PATH

{env.dockerfile_stanza()}
WORKDIR {GUEST_WORKSPACE}
# Deps before source: copy only the lock files and sync the dependencies, so
# this heavy layer (the whole dep download) is cached across source changes.
# Only the fast project-install re-runs when the tree changes.
COPY pyproject.toml uv.lock {GUEST_WORKSPACE}/
RUN uv sync --frozen --no-dev --no-install-project

COPY . {GUEST_WORKSPACE}
RUN uv sync --frozen --no-dev
"""


def run_script(
    inner_task: str,
    *,
    prebuilt: bool = False,
    agent: str = "mini-swe-agent",
    agent_image: str | None = None,
    jobs_subdir: str = JOBS_SUBDIR,
    env: InnerEnv,
) -> str:
    """The guest-side script the boot oneshot runs.

    Re-entry guard first: a reset that re-booted instead of exiting the VMM
    sees the done marker and resets again, exactly as the trial orchestrator
    does. Then the inner titanium run, its whole output under the result root.
    The forced reset is the completion signal the host observes.
    """
    flags = " ".join(shlex.quote(part) for part in env.titanium_flags())
    return f"""\
#!/bin/bash
# Generated by scripts/cella_runner_convert.py. The reflexive inner run.
set -uo pipefail
R={RESULT_ROOT}
end() {{ sync; reboot -f; echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger; }}
if [ -f "$R/done" ]; then end; fi
mkdir -p "$R/{jobs_subdir}"
# Phase markers, to the result root and to the console: on a lab cella the
# console is recorded, so a stall is placed without extracting first. On the
# field cella the console goes nowhere, at no cost.
phase() {{ echo "cella-runner: $(date -u +%T) $*" | tee -a "$R/phases.log" > /dev/console 2>/dev/null || true; }}
phase "boot: run script started"

# Member prelude: address the wire to the terminator, fold the pair CA into
# the system bundle, point Python TLS at it, pin the reply-port window.
{member_prelude("eth0")}
# Guest diagnostics, so an inner-environment failure is debuggable from the
# extracted payload rather than by re-running blind.
{{ uname -a; echo; date -u; echo; mount; echo; cat /proc/filesystems; echo;
   ls -la /sys/fs/cgroup/cgroup.controllers 2>/dev/null && cat /sys/fs/cgroup/cgroup.controllers;
   echo; dmesg | tail -40; }} > "$R/guest-diag.txt" 2>&1

{env.run_prep()}
cd {GUEST_WORKSPACE}
{env.run_env_exports(prebuilt, agent_image)}
# The model agent runs the real escape test -- a live agent attempting the
# breakout, not a canned solution. Its API key and model come from the baked
# .secrets (Makefile-style `export VAR = value`; normalize to shell env). The
# agent reaches its model through the guest's judged membrane.
if [ -f {GUEST_WORKSPACE}/.secrets ]; then
  set -a
  eval "$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$/\\2=\\3/p' {GUEST_WORKSPACE}/.secrets)"
  set +a
fi

# litellm fetches its model cost map from GitHub at import; the membrane
# refuses that, and a refused crossing is a dropped packet, so each attempt
# waits out a timeout. Use the bundled map, as the agent image already does.
export LITELLM_LOCAL_MODEL_COST_MAP=true

phase "titanium run: start"
{env.run_as} .venv/bin/titanium run \\
  --agent {shlex.quote(agent)} \\
  --model "${{OPENROUTER_MODEL:?no OPENROUTER_MODEL in baked .secrets}}" \\
  {flags} \\
  --path "{GUEST_WORKSPACE}/{inner_task}" \\
  --jobs-dir "$R/{jobs_subdir}" \\
  2>&1 | tee /dev/console > "$R/run.log"
echo "${{PIPESTATUS[0]}}" > "$R/exit-code"
phase "titanium run: exit $(cat "$R/exit-code")"
# The kernel log after the run: a seccomp kill inside the guest is only
# ever named here (audit type=1326, syscall=N).
dmesg > "$R/guest-dmesg.txt" 2>&1
touch "$R/done"
end
"""


def boot_layer(
    inner_task: str,
    *,
    prebuilt: bool = False,
    agent: str = "mini-swe-agent",
    agent_image: str | None = None,
    env: InnerEnv,
) -> BootLayer:
    """The run-on-boot layer: the oneshot unit, the run script, the symlink
    that enables the unit. Modeled on the trial orchestrator's boot entries
    (`environment.py` `_orchestrator_files`), one job simpler: it runs a whole
    inner titanium rather than a phase machine.

    The unit does not `Requires=docker.service`: the run script owns the
    daemon's lifecycle and log (see `run_script`), so a daemon that fails to
    start is captured under the result root rather than failing the unit's
    dependency and running nothing.
    """
    unit = f"""\
[Unit]
Description=Titanium reflexive inner run (cella-runner)
After=basic.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash {RUN_SCRIPT_PATH}
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
"""
    # The guest is a member of a terminated pair: it reaches the world only
    # through the terminator appliance, which resolves and judges by name.
    # Bake the pair CA and the appliance resolver, as every member does.
    ca_pem = pair_ca_path(Path.home()).read_bytes()
    return BootLayer(
        entries=tuple(member_trust_entries(ca_pem)) + (
            GuestFile(
                path=RUN_SCRIPT_PATH,
                contents=run_script(
                    inner_task, prebuilt=prebuilt, agent=agent, agent_image=agent_image, env=env
                ).encode(),
                mode=0o700,
                uid=0,
                gid=0,
            ),
            GuestFile(
                path=f"/etc/systemd/system/{RUN_UNIT_NAME}",
                contents=unit.encode(),
                mode=0o600,
                uid=0,
                gid=0,
            ),
            GuestSymlink(
                path=f"/etc/systemd/system/multi-user.target.wants/{RUN_UNIT_NAME}",
                target=f"../{RUN_UNIT_NAME}",
                uid=0,
                gid=0,
            ),
        )
    )


def stage_context(
    workspace: Path,
    inner_task: str,
    context: Path,
    *,
    agent: str = "mini-swe-agent",
    env: InnerEnv,
) -> str | None:
    """Copy the tracked tree into *context*, write the reflexive Dockerfile,
    and let the inner environment stage its own seed.

    Fails loudly when the inner task is not in the baked tree: a run against a
    task the guest will not carry is a mistake to catch on the host, not a
    boot that finds nothing.
    """
    files = tracked_files(workspace)
    if not (workspace / inner_task / "task.toml").is_file():
        raise SystemExit(
            f"no task at {inner_task!r}: expected {inner_task}/task.toml in the workspace"
        )
    context.mkdir(parents=True, exist_ok=True)
    for name in files:
        src = workspace / name
        if not src.is_file():
            continue
        dst = context / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())

    # .secrets is gitignored, so `ls-files` skips it -- but the model agent
    # needs its key and model, and baking it into the guest leaks nothing: it
    # is already on this host, in this project. Force-include it.
    secrets = workspace / ".secrets"
    if secrets.is_file():
        (context / ".secrets").write_bytes(secrets.read_bytes())

    (context / "Dockerfile").write_text(reflexive_dockerfile(env))

    # The guest is airgapped, so whatever the inner run needs is baked under
    # the context here and loaded at boot before the inner run.
    return env.stage(workspace / inner_task, context, agent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path,
                        help="the repo root to bake (its tracked tree)")
    parser.add_argument("--inner-task", required=True,
                        help="repo-relative path of the task to run in the guest")
    parser.add_argument("--context", required=True, type=Path,
                        help="where to stage the build context")
    parser.add_argument("--home", required=True, type=Path,
                        help="the CELLA_HOME to publish the flavor into")
    parser.add_argument("--flavor", required=True, help="the flavor name")
    parser.add_argument("--size-bytes", required=True, type=int)
    parser.add_argument("--build-timeout-sec", type=float, default=3600.0)
    parser.add_argument("--policy-dir", type=Path,
                        help="where to write member.policy and appliance.policy")
    parser.add_argument("--agent", default="mini-swe-agent",
                        help="the inner agent; its install is baked on the host")
    parser.add_argument("--inner-env", choices=sorted(INNER_ENVS), required=True,
                        help="the environment the inner run drives in the guest")
    args = parser.parse_args()
    env = INNER_ENVS[args.inner_env]

    if args.policy_dir:
        member, appliance = write_policies(args.policy_dir)
        print(f"[cella-runner] policies : {member} {appliance}")

    # The seed plan first, so an environment that cannot host this task
    # refuses before any tree is copied.
    _images, prebuilt = env.seed_plan(args.workspace / args.inner_task)
    agent_image = stage_context(
        args.workspace, args.inner_task, args.context, agent=args.agent, env=env
    )

    def identity(facts: BuildFacts) -> FlavorIdentity:
        # A per-run name. The runner's rootfs is disposable and never shared,
        # so the cache key is the run's own flavor -- nothing to pin across
        # runs. The build facts are recorded in the manifest regardless.
        return FlavorIdentity(flavor_name=args.flavor, manifest_fields={})

    result = convert_task_to_rootfs_flavor(
        environment_dir=args.context,
        ext4_size_bytes=args.size_bytes,
        render_boot_layer=lambda _inputs: boot_layer(
            args.inner_task, prebuilt=prebuilt, agent=args.agent,
            agent_image=agent_image, env=env,
        ),
        compute_flavor_identity=identity,
        plan_systemd_provisioning=plan_systemd_provisioning,
        home=args.home,
        build_timeout_sec=args.build_timeout_sec,
    )
    print(f"[cella-runner] flavor   : {result.flavor_name}")
    print(f"[cella-runner] artifact : {result.artifact_path} ({result.size_bytes} bytes)")
    print(f"[cella-runner] sha3-256 : {result.sha3_256}")
    print(f"[cella-runner] reused   : {result.reused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
