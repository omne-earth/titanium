"""The standard workload principal, and the guest account that backs it.

Two different things used to be one thing here, and conflating them was wrong.

*The principal* is what a host request selects. It is fixed: UID 1000, never
UID 0. Titanium's stable external name for it is ``titanium``, and that alias
is the vocabulary of the control surface -- it is not a claim about what any
guest's ``/etc/passwd`` says.

*The account* is the concrete ``/etc/passwd`` entry inside one task image that
carries UID 1000. A task may already ship one, under any name and with any
primary group:

    alice:x:1000:1234:...:/home/alice:/bin/bash

That account backs the principal as it stands. It is not renamed, its primary
GID is not changed, and no file it owns is touched -- rewriting a task's own
account to match Titanium's vocabulary would change the meaning of every file
in the image that references it.

The security invariant is this: the workload runs as UID 1000, its primary
group is never the root group, and root is never the standard workload
principal. Everything else about the account belongs to the image.

Group membership beyond the primary group is out of scope here, and remains a
requirement on whatever starts the workload: the process must be given exactly
the primary group resolved here, and must not inherit supplementary groups the
image happens to list for the account. Resolving an account neither grants nor
withholds them, and nothing in this module can stop a launcher from adding
them.

Nothing here reads an archive. The account rules operate on already-parsed
``passwd`` and ``group`` facts, so this module holds policy and no mechanics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: The workload's numeric identity. Not configurable: it is the invariant.
STANDARD_PRINCIPAL_UID = 1000

#: Titanium's external name for that principal, used by callers. An alias, not
#: an assertion about the guest's account database.
STANDARD_PRINCIPAL_ALIAS = "titanium"

#: What a synthetic account looks like when the image ships no UID 1000.
#: ``/bin/sh`` because it is the one shell a minimal image is sure to have.
SYNTHETIC_ACCOUNT_NAME = "titanium"
SYNTHETIC_ACCOUNT_HOME = "/home/titanium"
SYNTHETIC_ACCOUNT_SHELL = "/bin/sh"

_PASSWD_FIELDS = 7
_GROUP_FIELDS = 4


class IdentityError(ValueError):
    """The execution identity or the guest account could not be established."""


@dataclass(frozen=True)
class StandardGuestPrincipal:
    """What a host request resolves to.

    Attributes:
        alias: Titanium's name for the principal.
        uid: The numeric identity the workload runs as.
    """

    alias: str
    uid: int


STANDARD_GUEST_PRINCIPAL = StandardGuestPrincipal(
    alias=STANDARD_PRINCIPAL_ALIAS,
    uid=STANDARD_PRINCIPAL_UID,
)


@dataclass(frozen=True)
class PasswdEntry:
    """One ``/etc/passwd`` line."""

    name: str
    uid: int
    gid: int
    home: str
    shell: str


@dataclass(frozen=True)
class GroupEntry:
    """One ``/etc/group`` line, reduced to what account selection needs."""

    name: str
    gid: int


@dataclass(frozen=True)
class ResolvedGuestAccount:
    """The concrete account the workload will run as.

    Attributes:
        username: The account's own name. ``alice`` stays ``alice``.
        uid: Always :data:`STANDARD_PRINCIPAL_UID`.
        gid: The account's primary group. Taken from the image when the
            account already existed, never forced to 1000, and never 0.
        home: The account's home directory.
        shell: The account's login shell.
        synthetic: True when no UID 1000 existed and this account has to be
            created. False when an existing entry is being reused as-is.
    """

    username: str
    uid: int
    gid: int
    home: str
    shell: str
    synthetic: bool


@dataclass(frozen=True)
class GuestAccountPlan:
    """The account, plus the one group that may still need creating.

    Attributes:
        account: The account backing the standard principal.
        create_group: A group to add, or ``None`` when the account's primary
            group already exists. Only ever set on the synthetic path: an
            image's own group database is never edited to suit Titanium.
    """

    account: ResolvedGuestAccount
    create_group: GroupEntry | None


def resolve_standard_guest_user(
    *,
    requested_user: str | int | None,
) -> StandardGuestPrincipal:
    """Resolve a requested execution identity to the standard principal.

    Accepted, all meaning the same thing: ``None``, ``"titanium"``, ``"1000"``,
    and ``1000``. Everything else is refused, including ``"root"``, ``0`` and
    ``"0"``.

    The accepted set is deliberately not widened to the *account's* own name.
    A guest whose UID 1000 is ``alice`` is still addressed as ``titanium``,
    because the control surface names a principal and the image names an
    account; letting callers say ``alice`` would make the request's meaning
    depend on the image it happens to run against.

    Raises:
        TypeError: when *requested_user* is not a string, an integer, or None.
        IdentityError: for a bool, an empty string, a negative UID, or any
            identity other than the standard principal. There is no fallback:
            an unsupported identity fails rather than becoming UID 0.
    """
    if requested_user is None:
        return STANDARD_GUEST_PRINCIPAL

    # Checked before int: bool is an int subclass, and True would otherwise
    # read as UID 1.
    if isinstance(requested_user, bool):
        raise IdentityError("Boolean values are not valid guest users.")

    if not isinstance(requested_user, (str, int)):
        raise TypeError("User must be a username or numeric UID.")

    if isinstance(requested_user, str):
        normalized = requested_user.strip()
        if not normalized:
            raise IdentityError("User cannot be empty.")
        if normalized in (STANDARD_PRINCIPAL_ALIAS, str(STANDARD_PRINCIPAL_UID)):
            return STANDARD_GUEST_PRINCIPAL
        raise IdentityError(
            f"User {requested_user!r} is not permitted in the standard Cella guest."
        )

    if requested_user < 0:
        raise IdentityError("UID cannot be negative.")
    if requested_user == STANDARD_PRINCIPAL_UID:
        return STANDARD_GUEST_PRINCIPAL
    raise IdentityError(
        f"UID {requested_user} is not permitted in the standard Cella guest."
    )


def parse_passwd(text: str) -> tuple[PasswdEntry, ...]:
    """Parse ``/etc/passwd`` text into entries.

    Strict on purpose. libc resolves a duplicate or damaged database by
    first match; this refuses, because a file it cannot fully enumerate is a
    file in which it cannot prove which entry owns UID 1000.

    Raises:
        IdentityError: on any non-blank, non-comment line that is not seven
            colon-separated fields with a non-negative integer UID and GID.
    """
    return tuple(
        PasswdEntry(
            name=fields[0],
            uid=_number(fields[2], "UID", lineno, line),
            gid=_number(fields[3], "GID", lineno, line),
            home=fields[5],
            shell=fields[6],
        )
        for lineno, line, fields in _records(text, "passwd", _PASSWD_FIELDS)
    )


def parse_group(text: str) -> tuple[GroupEntry, ...]:
    """Parse ``/etc/group`` text into entries. Strict, as :func:`parse_passwd`."""
    return tuple(
        GroupEntry(name=fields[0], gid=_number(fields[2], "GID", lineno, line))
        for lineno, line, fields in _records(text, "group", _GROUP_FIELDS)
    )


def _records(text: str, what: str, count: int):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) != count:
            raise IdentityError(
                f"/etc/{what} line {lineno} has {len(fields)} fields, not "
                f"{count}: {line!r}. Refusing to guess which account owns "
                f"UID {STANDARD_PRINCIPAL_UID}."
            )
        yield lineno, line, fields


def _number(value: str, label: str, lineno: int, line: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise IdentityError(
            f"line {lineno} has a non-numeric {label} {value!r}: {line!r}."
        ) from None
    if number < 0:
        raise IdentityError(f"line {lineno} has a negative {label}: {line!r}.")
    return number


def resolve_guest_account(
    *,
    passwd: Sequence[PasswdEntry],
    groups: Sequence[GroupEntry] = (),
) -> GuestAccountPlan:
    """Decide which concrete account backs the standard principal.

    An existing UID 1000 is reused exactly as the image wrote it -- same
    name, same primary GID, same home and shell. Nothing is renamed and no
    ownership is rewritten.

    The one exception is a primary group of 0, which is refused rather than
    reused or corrected. UID 1000 is not UID 0, but the root group confers
    access to root-group-owned paths across the image on its own, so
    inheriting it would hand the workload privilege the principal is defined
    not to have. The GID is not rewritten and no replacement account is
    created: either would change what the image's own files mean.

    When no UID 1000 exists, a synthetic account is planned. Its primary group
    reuses an existing GID 1000 if there is one, and otherwise is created; an
    existing group is never renamed to suit Titanium.

    Raises:
        IdentityError: when the UID 1000 account's primary group is the root
            group, when two entries claim UID 1000, or when a synthetic
            account would collide with an existing name. Neither is resolved
            by picking one: both mean the concrete identity is ambiguous, and
            rewriting the image's own account database to break the tie is not
            something this resolution is allowed to do.
    """
    owners = [entry for entry in passwd if entry.uid == STANDARD_PRINCIPAL_UID]
    if len(owners) > 1:
        names = ", ".join(sorted(entry.name for entry in owners))
        raise IdentityError(
            f"Two or more passwd entries claim UID {STANDARD_PRINCIPAL_UID} "
            f"({names}). Refusing to pick one by position."
        )

    if owners:
        existing = owners[0]

        if existing.gid == 0:
            raise IdentityError(
                f"UID {STANDARD_PRINCIPAL_UID} account {existing.name!r} has "
                "primary GID 0. The standard workload principal must not inherit "
                "the root primary group."
            )

        return GuestAccountPlan(
            account=ResolvedGuestAccount(
                username=existing.name,
                uid=existing.uid,
                gid=existing.gid,
                home=existing.home,
                shell=existing.shell,
                synthetic=False,
            ),
            create_group=None,
        )

    name_holder = next(
        (entry for entry in passwd if entry.name == SYNTHETIC_ACCOUNT_NAME), None
    )
    if name_holder is not None:
        raise IdentityError(
            f"An account named {SYNTHETIC_ACCOUNT_NAME!r} already exists at UID "
            f"{name_holder.uid}, and UID {STANDARD_PRINCIPAL_UID} is free. "
            f"Rewriting that account would change what every file it owns "
            f"means, so the standard principal cannot be prepared here."
        )

    gid_owners = [entry for entry in groups if entry.gid == STANDARD_PRINCIPAL_UID]
    if len(gid_owners) > 1:
        names = ", ".join(sorted(entry.name for entry in gid_owners))
        raise IdentityError(
            f"Two or more groups claim GID {STANDARD_PRINCIPAL_UID} ({names}). "
            f"Refusing to pick one by position."
        )

    if gid_owners:
        # Reused by number, under whatever name the image gave it.
        create_group = None
        gid = gid_owners[0].gid
    else:
        name_collision = next(
            (entry for entry in groups if entry.name == SYNTHETIC_ACCOUNT_NAME), None
        )
        if name_collision is not None:
            raise IdentityError(
                f"A group named {SYNTHETIC_ACCOUNT_NAME!r} already exists at GID "
                f"{name_collision.gid}. Refusing to rename or reassign it."
            )
        gid = STANDARD_PRINCIPAL_UID
        create_group = GroupEntry(name=SYNTHETIC_ACCOUNT_NAME, gid=gid)

    return GuestAccountPlan(
        account=ResolvedGuestAccount(
            username=SYNTHETIC_ACCOUNT_NAME,
            uid=STANDARD_PRINCIPAL_UID,
            gid=gid,
            home=SYNTHETIC_ACCOUNT_HOME,
            shell=SYNTHETIC_ACCOUNT_SHELL,
            synthetic=True,
        ),
        create_group=create_group,
    )


__all__ = [
    "STANDARD_GUEST_PRINCIPAL",
    "STANDARD_PRINCIPAL_ALIAS",
    "STANDARD_PRINCIPAL_UID",
    "SYNTHETIC_ACCOUNT_HOME",
    "SYNTHETIC_ACCOUNT_NAME",
    "SYNTHETIC_ACCOUNT_SHELL",
    "GroupEntry",
    "GuestAccountPlan",
    "IdentityError",
    "PasswdEntry",
    "ResolvedGuestAccount",
    "StandardGuestPrincipal",
    "parse_group",
    "parse_passwd",
    "resolve_guest_account",
    "resolve_standard_guest_user",
]
