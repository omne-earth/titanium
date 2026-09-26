"""Making an exported task filesystem one that systemd can boot.

Cella boots a kernel and an ext4. Whatever is at ``/sbin/init`` in that
filesystem becomes PID 1, and there is no container runtime left to paper over
the difference -- no entrypoint wrapper, no ``podman run``, no OCI spec. A task
image built ``FROM ubuntu:24.04`` has no init at all; a task image built from a
distro that ships one may have a *different* one.

So this module answers one question about an exported rootfs tar,

    can this filesystem boot systemd as PID 1?

and, when the answer is no, runs one declared provisioning plan to make it
so -- then re-asks the same question of the result rather than trusting the
build's exit status.

Three properties hold throughout, and each rules out an easier design:

* **Nothing from the task is executed to find out.** No ``podman run``, no
  ``/bin/sh -c 'cat /etc/os-release'``. Detection reads the exported tar with
  Python. Running task code to classify task code inverts the trust order.
* **Nothing is guessed from a name.** ``ubuntu:24.04`` in a ``FROM`` line is a
  string, not evidence. Every fact in :class:`GuestOsInfo` was read out of the
  filesystem.
* **There is no fallback.** An image this module cannot make bootable fails the
  conversion. Substituting a plausible init, or booting whatever was already
  there, would hand the guest a PID 1 nobody chose.

What to install is not decided here. :func:`plan_systemd_provisioning` holds
that policy and is kept separate from the machinery around it, so the choice
between package managers -- and the judgement that a distro is unsupported --
stays one reviewable decision rather than something inferred from an ``ID``
field.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from titanium.environments.cella.constants import (
    GUEST_INIT_PATH,
    MAX_SYMLINK_HOPS,
    OS_RELEASE_CANDIDATES,
    OS_RELEASE_KEYS,
    STRATEGY_ALREADY_SYSTEMD,
    SYSTEMD_BINARY_CANDIDATES,
)
from titanium.environments.cella.image_config import parse_image_record
from titanium.environments.cella.podman import (
    build_image,
    export_rootfs_tar,
    inspect_image,
    new_build_tag,
    untag_image,
)


class SystemdBootError(RuntimeError):
    """The guest filesystem cannot be made systemd-bootable."""


# --------------------------------------------------------------- the facts


@dataclass(frozen=True)
class GuestOsInfo:
    """What the exported filesystem says about itself.

    Every field was read out of the archive. Nothing here was inferred from an
    image name, a tag, or a ``FROM`` line, and nothing here required running
    anything the task shipped.

    Attributes:
        os_id: ``ID`` from os-release, or ``None`` when os-release is absent or
            declares none. ``None`` is a fact, not an error: whether an
            unidentifiable guest is supportable is the planner's call.
        id_like: ``ID_LIKE``, split on whitespace, in declared order.
        version_id: ``VERSION_ID``, verbatim.
        pretty_name: ``PRETTY_NAME``, verbatim.
        init_present: Whether :data:`GUEST_INIT_PATH` resolves to something
            that exists in the archive.
        init_resolved_path: The canonical path ``/sbin/init`` resolves to,
            after following the guest's own symlinks. ``None`` when absent.
            Recorded even when it is *not* systemd -- an image whose init is
            something else is a fact the planner must see, not one to overwrite
            quietly.
        systemd_path: The first entry of :data:`SYSTEMD_BINARY_CANDIDATES`
            present as a real file, or ``None``.
        systemd_bootable: The whole postcondition, defined in
            :func:`probe_archive`.
    """

    os_id: str | None
    id_like: tuple[str, ...]
    version_id: str | None
    pretty_name: str | None

    init_present: bool
    init_resolved_path: str | None

    systemd_path: str | None
    systemd_bootable: bool


@dataclass(frozen=True)
class BuildRun:
    """One provisioning command, as an argv vector.

    Never a shell string. A plan that genuinely needs shell semantics says so
    out loud -- ``BuildRun(argv=("/bin/sh", "-c", "..."))`` -- so that the
    decision to invoke a shell is visible in the plan rather than implied by
    the presence of a metacharacter.
    """

    argv: tuple[str, ...]


@dataclass(frozen=True)
class SystemdProvisionPlan:
    """What the provisioning policy decided to do about a non-bootable guest.

    Attributes:
        strategy: A short stable name for what was decided, recorded as
            provenance. It is a label, not an instruction: nothing in this
            module switches on its value.
        steps: The commands to run, in order, as ``USER 0`` in a derived build.
    """

    strategy: str
    steps: tuple[BuildRun, ...]


@dataclass(frozen=True)
class PreparedSystemdRootfs:
    """The filesystem the ext4 will actually be built from, and how it got there.

    Attributes:
        rootfs_tar: The final export. The source tar when nothing was needed,
            the derived one when provisioning ran.
        source_info: What the task's own filesystem looked like.
        final_info: What the filesystem being shipped looks like. Equal to
            *source_info* when no provisioning ran, and in every case it is a
            *measurement of the final tar*, never a prediction.
        strategy: :data:`STRATEGY_ALREADY_SYSTEMD`, or the plan's own strategy.
        derived: Whether a second image was built.
        recipe_bytes: The derived build file, byte for byte, or ``None``. Kept
            because it explains the artifact -- though see the note on
            :attr:`boot_image_id` for what it does not establish.
        boot_image_id: The content id of the image the final tar came from.
            This, not the recipe, is what identifies the filesystem: a recipe
            saying ``apt-get install systemd`` resolves against a moving index
            and produces different content on different days.
    """

    rootfs_tar: Path
    source_info: GuestOsInfo
    final_info: GuestOsInfo

    strategy: str
    derived: bool

    recipe_bytes: bytes | None
    boot_image_id: str


#: Given what the guest filesystem turned out to be, decide how to make it
#: boot systemd. Called at most once per conversion, and only when the source
#: is not already bootable.
PlanSystemdProvisioning = Callable[[GuestOsInfo], SystemdProvisionPlan]


# ------------------------------------------------- reading the exported tar


@dataclass(frozen=True)
class _Entry:
    kind: str  # "file" | "dir" | "symlink" | "hardlink" | "other"
    linkname: str
    member: tarfile.TarInfo


class RootfsArchive:
    """A read-only view of an exported rootfs tar.

    Nothing is extracted. The archive is indexed by canonical guest path and
    read through :meth:`read_file`, so no path the task chose ever becomes a
    path on the host -- which is what keeps a hostile ``../../etc/passwd``
    member a lookup key rather than a write.

    Symlinks are resolved as *guest* links throughout. An absolute target is
    absolute against the guest's root, never the host's, and no host
    filesystem call is involved in following one.
    """

    def __init__(self, archive: tarfile.TarFile) -> None:
        self._archive = archive
        self._entries: dict[str, _Entry] = {}
        self._ambiguous: set[str] = set()
        # Ancestors of every member. A tar is not required to carry an entry
        # for each directory it implies, and a probe that demanded one would
        # report a filesystem with no /usr merely because the archive listed
        # /usr/lib/systemd/systemd and nothing above it.
        self._implied_dirs: set[str] = set()
        self._index()

    @classmethod
    @contextmanager
    def open(cls, tar_path: Path) -> Iterator[RootfsArchive]:
        """Open *tar_path* for the duration of the block, and close it after.

        A malformed archive surfaces as a :class:`SystemdBootError` rather than
        a bare ``TarError``: from the converter's side, an unreadable export is
        the same class of problem as an unbootable one.
        """
        try:
            with tarfile.open(tar_path, "r:*") as archive:
                yield cls(archive)
        except tarfile.TarError as exc:
            raise SystemdBootError(
                f"{tar_path} is not a readable root filesystem tar: {exc}"
            ) from exc

    def _index(self) -> None:
        for member in self._archive.getmembers():
            canonical = _canonical(member.name)
            if canonical is None:
                # A member name containing `..` cannot be placed in a guest
                # filesystem unambiguously. `podman export` does not emit one.
                raise SystemdBootError(
                    f"Exported filesystem carries a member whose name escapes "
                    f"the guest root: {member.name!r}."
                )
            entry = _Entry(
                kind=_kind_of(member),
                linkname=member.linkname or "",
                member=member,
            )
            previous = self._entries.get(canonical)
            if previous is not None and (
                previous.kind != entry.kind or previous.linkname != entry.linkname
            ):
                # Two different things claiming one path. Extraction would pick
                # one by ordering; a probe that picked the other would report a
                # filesystem nobody is going to boot.
                self._ambiguous.add(canonical)
            self._entries[canonical] = entry

            parts = _split(canonical)
            for depth in range(1, len(parts)):
                self._implied_dirs.add("/" + "/".join(parts[:depth]))

    def _entry(self, canonical: str) -> _Entry | None:
        if canonical in self._ambiguous:
            raise SystemdBootError(
                f"Exported filesystem declares {canonical} more than once, as "
                f"different objects. Refusing to guess which one boots."
            )
        return self._entries.get(canonical)

    def resolve(self, path: str) -> str | None:
        """The canonical path *path* names, or ``None`` when nothing is there.

        Follows symlinks the way the guest kernel would: an absolute target
        restarts at the guest root, a relative one is relative to the link's
        own parent, and ``..`` walks back up. A hard link resolves to the
        member it duplicates -- for the question this module asks, "which file
        is this" is the point, and a hard-linked init is the same file as its
        target.

        Raises:
            SystemdBootError: on a chain longer than :data:`MAX_SYMLINK_HOPS`,
                or on one that would resolve above the guest root.
        """
        resolved: list[str] = []
        pending = _split(path)
        hops = 0

        while pending:
            component = pending.pop(0)
            if component == ".":
                continue
            if component == "..":
                if not resolved:
                    raise SystemdBootError(
                        f"Resolving {path} leaves the guest root; refusing to "
                        f"follow it onto the host."
                    )
                resolved.pop()
                continue

            candidate = "/" + "/".join([*resolved, component])
            entry = self._entry(candidate)
            if entry is None:
                if candidate in self._implied_dirs:
                    # A directory the archive implies but never lists. An
                    # explicit entry always wins over this, so a symlink the
                    # image really shipped is still followed.
                    resolved.append(component)
                    continue
                return None

            if entry.kind in ("symlink", "hardlink"):
                hops += 1
                if hops > MAX_SYMLINK_HOPS:
                    raise SystemdBootError(
                        f"Resolving {path} exceeded {MAX_SYMLINK_HOPS} links; "
                        f"the guest filesystem has a link loop."
                    )
                target = entry.linkname
                if not target:
                    return None
                if entry.kind == "hardlink" or target.startswith("/"):
                    # A hard link's target is recorded as an archive member
                    # name, which is guest-absolute by construction.
                    resolved = []
                pending = _split(target) + pending
                continue

            resolved.append(component)

        return "/" + "/".join(resolved)

    def is_file(self, canonical: str) -> bool:
        entry = self._entry(canonical)
        return entry is not None and entry.kind == "file"

    def read_file(self, path: str) -> bytes | None:
        """The bytes at *path* after resolution, or ``None`` when absent."""
        canonical = self.resolve(path)
        if canonical is None:
            return None
        entry = self._entry(canonical)
        if entry is None or entry.kind != "file":
            return None
        handle = self._archive.extractfile(entry.member)
        if handle is None:
            return None
        with handle:
            return handle.read()


def _kind_of(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.isdir():
        return "dir"
    if member.isfile():
        return "file"
    return "other"


def _split(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _canonical(name: str) -> str | None:
    """A member name as a guest-absolute path, or ``None`` if it escapes."""
    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/" + "/".join(parts)


# ----------------------------------------------------------------- os-release


def _parse_os_release(raw: bytes) -> dict[str, str]:
    """The four fields this module reports, and nothing else.

    Not evaluated as shell. os-release is *shell-compatible* syntax, which is
    not the same as being safe to source, and sourcing a file the task wrote
    would execute task content during detection.

    A malformed value for one of the four keys raises rather than being
    guessed at. A malformed line for any other key is skipped -- this parser
    has no opinion about fields it does not report.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemdBootError(
            f"os-release is not valid UTF-8: {exc}. Refusing to guess at the "
            f"guest's identity."
        ) from exc

    found: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or key not in OS_RELEASE_KEYS:
            continue
        found[key] = _unquote(key, value.strip(), lineno)
    return found


