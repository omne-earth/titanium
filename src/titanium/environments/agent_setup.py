from __future__ import annotations

import json
import re
import secrets
import shlex
from pathlib import Path

from titanium.models.agent.install import AgentInstallSpec, InstallStep
from titanium.models.agent.network import NetworkAllowlist

AGENT_INSTALL_DIR = ".titanium-agent-install"
EGRESS_PROXY_SERVICE = "titanium-egress-proxy"
EGRESS_PROXY_PORT = 8080

# `FROM [--flags…] <image> [AS <stage>]`, case-insensitive like Docker itself.
_FROM_LINE_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--\S+\s+)*)(?P<image>\S+)(?P<suffix>\s+.*)?$",
    re.IGNORECASE,
)
_AS_STAGE_RE = re.compile(r"\s+AS\s+(?P<stage>\S+)", re.IGNORECASE)


def qualify_image_reference(ref: str) -> str:
    """Return *ref* with an explicit registry, Docker's implied one made real.

    A short name (`ubuntu:24.04`, `alexgshaw/build-pmars`) means Docker Hub in
    the Dockerfile dialect tasks are written in; emitting `docker.io/…`
    preserves exactly that meaning while removing the resolution question for
    engines that would otherwise consult host-global search-registry
    configuration (PODMAN.md §2.1). Already-qualified references, `scratch`,
    and variable substitutions pass through untouched.
    """
    if not ref or ref.startswith("$") or "${" in ref or ref == "scratch":
        return ref
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return ref  # first component is a registry host
    if "/" in ref:
        return f"docker.io/{ref}"
    return f"docker.io/library/{ref}"


def qualify_dockerfile_froms(text: str) -> str:
    """Qualify every `FROM` image in *text*, leaving stage references alone.

    Multi-stage builds may `FROM <stage>` a name declared by an earlier
    `FROM … AS <stage>`; those are build-local and must never be rewritten
    into registry references.
    """
    stages: set[str] = set()
    lines = []
    for line in text.splitlines():
        match = _FROM_LINE_RE.match(line)
        if match:
            image = match.group("image")
            suffix = match.group("suffix") or ""
            as_match = _AS_STAGE_RE.search(suffix)
            if as_match:
                stages.add(as_match.group("stage").lower())
            if image.lower() not in stages:
                line = f"{match.group('prefix')}{qualify_image_reference(image)}{suffix}"
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def docker_run_command(script: str) -> str:
    return "RUN " + json.dumps(["/bin/bash", "-c", script])


def _run_with_step_env(step: InstallStep) -> str:
    if not step.env:
        return step.run
    exports = "".join(
        f"export {key}={shlex.quote(value)}; " for key, value in step.env.items()
    )
    return exports + step.run


def dockerfile_install_commands(
    install: AgentInstallSpec,
    *,
    user: str | int | None,
) -> list[str]:
    commands: list[str] = []
    docker_agent_user = "root" if user is None else str(user)
    for step in install.steps:
        docker_user = "root" if step.user == "root" else docker_agent_user
        commands.extend(
            [
                f"USER {docker_user}",
                docker_run_command(_run_with_step_env(step)),
            ]
        )
    return commands


def write_agent_dockerfile(
    *,
    build_dir: Path,
    source_environment_dir: Path,
    prebuilt_image_name: str | None,
    install: AgentInstallSpec,
    user: str | int | None,
) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_path = build_dir / "Dockerfile"

    if prebuilt_image_name:
        dockerfile = [f"FROM {qualify_image_reference(prebuilt_image_name)}"]
    else:
        source = source_environment_dir / "Dockerfile"
        dockerfile = [qualify_dockerfile_froms(source.read_text())]

    fingerprint = install.fingerprint()
    dockerfile.extend(
        [
            f"ARG TITANIUM_AGENT_INSTALL_FINGERPRINT={fingerprint}",
            docker_run_command(
                'printf "Titanium agent install fingerprint: %s\\n" '
                '"$TITANIUM_AGENT_INSTALL_FINGERPRINT"'
            ),
        ]
    )
    dockerfile.extend(dockerfile_install_commands(install, user=user))
    dockerfile.append("")
    dockerfile_path.write_text("\n".join(dockerfile))
    return dockerfile_path


