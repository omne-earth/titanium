"""The standard principal a request selects, and the account that backs it.

Two separate questions, tested separately: what a host may ask to run as, and
which /etc/passwd entry inside one image carries UID 1000. The first is fixed
vocabulary; the second belongs to the image.
"""

from __future__ import annotations

import pytest

from titanium.environments.cella.identity import (
    STANDARD_GUEST_PRINCIPAL,
    STANDARD_PRINCIPAL_ALIAS,
    STANDARD_PRINCIPAL_UID,
    SYNTHETIC_ACCOUNT_HOME,
    SYNTHETIC_ACCOUNT_NAME,
    SYNTHETIC_ACCOUNT_SHELL,
    GroupEntry,
    GuestAccountPlan,
    IdentityError,
    PasswdEntry,
    ResolvedGuestAccount,
    StandardGuestPrincipal,
    parse_group,
    parse_passwd,
    resolve_guest_account,
    resolve_standard_guest_user,
)

DEBIAN_PASSWD = """\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
"""

DEBIAN_GROUP = """\
root:x:0:
daemon:x:1:
nogroup:x:65534:
"""

ALICE = PasswdEntry(
    name="alice", uid=1000, gid=1234, home="/home/alice", shell="/bin/bash"
)
ROOT = PasswdEntry(name="root", uid=0, gid=0, home="/root", shell="/bin/bash")


# ------------------------------------------------------ the shape of the model

# The whole point of the split: a principal is an alias and a UID, and an
# account is a passwd row. Anything that conflates them is the bug this model
# was rewritten to remove.


def test_a_request_resolves_to_a_principal_not_an_account():
    resolved = resolve_standard_guest_user(requested_user=None)
    assert isinstance(resolved, StandardGuestPrincipal)
    assert not isinstance(resolved, ResolvedGuestAccount)


def test_the_principal_carries_no_account_fields():
    """It used to carry username, groupname and gid, and that was the defect.

    A principal that named a group would be asserting something about the
    image's account database that Titanium does not get to decide.
    """
    for field in ("username", "groupname", "gid", "home", "shell", "synthetic"):
        assert not hasattr(STANDARD_GUEST_PRINCIPAL, field), field


def test_account_resolution_returns_a_plan_around_an_account():
    plan = resolve_guest_account(passwd=[ALICE])
    assert isinstance(plan, GuestAccountPlan)
    assert isinstance(plan.account, ResolvedGuestAccount)


@pytest.mark.parametrize(
    "value",
    [
        STANDARD_GUEST_PRINCIPAL,
        PasswdEntry("alice", 1000, 1234, "/home/alice", "/bin/sh"),
        GroupEntry("staff", 1000),
    ],
)
def test_the_model_is_frozen(value):
    """A resolved identity is a decision, not a mutable holder."""
    with pytest.raises(AttributeError):
        value.uid = 4242


def test_the_resolved_account_and_its_plan_are_frozen():
    plan = resolve_guest_account(passwd=[ALICE])
    with pytest.raises(AttributeError):
        plan.account.uid = 0
    with pytest.raises(AttributeError):
        plan.create_group = GroupEntry("x", 1)


# ------------------------------------------------ what a host may ask to run as


@pytest.mark.parametrize(
    "requested", [None, "titanium", "1000", 1000, "  titanium  ", "  1000  "]
)
def test_every_spelling_of_the_principal_resolves_to_it(requested):
    assert resolve_standard_guest_user(requested_user=requested) is (
        STANDARD_GUEST_PRINCIPAL
    )


def test_the_principal_is_uid_1000_under_titaniums_own_alias():
    assert STANDARD_GUEST_PRINCIPAL.uid == 1000
    assert STANDARD_GUEST_PRINCIPAL.alias == "titanium"
    assert STANDARD_PRINCIPAL_UID == 1000
    assert STANDARD_PRINCIPAL_ALIAS == "titanium"


