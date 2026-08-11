"""File-transfer ops for the Podman environment: podman-compose has no ``cp``
subcommand, so these overrides resolve the container by label and use
``podman cp`` directly. Exec still goes through podman-compose."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from titanium.environments.docker.docker_unix import UnixOps

if TYPE_CHECKING:
    from titanium.environments.podman.podman import PodmanEnvironment


class PodmanUnixOps(UnixOps):
    """UnixOps with compose-cp replaced by `podman cp`."""

    _env: PodmanEnvironment

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        cid = await self._env.resolve_container()
        await self._env.podman_cp(str(source_path), f"{cid}:{target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        cid = await self._env.resolve_container()
        # Trailing /. copies directory *contents*, matching `compose cp` and
        # avoiding a nested dir when target_dir already exists.
        await self._env.podman_cp(f"{source_dir}/.", f"{cid}:{target_dir}")

    async def download_file(
        self, source_path: str, target_path: Path | str
    ) -> None:
        await self._env._chown_to_host_user(source_path)
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        cid = await self._env.resolve_container()
        await self._env.podman_cp(f"{cid}:{source_path}", str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await self._env._chown_to_host_user(source_dir, recursive=True)
        os.makedirs(target_dir, exist_ok=True)
        cid = await self._env.resolve_container()
        await self._env.podman_cp(f"{cid}:{source_dir}/.", str(target_dir))