def proxy_environment(
    token: str, host: str, port: int = EGRESS_PROXY_PORT
) -> dict[str, str]:
    proxy_url = f"http://agent:{token}@{host}:{port}"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }


def new_proxy_token() -> str:
    return secrets.token_urlsafe(24)


def squid_bootstrap_command() -> str:
    return r"""#!/usr/bin/env bash
set -eu

printf '%s' "$ALLOWLIST_DOMAINS" | tr ',' '\n' | sed '/^[[:space:]]*$/d' \
  > /tmp/allowed_domains.txt

htpasswd -bc /tmp/squid.passwd agent "$PROXY_TOKEN"

cat > /tmp/squid.conf <<'EOF'
http_port 0.0.0.0:8080
pid_filename /tmp/squid.pid
coredump_dir /tmp

auth_param basic program /usr/lib/squid/basic_ncsa_auth /tmp/squid.passwd
auth_param basic realm TitaniumPolicyProxy
acl authenticated proxy_auth REQUIRED

acl SSL_ports port 443
acl Safe_ports port 80 443
acl CONNECT method CONNECT
acl allowed_domains dstdomain "/tmp/allowed_domains.txt"

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow authenticated allowed_domains
http_access deny all

cache deny all
access_log stdio:/tmp/squid_access.log
cache_log /tmp/squid_cache.log
log_mime_hdrs off
shutdown_lifetime 1 seconds
EOF

exec squid -N -f /tmp/squid.conf -d 1
"""


def proxy_policy_env(allowlist: NetworkAllowlist, token: str) -> dict[str, str]:
    return {
        "PROXY_TOKEN": token,
        "ALLOWLIST_DOMAINS": ",".join(allowlist.domains),
    }


def write_docker_proxy_compose(
    *,
    path: Path,
    proxy_dir: Path,
    allowlist: NetworkAllowlist,
    token: str,
) -> Path:
    proxy_dir.mkdir(parents=True, exist_ok=True)
    (proxy_dir / "Dockerfile").write_text(
        "\n".join(
            [
                # Alpine over Ubuntu: same squid, same helper paths, a tenth
                # of the userland exposed to hostile sandbox traffic
                # (GVISOR.md §2.3 / §4). bash is explicit — the bootstrap and
                # the /dev/tcp healthcheck both need real bash.
                "FROM docker.io/library/alpine:3.22",
                "RUN apk add --no-cache bash squid apache2-utils ca-certificates",
                "COPY start-squid.sh /usr/local/bin/start-squid.sh",
                "RUN chmod +x /usr/local/bin/start-squid.sh",
                'CMD ["bash", "/usr/local/bin/start-squid.sh"]',
                "",
            ]
        )
    )
    (proxy_dir / "start-squid.sh").write_text(squid_bootstrap_command())
    compose = {
        "services": {
            "main": {
                "networks": ["titanium-egress-internal"],
                "depends_on": {
                    EGRESS_PROXY_SERVICE: {
                        "condition": "service_healthy",
                    },
                },
            },
            EGRESS_PROXY_SERVICE: {
                "build": {"context": str(proxy_dir.resolve().absolute())},
                "environment": proxy_policy_env(allowlist, token),
                "healthcheck": {
                    "test": ["CMD-SHELL", "bash -lc '</dev/tcp/127.0.0.1/8080'"],
                    "interval": "1s",
                    "timeout": "1s",
                    "retries": 30,
                },
                "networks": ["titanium-egress-internal", "default"],
            },
        },
        "networks": {
            "titanium-egress-internal": {
                "internal": True,
            },
            # Declared explicitly: the proxy joins `default` for outbound egress,
            # and podman-compose (unlike docker compose) refuses to parse a
            # service referencing a network the file does not declare.
            "default": {},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2))
    return path


def shell_export_env(env: dict[str, str]) -> str:
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