def _unquote(key: str, value: str, lineno: int) -> str:
    if not value:
        return ""

    quote = value[0]
    if quote in ('"', "'"):
        if len(value) < 2 or not value.endswith(quote):
            raise SystemdBootError(
                f"os-release line {lineno}: {key} opens with {quote} and never "
                f"closes it."
            )
        inner = value[1:-1]
        if quote == "'":
            if "'" in inner:
                raise SystemdBootError(
                    f"os-release line {lineno}: {key} has a quote inside a "
                    f"single-quoted value."
                )
            return inner
        return _unescape(key, inner, lineno)

    # Unquoted os-release values are a restricted charset. Anything outside it
    # would have to be interpreted as shell to know what it means.
    if any(character.isspace() for character in value) or any(
        character in value for character in "\"'$`\\|&;<>()"
    ):
        raise SystemdBootError(
            f"os-release line {lineno}: {key}={value!r} is unquoted but is not "
            f"a plain value. Refusing to interpret it as shell."
        )
    return value


def _unescape(key: str, inner: str, lineno: int) -> str:
    out: list[str] = []
    index = 0
    while index < len(inner):
        character = inner[index]
        if character == "\\":
            if index + 1 >= len(inner):
                raise SystemdBootError(
                    f"os-release line {lineno}: {key} ends in a trailing backslash."
                )
            following = inner[index + 1]
            if following not in '"\\$`':
                raise SystemdBootError(
                    f"os-release line {lineno}: {key} contains the escape "
                    f"\\{following}, which os-release does not define."
                )
            out.append(following)
            index += 2
            continue
        if character == '"':
            raise SystemdBootError(
                f"os-release line {lineno}: {key} has an unescaped quote inside "
                f"a double-quoted value."
            )
        out.append(character)
        index += 1
    return "".join(out)