@pytest.mark.parametrize(
    "requested", ["root", 0, "0", "alice", 1234, -1, "", "   ", "Titanium", "1000x"]
)
def test_anything_else_is_refused(requested):
    with pytest.raises(IdentityError):
        resolve_standard_guest_user(requested_user=requested)


@pytest.mark.parametrize("requested", [True, False])
def test_a_bool_is_refused_before_it_can_read_as_uid_one(requested):
    with pytest.raises(IdentityError):
        resolve_standard_guest_user(requested_user=requested)


@pytest.mark.parametrize("requested", [1.5, [], {}, b"titanium"])
def test_an_unsupported_type_is_refused(requested):
    with pytest.raises(TypeError):
        resolve_standard_guest_user(requested_user=requested)


def test_nothing_ever_falls_back_to_root():
    for requested in ("root", 0, "0", "other-user", 2000):
        with pytest.raises(IdentityError):
            resolve_standard_guest_user(requested_user=requested)


def test_the_accounts_own_name_is_not_request_vocabulary():
    """A guest whose UID 1000 is `alice` is still addressed as `titanium`.

    The control surface names a principal; the image names an account. If
    callers could say `alice`, a request's meaning would depend on the image
    it happened to run against.
    """
    plan = resolve_guest_account(passwd=[ROOT, ALICE])
    assert plan.account.username == "alice"
    with pytest.raises(IdentityError):
        resolve_standard_guest_user(requested_user="alice")
    assert resolve_standard_guest_user(requested_user="titanium").uid == 1000


# ---------------------------------------------------------- reusing an account


def test_an_existing_uid_1000_is_reused_exactly():
    plan = resolve_guest_account(passwd=[ROOT, ALICE])
    account = plan.account
    assert account.username == "alice"
    assert account.uid == 1000
    assert account.gid == 1234
    assert account.home == "/home/alice"
    assert account.shell == "/bin/bash"
    assert account.synthetic is False
    assert plan.create_group is None


def test_a_reused_account_is_never_renamed_to_titanium():
    assert resolve_guest_account(passwd=[ALICE]).account.username != "titanium"


def test_a_reused_accounts_primary_gid_is_never_forced_to_1000():
    """Changing it would change what every file the group owns means."""
    assert resolve_guest_account(passwd=[ALICE]).account.gid == 1234


def test_a_reused_account_needs_no_group_created():
    """The image's own group database is not edited to suit Titanium."""
    plan = resolve_guest_account(passwd=[ALICE], groups=[GroupEntry("alice", 1234)])
    assert plan.create_group is None


def test_an_unusual_but_non_root_account_is_still_reused():
    """Odd is fine. Only the root group is not."""
    odd = PasswdEntry(
        name="app", uid=1000, gid=4321, home="/srv/app", shell="/usr/sbin/nologin"
    )
    plan = resolve_guest_account(passwd=[ROOT, odd])
    account = plan.account
    assert account.username == "app"
    assert account.uid == 1000
    assert account.gid == 4321
    assert account.home == "/srv/app"
    assert account.shell == "/usr/sbin/nologin"
    assert account.synthetic is False
    assert plan.create_group is None


def test_a_uid_1000_account_in_the_root_group_is_refused():
    """UID 1000 is not UID 0, but the root group grants access on its own."""
    rooted = PasswdEntry(name="app", uid=1000, gid=0, home="/", shell="/bin/sh")
    with pytest.raises(IdentityError):
        resolve_guest_account(passwd=[ROOT, rooted])


def test_the_root_group_refusal_names_the_problem():
    rooted = PasswdEntry(name="app", uid=1000, gid=0, home="/", shell="/bin/sh")
    with pytest.raises(IdentityError, match="root primary group"):
        resolve_guest_account(passwd=[ROOT, rooted])
    with pytest.raises(IdentityError, match="primary GID 0"):
        resolve_guest_account(passwd=[ROOT, rooted])


