"""Linux container file transfer for a ``main`` service running under gVisor.

``docker compose cp`` is unusable in both directions against a running gVisor
container: the root filesystem is sandbox-private, so copies in are never
observed by the sandbox and copies out report that the file does not exist.
Every transfer therefore moves through the scoped staging bind mounts declared
in :mod:`titanium.environments.gvisor.runtime`, with the copy between staging and
the private root filesystem performed *inside* the sandbox. There is
deliberately no ``docker cp`` fast path -- not even for paths that exist in the
image, where a host-side copy would silently return stale image content instead
of what the sandbox wrote.

The observable contract matches :class:`~titanium.environments.docker.docker_unix.UnixOps`:

* ``upload_dir`` copies directory *contents*, never an extra containing
  directory (a regression that silently breaks the verifier by producing
  ``/tests/tests/test.sh``), and never rewrites the target directory's own
  ownership, mode or timestamps.
* ``upload_file`` to an existing directory lands under the source's basename.
* Uploaded entries end up root-owned, matching what callers already expect of
  ``docker cp``.
* Symlinks are copied as symlinks and never followed, so a hostile symlink
  cannot redirect a copy or a ``chown`` outside its tree.
* Downloads chown only the staged *copy* to the host user; the original
  in-container source is never touched, because chowning sources breaks the
  next step of a multi-step task when the agent runs as a non-root user.

Host-side placement is the other half of the symlink story. A download
destination such as ``<trial_dir>/artifacts`` is bind-mounted into the sandbox
and therefore agent-writable, so anything already inside it is untrusted: the
agent can plant a symlink and wait for the next download to write through it.
The staged export tree and destination parents are both treated as untrusted.
They are traversed component-by-component through directory descriptors with
``O_NOFOLLOW``; staged special files are rejected, and leaf files are created
with ``O_EXCL`` so a symlink appearing between removal of an old entry and
creation of the new one is never written through.

The symlink-safe placement helpers live in this module rather than a separate
one: they are the host-side half of the same transfer contract, they are only
ever reached through it, and the two halves are reviewed and tested together.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from titanium.environments.docker.docker_unix import UnixOps
from titanium.environments.gvisor.runtime import STAGE_IN, STAGE_OUT

if TYPE_CHECKING:
    from titanium.environments.gvisor.environment import GVisorEnvironment

# Bounded so a hostile process racing us cannot spin the resolver forever.
_MAX_RESOLVE_ATTEMPTS = 8


# ---------------------------------------------------------------------------
# Symlink-safe host-side placement
# ---------------------------------------------------------------------------


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Traversal-only open for intermediate components. O_PATH needs search (x)
# permission, not read: the runner user deliberately carries x-only ACLs on
# every component above the repo, so an O_RDONLY walk dies with EACCES at
# /home — found by the first artifact collected from outside the bind
# mounts. The symlink hard failure survives: with O_PATH, O_NOFOLLOW opens
# a symlink as itself and O_DIRECTORY then rejects it with ENOTDIR.
_WALK_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_dir_tree(path: Path, *, create: bool) -> int:
    """Open *path* component-by-component without following any symlink.

    Walking from ``/`` with directory descriptors removes the usual
    check-then-open gap: once a component is opened, later renames do not
    redirect the descriptor. Missing components may be created for trusted
    destination parents, but an existing symlink or non-directory is always a
    hard failure rather than something we traverse or replace.

    Intermediate components open with ``_WALK_FLAGS`` (traversal only); the
    final component opens with ``_DIR_FLAGS``, because callers list or copy
    inside it and the leaf directories all live under the trial directory,
    where read permission is guaranteed.
    """
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts[1:]
    if not parts:
        return os.open("/", _DIR_FLAGS)
    current_fd = os.open("/", _WALK_FLAGS)
    try:
        for index, component in enumerate(parts):
            flags = _DIR_FLAGS if index == len(parts) - 1 else _WALK_FLAGS
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, dir_fd=current_fd)
                except FileExistsError:
                    # A concurrent creator won the race. The O_NOFOLLOW open
                    # below decides whether the resulting entry is acceptable.
                    pass
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RuntimeError(
                        f"Refusing to traverse unsafe directory component "
                        f"{component!r} in {absolute}."
                    ) from exc
                raise
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _remove_entry(dir_fd: int, name: str) -> None:
    """Delete *name* under *dir_fd* without following it.

    ``os.unlink`` acts on the symlink itself, so a hostile link is removed
    rather than dereferenced -- the file it pointed at is left untouched.
    """
    try:
        os.unlink(name, dir_fd=dir_fd)
        return
    except FileNotFoundError:
        return
    except OSError as exc:
        # Linux reports EISDIR here; some platforms report EPERM.
        if exc.errno not in (errno.EISDIR, errno.EPERM):
            raise
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _descend(dir_fd: int, name: str) -> int:
    """Open the child directory *name*, creating it, never following a symlink."""
    for _ in range(_MAX_RESOLVE_ATTEMPTS):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            try:
                os.mkdir(name, dir_fd=dir_fd)
            except FileExistsError:
                pass
        except OSError as exc:
            # ELOOP: O_NOFOLLOW refused a symlink. ENOTDIR: the entry is a file.
            # Neither is traversed; both are replaced by a real directory.
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            _remove_entry(dir_fd, name)
    raise RuntimeError(
        f"Could not safely open the destination directory {name!r}; it is "
        "being modified concurrently."
    )


def _write_file(source_fd: int, info: os.stat_result, dir_fd: int, name: str) -> None:
    _remove_entry(dir_fd, name)
    # O_EXCL: if anything reappears at this name after the removal above, the
    # open fails instead of writing through it, so this is not a bare
    # check-then-copy sequence.
    destination_fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        with os.fdopen(os.dup(source_fd), "rb") as staged:
            with os.fdopen(os.dup(destination_fd), "wb") as destination:
                shutil.copyfileobj(staged, destination)
        os.fchmod(destination_fd, stat.S_IMODE(info.st_mode))
        os.utime(destination_fd, ns=(info.st_atime_ns, info.st_mtime_ns))
    finally:
        os.close(destination_fd)


def _write_symlink(target: str, dir_fd: int, name: str) -> None:
    _remove_entry(dir_fd, name)
    os.symlink(target, name, dir_fd=dir_fd)


def _open_regular(source_dir_fd: int, name: str) -> tuple[int, os.stat_result]:
    """Open a staged regular file without following a replacement symlink.

    ``O_NONBLOCK`` prevents a hostile FIFO from hanging the Titanium process before
    ``fstat`` can reject it. The post-open type check also catches a file that
    was swapped between enumeration and opening.
    """
    try:
        source_fd = os.open(name, _FILE_FLAGS, dir_fd=source_dir_fd)
    except OSError as exc:
        raise RuntimeError(f"Could not safely open staged entry {name!r}.") from exc
    info = os.fstat(source_fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(source_fd)
        raise RuntimeError(
            f"Refusing to copy staged entry {name!r}: only regular files, "
            "directories, and symlinks are supported."
        )
    return source_fd, info


def _copy_into(source_dir_fd: int, destination_dir_fd: int) -> None:
    for name in sorted(os.listdir(source_dir_fd)):
        try:
            info = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Staged entry {name!r} disappeared while it was being copied."
            ) from exc

        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(name, dir_fd=source_dir_fd)
            except OSError as exc:
                raise RuntimeError(
                    f"Staged symlink {name!r} changed while it was being copied."
                ) from exc
            _write_symlink(target, destination_dir_fd, name)
        elif stat.S_ISDIR(info.st_mode):
            try:
                child_source_fd = os.open(name, _DIR_FLAGS, dir_fd=source_dir_fd)
            except OSError as exc:
                raise RuntimeError(
                    f"Could not safely open staged directory {name!r}."
                ) from exc
            child_destination_fd = _descend(destination_dir_fd, name)
            try:
                _copy_into(child_source_fd, child_destination_fd)
            finally:
                os.close(child_destination_fd)
                os.close(child_source_fd)
        elif stat.S_ISREG(info.st_mode):
            source_fd, opened_info = _open_regular(source_dir_fd, name)
            try:
                _write_file(source_fd, opened_info, destination_dir_fd, name)
            finally:
                os.close(source_fd)
        else:
            raise RuntimeError(
                f"Refusing to copy staged entry {name!r}: only regular files, "
                "directories, and symlinks are supported."
            )


def safe_copy_tree(staged_dir: Path, destination: Path) -> None:
    """Copy the contents of *staged_dir* into *destination* safely.

    Both trees are opened through directory descriptors with ``O_NOFOLLOW``.
    Existing entries *inside* the destination are replaced without traversal,
    while a symlink in any destination parent is rejected. The staged tree is
    treated as attacker-controlled for the entire operation.
    """
    source_fd = _open_dir_tree(staged_dir, create=False)
    parent_fd = _open_dir_tree(destination.parent, create=True)
    try:
        destination_fd = _descend(parent_fd, destination.name)
        try:
            _copy_into(source_fd, destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(parent_fd)
        os.close(source_fd)


def safe_place_file(staged_file: Path, destination: Path) -> None:
    """Place one staged file or symlink without following either tree."""
    source_parent_fd = _open_dir_tree(staged_file.parent, create=False)
    destination_parent_fd = _open_dir_tree(destination.parent, create=True)
    try:
        try:
            info = os.stat(
                staged_file.name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Staged entry {staged_file.name!r} disappeared before placement."
            ) from exc

        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(staged_file.name, dir_fd=source_parent_fd)
            except OSError as exc:
                raise RuntimeError(
                    f"Staged symlink {staged_file.name!r} changed before placement."
                ) from exc
            _write_symlink(target, destination_parent_fd, destination.name)
        elif stat.S_ISREG(info.st_mode):
            source_fd, opened_info = _open_regular(source_parent_fd, staged_file.name)
            try:
                _write_file(
                    source_fd,
                    opened_info,
                    destination_parent_fd,
                    destination.name,
                )
            finally:
                os.close(source_fd)
        else:
            raise RuntimeError(
                f"Refusing to copy staged entry {staged_file.name!r}: only "
                "regular files and symlinks are supported."
            )
    finally:
        os.close(destination_parent_fd)
        os.close(source_parent_fd)


class GVisorUnixOps(UnixOps):
    """Staging-mount transfer operations for gVisor-sandboxed containers."""

    def __init__(self, env: GVisorEnvironment) -> None:
        super().__init__(env)

    # -- staging helpers ---------------------------------------------------

    @staticmethod
    def _op_id() -> str:
        return uuid.uuid4().hex

    def _new_upload_stage(self) -> tuple[Path, PurePosixPath]:
        """Create a fresh host upload directory and its container path."""
        op_id = self._op_id()
        host_dir = self._env.stage_in / op_id
        host_dir.mkdir(parents=True, exist_ok=False)
        return host_dir, STAGE_IN / op_id

    def _new_download_stage(self) -> tuple[Path, PurePosixPath]:
        """Reserve a fresh download directory; the container creates it."""
        op_id = self._op_id()
        return self._env.stage_out / op_id, STAGE_OUT / op_id

    @staticmethod
    def _discard(host_dir: Path) -> None:
        shutil.rmtree(host_dir, ignore_errors=True)

    def _align_stage_labels(self, staged_root: Path) -> None:
        """Re-align a staged tree's SELinux context with the staging mount.

        ``shutil.copy2``/``copytree`` copy extended attributes, and
        ``security.selinux`` rides along: a staged upload keeps its *source*
        label (for a checkout under $HOME, ``user_home_t``) instead of the
        ``container_file_t`` the staging mount's relabel established. An
        unconfined sandbox never notices — which is why the runsc flavors,
        whose ``main`` runs ``label=disable``, masked this — but a labeled
        sandbox (krun's ``container_kvm_t``) is denied the read. Stamp the
        staging root's own context onto everything staged. Best-effort by
        design: hosts without SELinux raise OSError on the xattr and skip.
        """
        try:
            context = os.getxattr(self._env.stage_in, "security.selinux")
        except OSError:
            return
        for path in [staged_root, *staged_root.rglob("*")]:
            try:
                os.setxattr(
                    path, "security.selinux", context, follow_symlinks=False
                )
            except OSError:
                pass

    def _host_owner(self) -> str | None:
        if not hasattr(os, "getuid"):
            return None
        return f"{os.getuid()}:{os.getgid()}"

    async def _exec_root(self, command: str, *, what: str) -> None:
        result = await self._env.exec(command, user="root")
        if result.return_code != 0:
            output = result.stderr or result.stdout or "no output"
            raise RuntimeError(
                f"gVisor staging transfer failed to {what} "
                f"(exit code {result.return_code}): {output}"
            )

    def cleanup(self) -> None:
        """Remove any staging directories left behind by earlier operations."""
        for directory in (self._env.stage_in, self._env.stage_out):
            shutil.rmtree(directory, ignore_errors=True)

    # -- uploads -----------------------------------------------------------

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        host_dir, container_dir = self._new_upload_stage()
        try:
            shutil.copy2(source, host_dir / source.name, follow_symlinks=False)
            self._align_stage_labels(host_dir)
            staged = shlex.quote(str(container_dir / source.name))
            target = shlex.quote(str(target_path))
            base = shlex.quote(source.name)
            # Mirror `docker cp <file> main:<target>`: a target that names an
            # existing directory (or ends in a slash) receives the file under
            # its basename; anything else is the destination path itself.
            command = (
                "set -e; "
                f"dest={target}; "
                f'case "$dest" in '
                f'*/) dest="${{dest%/}}/"{base} ;; '
                f'*) if [ -d "$dest" ]; then dest="$dest/"{base}; fi ;; '
                "esac; "
                'mkdir -p "$(dirname "$dest")"; '
                f'cp -a {staged} "$dest"; '
                'chown -h 0:0 "$dest"'
            )
            await self._exec_root(command, what=f"upload {source} to {target_path}")
        finally:
            self._discard(host_dir)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        host_dir, container_dir = self._new_upload_stage()
        try:
            staging_root = host_dir / "payload"
            shutil.copytree(source, staging_root, symlinks=True)
            self._align_stage_labels(host_dir)
            staged = shlex.quote(str(container_dir / "payload"))
            target = shlex.quote(str(target_dir))
            # Copy the staged entries one by one rather than `cp -a "$S"/.
            # "$T"/`, which would additionally stamp the staging directory's own
            # ownership, mode and timestamps onto an existing target directory.
            # `find -mindepth 1 -maxdepth 1` covers dotfiles and names with
            # spaces, and `cp -a` keeps each entry recursive, mode-preserving
            # and symlink-preserving. Ownership is then normalised on exactly
            # the entries introduced -- enumerated from the staged tree, so
            # pre-existing files under the target keep their owner -- with `-h`
            # so a symlink is chowned rather than its referent.
            command = (
                "set -e; "
                f"T={target}; S={staged}; "
                'mkdir -p "$T"; '
                'cd "$S"; '
                'find . -mindepth 1 -maxdepth 1 -exec cp -a {} "$T"/ \\; ; '
                'find . -mindepth 1 -exec chown -h 0:0 "$T"/{} \\;'
            )
            await self._exec_root(command, what=f"upload {source} to {target_dir}")
        finally:
            self._discard(host_dir)

    # -- downloads ---------------------------------------------------------

    async def _export(
        self, source: str, *, contents: bool
    ) -> tuple[Path, PurePosixPath]:
        """Copy *source* into a fresh staging directory owned by the host user."""
        host_dir, container_dir = self._new_download_stage()
        staged = shlex.quote(str(container_dir))
        spec = shlex.quote(f"{source.rstrip('/')}/." if contents else source)
        owner = self._host_owner()
        # The chown targets the staged copy only. `-h` keeps a hostile symlink
        # in the exported tree from redirecting the chown at an arbitrary file.
        chown = f'; chown -Rh {owner} "$D"' if owner else ""
        command = f'set -e; D={staged}; mkdir -p "$D"; cp -a {spec} "$D"/{chown}'
        await self._exec_root(command, what=f"export {source} from the sandbox")
        if not os.path.lexists(host_dir):
            raise RuntimeError(
                f"gVisor staging transfer exported {source!r} but nothing "
                f"appeared at {host_dir}."
            )
        return host_dir, container_dir

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        name = PurePosixPath(str(source_path).rstrip("/")).name
        host_dir, _ = await self._export(str(source_path), contents=False)
        try:
            staged = host_dir / name
            # lexists, not exists: a dangling symlink is a legitimate export
            # and must survive the round trip rather than look like a failure.
            if not os.path.lexists(staged):
                raise RuntimeError(
                    f"gVisor staging transfer could not find {name!r} after "
                    f"exporting {source_path!r}."
                )
            target = Path(target_path)
            # A symlink that happens to point at a directory is replaced, not
            # entered, so a planted link cannot redirect the write.
            if target.is_dir() and not target.is_symlink():
                target = target / name
            safe_place_file(staged, target)
        finally:
            self._discard(host_dir)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        host_dir, _ = await self._export(str(source_dir), contents=True)
        try:
            safe_copy_tree(host_dir, Path(target_dir))
        finally:
            self._discard(host_dir)