# --------------------------------------------------------------- the probe


def probe_archive(archive: RootfsArchive) -> GuestOsInfo:
    """Read one exported filesystem's own account of itself.

    ``systemd_bootable`` is deliberately mechanical, and deliberately narrow.
    It is true only when

    * a recognized systemd executable is present as a real file, **and**
    * ``/sbin/init`` resolves, through the guest's own links, to that same
      file.

    Both halves are load-bearing. A filesystem carrying every systemd package
    file but no ``/sbin/init`` does not boot -- the kernel needs a path, not a
    package. And a ``/sbin/init`` that resolves to some other init is not made
    into systemd by systemd being installed nearby.

    Neither half is distro policy. This function does not know what apt is.
    """
    fields: dict[str, str] = {}
    for candidate in OS_RELEASE_CANDIDATES:
        raw = archive.read_file(candidate)
        if raw is not None:
            fields = _parse_os_release(raw)
            break

    systemd_targets: dict[str, str] = {}
    for candidate in SYSTEMD_BINARY_CANDIDATES:
        resolved = archive.resolve(candidate)
        if resolved is not None and archive.is_file(resolved):
            systemd_targets[candidate] = resolved

    init_resolved = archive.resolve(GUEST_INIT_PATH)
    id_like = tuple(fields.get("ID_LIKE", "").split())

    return GuestOsInfo(
        os_id=fields.get("ID") or None,
        id_like=id_like,
        version_id=fields.get("VERSION_ID") or None,
        pretty_name=fields.get("PRETTY_NAME") or None,
        init_present=init_resolved is not None,
        init_resolved_path=init_resolved,
        systemd_path=next(iter(systemd_targets), None),
        systemd_bootable=(
            init_resolved is not None and init_resolved in set(systemd_targets.values())
        ),
    )