def test_the_root_group_account_is_neither_rewritten_nor_replaced():
    """Refusal, not correction: changing its GID or shadowing it with a new
    account would both change what the image's own files mean."""
    rooted = PasswdEntry(name="app", uid=1000, gid=0, home="/", shell="/bin/sh")
    with pytest.raises(IdentityError):
        resolve_guest_account(passwd=[ROOT, rooted])
    # And it is refused even when a free GID 1000 exists to move it to.
    with pytest.raises(IdentityError):
        resolve_guest_account(passwd=[ROOT, rooted], groups=[GroupEntry("app", 1000)])


@pytest.mark.parametrize("gid", [1, 100, 1000, 4321, 65534])
def test_every_nonzero_primary_group_is_accepted_as_written(gid):
    entry = PasswdEntry(name="app", uid=1000, gid=gid, home="/srv/app", shell="/bin/sh")
    assert resolve_guest_account(passwd=[ROOT, entry]).account.gid == gid


# ------------------------------------------------------- creating one instead


def test_no_uid_1000_yields_a_synthetic_account():
    plan = resolve_guest_account(passwd=parse_passwd(DEBIAN_PASSWD))
    account = plan.account
    assert account.username == SYNTHETIC_ACCOUNT_NAME
    assert account.uid == 1000
    assert account.gid == 1000
    assert account.home == SYNTHETIC_ACCOUNT_HOME
    assert account.shell == SYNTHETIC_ACCOUNT_SHELL
    assert account.synthetic is True
    assert plan.create_group == GroupEntry(name="titanium", gid=1000)


def test_an_existing_gid_1000_is_reused_under_its_own_name():
    """Not renamed to titanium merely because Titanium wants that GID."""
    plan = resolve_guest_account(
        passwd=parse_passwd(DEBIAN_PASSWD), groups=[GroupEntry("staff", 1000)]
    )
    assert plan.account.gid == 1000
    assert plan.account.synthetic is True
    assert plan.create_group is None


def test_a_group_is_created_only_when_gid_1000_is_free():
    plan = resolve_guest_account(
        passwd=parse_passwd(DEBIAN_PASSWD), groups=parse_group(DEBIAN_GROUP)
    )
    assert plan.create_group == GroupEntry(name="titanium", gid=1000)


def test_an_empty_account_database_still_yields_the_synthetic_account():
    plan = resolve_guest_account(passwd=[])
    assert plan.account.uid == 1000
    assert plan.account.synthetic is True


# ---------------------------------------------------------------- refusals


def test_two_entries_claiming_uid_1000_are_refused():
    """libc would take the first match. That is a guess, so this refuses."""
    bob = PasswdEntry(name="bob", uid=1000, gid=1000, home="/home/bob", shell="/bin/sh")
    with pytest.raises(IdentityError, match="claim UID 1000"):
        resolve_guest_account(passwd=[ALICE, bob])


def test_the_duplicate_refusal_names_both_accounts():
    bob = PasswdEntry(name="bob", uid=1000, gid=1000, home="/home/bob", shell="/bin/sh")
    with pytest.raises(IdentityError, match="alice, bob"):
        resolve_guest_account(passwd=[ALICE, bob])


def test_an_existing_titanium_name_at_another_uid_is_refused_not_rewritten():
    impostor = PasswdEntry(
        name="titanium", uid=1500, gid=1500, home="/home/titanium", shell="/bin/sh"
    )
    with pytest.raises(IdentityError, match="already exists at UID 1500"):
        resolve_guest_account(passwd=[ROOT, impostor])


def test_an_existing_titanium_group_at_another_gid_is_refused_not_reassigned():
    with pytest.raises(IdentityError, match="already exists at GID 1500"):
        resolve_guest_account(
            passwd=parse_passwd(DEBIAN_PASSWD), groups=[GroupEntry("titanium", 1500)]
        )