def probe_rootfs_tar(tar_path: Path) -> GuestOsInfo:
    """:func:`probe_archive`, opening and closing the tar around it."""
    with RootfsArchive.open(tar_path) as archive:
        return probe_archive(archive)


# ------------------------------------------------------ provisioning policy


def plan_systemd_provisioning(
    info: GuestOsInfo,
) -> SystemdProvisionPlan:
    if info.systemd_bootable:
        raise SystemdBootError(
            "Systemd provisioning was requested for a rootfs that is "
            "already systemd-bootable."
        )

    if info.init_present:
        raise SystemdBootError(
            "Refusing to replace an existing non-systemd /sbin/init "
            f"(resolved path: {info.init_resolved_path!r})."
        )

    family = {
        value.lower() for value in (info.os_id, *info.id_like) if value is not None
    }

    if "debian" in family or "ubuntu" in family:
        return SystemdProvisionPlan(
            strategy="debian-systemd",
            steps=(
                BuildRun(
                    argv=(
                        "/usr/bin/apt-get",
                        "update",
                    )
                ),
                BuildRun(
                    argv=(
                        "/usr/bin/env",
                        "DEBIAN_FRONTEND=noninteractive",
                        "/usr/bin/apt-get",
                        "install",
                        "-y",
                        "--no-install-recommends",
                        "systemd",
                        "systemd-sysv",
                        # The rung's runner configures wire nics
                        # in-guest (the agent line); ip(8) is the one
                        # tool that needs.
                        "iproute2",
                    )
                ),
                BuildRun(
                    argv=(
                        "/bin/rm",
                        "-f",
                        "/etc/machine-id",
                    )
                ),
                BuildRun(
                    argv=(
                        "/usr/bin/touch",
                        "/etc/machine-id",
                    )
                ),
            ),
        )

    raise SystemdBootError(
        "No validated systemd provisioning strategy for guest OS "
        f"{info.os_id!r}, ID_LIKE={info.id_like!r}."
    )