def test_two_groups_claiming_gid_1000_are_refused():
    with pytest.raises(IdentityError, match="claim GID 1000"):
        resolve_guest_account(
            passwd=parse_passwd(DEBIAN_PASSWD),
            groups=[GroupEntry("staff", 1000), GroupEntry("users", 1000)],
        )


def test_a_titanium_name_collision_does_not_block_a_reused_account():
    """The name only matters when an account has to be created."""
    impostor = PasswdEntry(
        name="titanium", uid=1500, gid=1500, home="/home/titanium", shell="/bin/sh"
    )
    plan = resolve_guest_account(passwd=[ALICE, impostor])
    assert plan.account.username == "alice"


# ------------------------------------------------------------------ parsing


def test_a_real_passwd_file_parses():
    entries = parse_passwd(DEBIAN_PASSWD)
    assert [entry.name for entry in entries] == ["root", "daemon", "nobody"]
    assert entries[0] == PasswdEntry("root", 0, 0, "/root", "/bin/bash")


def test_blank_lines_and_comments_are_skipped():
    assert parse_passwd("# a comment\n\n  \nroot:x:0:0:root:/root:/bin/sh\n") == (
        PasswdEntry("root", 0, 0, "/root", "/bin/sh"),
    )


def test_a_group_file_parses():
    assert parse_group(DEBIAN_GROUP)[0] == GroupEntry("root", 0)


def test_an_empty_gecos_or_shell_is_preserved():
    entry = parse_passwd("svc:x:1000:1000::/nonexistent:\n")[0]
    assert entry.home == "/nonexistent"
    assert entry.shell == ""


@pytest.mark.parametrize(
    "text",
    [
        "alice:x:1000\n",
        "alice:x:1000:1234:x:/home/alice:/bin/sh:extra\n",
        "alice:x:notanumber:1234:x:/home/alice:/bin/sh\n",
        "alice:x:1000:notanumber:x:/home/alice:/bin/sh\n",
        "alice:x:-5:1234:x:/home/alice:/bin/sh\n",
    ],
)
def test_a_malformed_passwd_line_is_refused(text):
    """Stricter than libc, deliberately.

    A file that cannot be fully enumerated is a file in which the owner of
    UID 1000 cannot be proven unique, so parsing refuses rather than skipping
    the line and reporting a possibly-wrong account.
    """
    with pytest.raises(IdentityError):
        parse_passwd(text)


def test_a_malformed_line_is_refused_even_beside_a_good_uid_1000():
    text = "alice:x:1000:1234:x:/home/alice:/bin/sh\nbroken:x:oops\n"
    with pytest.raises(IdentityError):
        parse_passwd(text)


def test_a_malformed_group_line_is_refused():
    with pytest.raises(IdentityError):
        parse_group("staff:x:1000\n")


def test_the_literal_alice_line_parses_and_resolves_end_to_end():
    """The exact row a task image would ship, GECOS field and all."""
    entries = parse_passwd(
        "root:x:0:0:root:/root:/bin/bash\n"
        "alice:x:1000:1234:Alice:/home/alice:/bin/bash\n"
    )
    plan = resolve_guest_account(passwd=entries)
    account = plan.account
    assert account.username == "alice"
    assert account.uid == 1000
    assert account.gid == 1234
    assert account.home == "/home/alice"
    assert account.shell == "/bin/bash"
    assert account.synthetic is False
    assert plan.create_group is None
    # And the host still addresses it by the alias, not by "alice".
    assert resolve_standard_guest_user(requested_user="titanium").uid == account.uid


# -------------------------------------------------------------- the invariant


@pytest.mark.parametrize(
    "passwd",
    [
        [ROOT, ALICE],
        [ROOT],
        [],
        [PasswdEntry("app", 1000, 4321, "/srv/app", "/bin/sh")],
    ],
)
def test_the_resolved_account_is_always_uid_1000_and_never_root(passwd):
    account = resolve_guest_account(passwd=passwd).account
    assert account.uid == STANDARD_PRINCIPAL_UID
    assert account.uid != 0
    assert account.gid != 0