# ------------------------------------------------------------ plan handling


def validate_provision_plan(plan: SystemdProvisionPlan) -> SystemdProvisionPlan:
    """Check a plan's shape and return it unchanged.

    The plan is trusted policy, but a trusted decision can still be malformed,
    and every value here ends up in a build file. Nothing is normalized or
    repaired: a plan that arrives wrong leaves as an exception.

    Only ever called for a source that is *not* bootable, so an empty plan is
    a contradiction -- it claims nothing needs doing about a filesystem that
    demonstrably does.

    Raises:
        TypeError: when the plan or one of its parts is the wrong type.
        SystemdBootError: when a well-typed plan cannot be executed.
    """
    if not isinstance(plan, SystemdProvisionPlan):
        raise TypeError(
            f"A provisioning plan must be a SystemdProvisionPlan, got "
            f"{type(plan).__name__}."
        )
    if not isinstance(plan.strategy, str):
        raise TypeError(
            f"A plan's strategy must be a str, got {type(plan.strategy).__name__}."
        )
    if not plan.strategy.strip():
        raise SystemdBootError("A plan's strategy cannot be empty.")
    if "\x00" in plan.strategy:
        raise SystemdBootError("A plan's strategy contains a NUL byte.")
    if not isinstance(plan.steps, tuple):
        raise TypeError(
            f"A plan's steps must be a tuple, got {type(plan.steps).__name__}."
        )
    if not plan.steps:
        raise SystemdBootError(
            "A plan for a non-bootable guest declares no steps. An empty plan "
            "cannot make a filesystem bootable, and the postcondition would "
            "fail after a build that did nothing."
        )

    for position, step in enumerate(plan.steps):
        label = f"step {position}"
        if not isinstance(step, BuildRun):
            raise TypeError(f"{label} must be a BuildRun, got {type(step).__name__}.")
        if not isinstance(step.argv, tuple):
            raise TypeError(
                f"{label}: argv must be a tuple, got {type(step.argv).__name__}."
            )
        if not step.argv:
            raise SystemdBootError(f"{label}: argv is empty.")
        for argument in step.argv:
            if not isinstance(argument, str):
                raise TypeError(
                    f"{label}: every argv item must be a str, got "
                    f"{type(argument).__name__}."
                )
            if "\x00" in argument:
                raise SystemdBootError(f"{label}: an argv item contains a NUL byte.")
    return plan


def render_derived_build_file(*, source_tag: str, plan: SystemdProvisionPlan) -> bytes:
    """The derived image's whole recipe, deterministically.

    Same tag and same plan produce the same bytes, in any process on any host
    -- there is no timestamp, no ordering by set iteration, and no host detail
    in it.

    ``RUN`` is emitted in JSON exec form, so an argv vector stays a vector.
    Joining it into a shell string would make ``&&`` in a package name a
    command separator, and would put the plan's data back under shell
    interpretation after :class:`BuildRun` deliberately took it out.

    Nothing about the task's own ``CMD``, ``ENTRYPOINT``, ``USER`` or
    ``WORKDIR`` appears here, and none of them is overridden. The derived
    image exists to change filesystem *contents* before export; its config is
    never read, because the source image's record remains the authority on
    what the task declared.
    """
    lines = [
        "# Generated by Titanium's Cella converter. Do not edit.",
        "# Its only purpose is to change filesystem contents before export.",
        f"FROM {source_tag}",
        # Explicit, because provisioning installs packages and the source
        # image may declare any USER at all -- including one that cannot.
        "USER 0",
    ]
    lines += [f"RUN {json.dumps(list(step.argv))}" for step in plan.steps]
    return ("\n".join(lines) + "\n").encode()


# ------------------------------------------------------------- preparation


def prepare_systemd_rootfs(
    *,
    source_tag: str,
    source_image_id: str,
    source_rootfs_tar: Path,
    work_dir: Path,
    plan_provisioning: PlanSystemdProvisioning,
    timeout_sec: float | None = None,
) -> PreparedSystemdRootfs:
    """Return a filesystem that provably boots systemd, or raise.

    When the source already boots systemd this does nothing at all: the
    planner is not called, no image is built, and the source tar is the final
    tar. Provisioning is not a step every conversion pays for.

    Otherwise the planner is called exactly once, its plan becomes a derived
    image built ``FROM`` the local source tag, and that image is exported and
    **re-probed**. A build that exits zero is not evidence: ``apt-get install``
    can succeed while leaving ``/sbin/init`` pointing somewhere else entirely.
    The postcondition is checked against the filesystem being shipped.

    Args:
        source_tag: The local tag of the task image the converter just built.
        source_image_id: That image's content id, reported unchanged when no
            provisioning is needed.
        source_rootfs_tar: Its already-exported filesystem.
        work_dir: Where the derived context and derived tar are written.
        plan_provisioning: The policy decision. Injected, never imported, so
            this module cannot acquire an opinion about package managers.
        timeout_sec: Applied to each podman invocation.

    Raises:
        SystemdBootError: when the provisioned filesystem still does not boot
            systemd, or when the archive cannot be read unambiguously.
        TypeError: when the planner returned a malformed plan.
        PodmanError: from the derived build or export.
    """
    source_info = probe_rootfs_tar(source_rootfs_tar)
    if source_info.systemd_bootable:
        return PreparedSystemdRootfs(
            rootfs_tar=source_rootfs_tar,
            source_info=source_info,
            final_info=source_info,
            strategy=STRATEGY_ALREADY_SYSTEMD,
            derived=False,
            recipe_bytes=None,
            boot_image_id=source_image_id,
        )

    plan = validate_provision_plan(plan_provisioning(source_info))
    recipe = render_derived_build_file(source_tag=source_tag, plan=plan)

    context_dir = work_dir / "systemd-context"
    context_dir.mkdir(parents=True, exist_ok=True)
    build_file = context_dir / "Containerfile"
    build_file.write_bytes(recipe)

    derived_tag = new_build_tag("titanium-cella-systemd")
    derived_tar = work_dir / "rootfs-systemd.tar"
    try:
        build_image(
            context_dir=context_dir,
            build_file=build_file,
            tag=derived_tag,
            timeout_sec=timeout_sec,
            # The parent is the local tag this conversion just built. There is
            # nothing to pull, and a registry round trip could resolve the
            # `FROM` to something other than the image just inspected.
            pull="never",
        )
        derived_record = parse_image_record(
            inspect_image(derived_tag, timeout_sec=timeout_sec)
        )
        export_rootfs_tar(
            image=derived_tag, dest_tar=derived_tar, timeout_sec=timeout_sec
        )
    finally:
        # Untag, never remove: the derived image's layers may be another
        # build's cache, exactly as for the source tag.
        untag_image(derived_tag)

    final_info = probe_rootfs_tar(derived_tar)
    if not final_info.systemd_bootable:
        raise SystemdBootError(
            f"Provisioning strategy {plan.strategy!r} completed successfully, "
            f"but the resulting filesystem still does not boot systemd: "
            f"/sbin/init resolves to {final_info.init_resolved_path!r} and the "
            f"systemd binary found is {final_info.systemd_path!r}. Refusing to "
            f"ship a filesystem whose PID 1 nothing chose."
        )

    return PreparedSystemdRootfs(
        rootfs_tar=derived_tar,
        source_info=source_info,
        final_info=final_info,
        strategy=plan.strategy,
        derived=True,
        recipe_bytes=recipe,
        boot_image_id=derived_record.image_id,
    )


__all__ = [
    "GUEST_INIT_PATH",
    "MAX_SYMLINK_HOPS",
    "OS_RELEASE_CANDIDATES",
    "STRATEGY_ALREADY_SYSTEMD",
    "SYSTEMD_BINARY_CANDIDATES",
    "BuildRun",
    "GuestOsInfo",
    "PlanSystemdProvisioning",
    "PreparedSystemdRootfs",
    "RootfsArchive",
    "SystemdBootError",
    "SystemdProvisionPlan",
    "plan_systemd_provisioning",
    "prepare_systemd_rootfs",
    "probe_archive",
    "probe_rootfs_tar",
    "render_derived_build_file",
    "validate_provision_plan",
]
