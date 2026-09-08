"""The boot layer's datatypes and what validation refuses.

Mostly the mechanical seam: whether a set of entries is installable into a
guest filesystem at all. What the entries should *be* is render_boot_layer's
decision, and as of A2 the answer is "none" -- systemd is put in place by the
provisioning stage, and A3 is what adds the controller.
"""

from __future__ import annotations

import pytest

from titanium.environments.cella.boot_layer import (
    MAX_GUEST_FILE_MODE,
    BootLayer,
    BootLayerError,
    BootLayerInputs,
    GuestFile,
    GuestSymlink,
    boot_layer_digest,
    render_boot_layer,
    validate_boot_layer,
)
from titanium.environments.cella.image_config import parse_image_record

INSPECT = [
    {
        "Id": "sha256:deadbeef",
        "Digest": "sha256:cafebabe",
        "RepoDigests": [],
        "Config": {"Entrypoint": ["/app/run.sh"], "WorkingDir": "/app"},
    }
]


def a_file(**overrides) -> GuestFile:
    fields = {
        "path": "/etc/systemd/system/titanium.service",
        "contents": b"[Unit]\n",
        "mode": 0o644,
        "uid": 0,
        "gid": 0,
    }
    fields.update(overrides)
    return GuestFile(**fields)


def a_symlink(**overrides) -> GuestSymlink:
    fields = {
        "path": "/etc/systemd/system/default.target",
        "target": "multi-user.target",
        "uid": 0,
        "gid": 0,
    }
    fields.update(overrides)
    return GuestSymlink(**fields)


def layer_of(*entries) -> BootLayer:
    return BootLayer(entries=tuple(entries))


# ------------------------------------------------------------------ datatypes


def test_a_boot_layer_carries_files_and_symlinks_together():
    layer = layer_of(a_file(), a_symlink())
    assert validate_boot_layer(layer) is layer
    assert isinstance(layer.entries, tuple)


def test_the_entries_are_ordered_and_the_order_survives():
    first = a_file(path="/etc/one")
    second = a_file(path="/etc/two")
    layer = validate_boot_layer(layer_of(first, second))
    assert layer.entries == (first, second)


def test_the_datatypes_are_frozen():
    with pytest.raises(AttributeError):
        a_file().path = "/elsewhere"
    with pytest.raises(AttributeError):
        a_symlink().target = "elsewhere"
    with pytest.raises(AttributeError):
        layer_of().entries = ()


def test_a_symlink_carries_no_mode():
    """Linux ignores a symlink's permission bits; recording one would lie."""
    assert not hasattr(a_symlink(), "mode")


def test_the_inputs_carry_the_image_and_the_explicit_user_separately():
    """Config.User must never become the runtime-user decision by default."""
    record = parse_image_record(INSPECT)
    inputs = BootLayerInputs(image=record, agent_user="app", agent_install_applied=True)
    assert inputs.image.image_id == "sha256:deadbeef"
    assert inputs.image.config["Entrypoint"] == ["/app/run.sh"]
    assert inputs.agent_user == "app"
    assert inputs.agent_install_applied is True


# ------------------------------------------------------------ the empty layer


def test_a_layer_that_contributes_nothing_is_structurally_valid():
    """Whether it is *correct* is a boot-policy question, not a shape one."""
    layer = layer_of()
    assert validate_boot_layer(layer) is layer


# ----------------------------------------------------------- structural types


def test_something_that_is_not_a_boot_layer_is_refused():
    with pytest.raises(TypeError, match="must be a BootLayer"):
        validate_boot_layer([a_file()])


def test_entries_that_are_not_a_tuple_are_refused():
    with pytest.raises(TypeError, match="must be a tuple"):
        validate_boot_layer(BootLayer(entries=[a_file()]))


def test_an_entry_of_an_unknown_kind_is_refused():
    with pytest.raises(TypeError, match="GuestFile or GuestSymlink"):
        validate_boot_layer(BootLayer(entries=("/etc/passwd",)))


# -------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "cannot be empty"),
        ("etc/hostname", "is not absolute"),
        ("./etc/hostname", "is not absolute"),
        ("/etc/host\x00name", "NUL byte"),
        ("/", "guest root"),
        ("/etc/../etc/hostname", "'..' component"),
        ("/..", "'..' component"),
        ("/etc/..", "'..' component"),
    ],
)
def test_an_unusable_guest_path_is_refused(path, message):
    with pytest.raises(BootLayerError, match=message):
        validate_boot_layer(layer_of(a_file(path=path)))


def test_a_path_rule_applies_to_symlinks_too():
    with pytest.raises(BootLayerError, match="is not absolute"):
        validate_boot_layer(layer_of(a_symlink(path="etc/rc.local")))


def test_a_path_that_is_not_a_string_is_refused():
    with pytest.raises(TypeError, match="guest path must be a str"):
        validate_boot_layer(layer_of(a_file(path=b"/etc/hostname")))


# ------------------------------------------------- one canonical spelling only


@pytest.mark.parametrize(
    "alias",
    [
        "/sbin//init",
        "/sbin/./init",
        "/sbin/init/",
        "//sbin/init",
        "/./sbin/init",
        "///sbin///init",
        "/etc/systemd/system/",
    ],
)
def test_a_non_canonical_spelling_of_a_destination_is_refused(alias):
    """Every one of these names a file some other entry could name plainly."""
    with pytest.raises(BootLayerError, match="not canonical"):
        validate_boot_layer(layer_of(a_file(path=alias)))


def test_the_canonical_spelling_of_that_destination_is_accepted():
    layer = layer_of(a_file(path="/sbin/init"))
    assert validate_boot_layer(layer) is layer


def test_the_canonical_rule_applies_to_symlink_destinations_too():
    with pytest.raises(BootLayerError, match="not canonical"):
        validate_boot_layer(layer_of(a_symlink(path="/etc/systemd//system/x.target")))


def test_an_alias_cannot_smuggle_a_duplicate_past_the_collision_check():
    """Canonical spelling is what makes a string comparison a sound check.

    /sbin/init and /sbin//init are one destination. Refusing the alias is what
    stops two entries claiming it while comparing as different strings.
    """
    with pytest.raises(BootLayerError, match="not canonical"):
        validate_boot_layer(
            layer_of(a_file(path="/sbin/init"), a_file(path="/sbin//init"))
        )


# ------------------------------------------------- but not of symlink targets


@pytest.mark.parametrize(
    "target",
    ["multi-user.target", "../multi-user.target", "./x.target", "a//b", "x/"],
)
def test_a_symlink_target_is_not_held_to_the_canonical_rule(target):
    """Relative targets are how normal systemd enablement links are written.

    The rule exists to keep one destination from having several spellings. A
    target is not a destination -- it is the string the link stores, and
    rewriting it would change where the link points.
    """
    layer = layer_of(a_symlink(target=target))
    assert validate_boot_layer(layer) is layer


def test_a_dotdot_inside_a_component_is_not_a_dotdot_component():
    """'..' is refused as a path component, not as a substring."""
    layer = layer_of(a_file(path="/etc/..hidden"), a_file(path="/etc/a..b"))
    assert validate_boot_layer(layer) is layer


# --------------------------------------------------------- duplicate claims


def test_two_entries_claiming_one_destination_are_refused():
    with pytest.raises(BootLayerError, match="Two boot entries claim"):
        validate_boot_layer(layer_of(a_file(path="/etc/x"), a_file(path="/etc/x")))


def test_a_file_and_a_symlink_claiming_one_destination_are_refused():
    with pytest.raises(BootLayerError, match="Two boot entries claim"):
        validate_boot_layer(layer_of(a_file(path="/etc/x"), a_symlink(path="/etc/x")))


# ------------------------------------------------------------------ contents


def test_file_contents_that_are_not_bytes_are_refused():
    with pytest.raises(TypeError, match="contents must be bytes"):
        validate_boot_layer(layer_of(a_file(contents="[Unit]\n")))


def test_empty_file_contents_are_allowed():
    """An empty /etc/machine-id is a real thing to place, so shape says yes."""
    layer = layer_of(a_file(contents=b""))
    assert validate_boot_layer(layer) is layer


# ---------------------------------------------------------------------- mode


@pytest.mark.parametrize("mode", [0, 0o644, 0o755, 0o4755, MAX_GUEST_FILE_MODE])
def test_every_posix_mode_is_accepted(mode):
    layer = layer_of(a_file(mode=mode))
    assert validate_boot_layer(layer) is layer


@pytest.mark.parametrize("mode", [-1, MAX_GUEST_FILE_MODE + 1, 0o10000])
def test_a_mode_outside_the_posix_range_is_refused(mode):
    with pytest.raises(BootLayerError, match="outside 0o0000"):
        validate_boot_layer(layer_of(a_file(mode=mode)))


def test_a_bool_mode_is_refused_by_name():
    """isinstance(True, int) is true, so True would otherwise be mode 0o1."""
    with pytest.raises(BootLayerError, match="not a bool"):
        validate_boot_layer(layer_of(a_file(mode=True)))


def test_a_mode_that_is_not_an_int_is_refused():
    with pytest.raises(TypeError, match="mode must be an int"):
        validate_boot_layer(layer_of(a_file(mode="0644")))


# ------------------------------------------------------------------ uid / gid


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_a_negative_owner_is_refused(field):
    with pytest.raises(BootLayerError, match="cannot be negative"):
        validate_boot_layer(layer_of(a_file(**{field: -1})))


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_a_bool_owner_is_refused_by_name(field):
    with pytest.raises(BootLayerError, match="not a bool"):
        validate_boot_layer(layer_of(a_file(**{field: True})))


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_an_owner_that_is_not_an_int_is_refused(field):
    """Numeric because the guest's NSS database is the image's, not the host's."""
    with pytest.raises(TypeError, match="must be an int"):
        validate_boot_layer(layer_of(a_file(**{field: "root"})))


@pytest.mark.parametrize("field", ["uid", "gid"])
def test_owner_rules_apply_to_symlinks_too(field):
    with pytest.raises(BootLayerError, match="cannot be negative"):
        validate_boot_layer(layer_of(a_symlink(**{field: -1})))


def test_a_high_numeric_owner_is_accepted_here():
    """The sub-uid ceiling is the rootfs builder's to enforce, not this one's."""
    layer = layer_of(a_file(uid=1234, gid=5678))
    assert validate_boot_layer(layer) is layer


# ------------------------------------------------------------ symlink target


def test_a_relative_symlink_target_is_allowed():
    """How a systemd unit directory refers to its neighbours."""
    layer = layer_of(a_symlink(target="../multi-user.target"))
    assert validate_boot_layer(layer) is layer


def test_an_absolute_symlink_target_is_allowed():
    layer = layer_of(a_symlink(target="/usr/lib/systemd/systemd"))
    assert validate_boot_layer(layer) is layer


def test_an_empty_symlink_target_is_refused():
    with pytest.raises(BootLayerError, match="target cannot be empty"):
        validate_boot_layer(layer_of(a_symlink(target="")))


def test_a_symlink_target_with_a_nul_is_refused():
    with pytest.raises(BootLayerError, match="NUL byte"):
        validate_boot_layer(layer_of(a_symlink(target="multi\x00user.target")))


def test_a_symlink_target_that_is_not_a_string_is_refused():
    with pytest.raises(TypeError, match="target must be a str"):
        validate_boot_layer(layer_of(a_symlink(target=b"multi-user.target")))


# ---------------------------------------------------------- nothing repaired


def test_validation_returns_the_same_object_it_was_given():
    """Nothing is normalized, defaulted, or repaired on the way through."""
    layer = layer_of(a_file(), a_symlink())
    assert validate_boot_layer(layer) is layer


def test_a_bad_entry_is_refused_even_behind_good_ones():
    with pytest.raises(BootLayerError, match="is not absolute"):
        validate_boot_layer(
            layer_of(a_file(path="/etc/one"), a_symlink(), a_file(path="etc/two"))
        )


# ------------------------------------------------------------ the human seam


def test_the_production_boot_layer_is_empty():
    """Correct, not unfinished.

    The distro's own systemd is PID 1, and it gets there by the provisioning
    stage changing the filesystem before it is exported -- not by a file
    dropped on top afterwards. There is nothing for this layer to contribute
    until A3 adds the controller.
    """
    record = parse_image_record(INSPECT)
    inputs = BootLayerInputs(image=record, agent_user=None, agent_install_applied=False)
    assert render_boot_layer(inputs) == BootLayer(entries=())


def test_the_empty_production_layer_is_itself_installable():
    record = parse_image_record(INSPECT)
    inputs = BootLayerInputs(image=record, agent_user="app", agent_install_applied=True)
    layer = render_boot_layer(inputs)
    assert validate_boot_layer(layer) is layer


def test_the_production_layer_places_no_init_and_no_unit():
    """Nothing here decides what boots. Placing a unit would be a boot policy."""
    record = parse_image_record(INSPECT)
    inputs = BootLayerInputs(image=record, agent_user=None, agent_install_applied=False)
    assert render_boot_layer(inputs).entries == ()


# ------------------------------------------------------------------ provenance

# Everything a layer would place has to reach the digest, because the digest is
# what stops a published artifact answering for a layer it was not built from.


def test_the_same_layer_always_digests_the_same():
    layer = layer_of(a_file(), a_symlink())
    assert boot_layer_digest(layer) == boot_layer_digest(layer)
    assert boot_layer_digest(layer) == boot_layer_digest(
        layer_of(a_file(), a_symlink())
    )


def test_the_digest_is_64_lowercase_hex():
    import re

    assert re.fullmatch(r"[0-9a-f]{64}", boot_layer_digest(layer_of(a_file())))


def test_one_content_byte_changes_the_digest():
    """A controller binary that differs by a byte is a different layer."""
    before = layer_of(a_file(contents=b"\x00" * 32))
    after = layer_of(a_file(contents=b"\x00" * 31 + b"\x01"))
    assert boot_layer_digest(before) != boot_layer_digest(after)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/etc/systemd/system/other.service"},
        {"contents": b"[Unit]\nDescription=x\n"},
        {"mode": 0o600},
        {"uid": 1000},
        {"gid": 1000},
    ],
)
def test_every_file_field_reaches_the_digest(change):
    assert boot_layer_digest(layer_of(a_file())) != boot_layer_digest(
        layer_of(a_file(**change))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/etc/other.target"},
        {"target": "rescue.target"},
        {"uid": 1},
        {"gid": 1},
    ],
)
def test_every_symlink_field_reaches_the_digest(change):
    assert boot_layer_digest(layer_of(a_symlink())) != boot_layer_digest(
        layer_of(a_symlink(**change))
    )


def test_entry_order_is_part_of_the_digest():
    """Order is placement order, so two orderings are two layers."""
    one = a_file(path="/etc/one")
    two = a_file(path="/etc/two")
    assert boot_layer_digest(layer_of(one, two)) != boot_layer_digest(
        layer_of(two, one)
    )


def test_adding_an_entry_changes_the_digest():
    assert boot_layer_digest(layer_of(a_file())) != boot_layer_digest(
        layer_of(a_file(), a_symlink())
    )
    assert boot_layer_digest(layer_of()) != boot_layer_digest(layer_of(a_file()))


def test_field_boundaries_cannot_be_forged():
    """Length prefixes: no value can imitate the start of the next field."""
    first = layer_of(a_file(path="/etc/ab", contents=b"c"))
    second = layer_of(a_file(path="/etc/a", contents=b"bc"))
    assert boot_layer_digest(first) != boot_layer_digest(second)


def test_an_invalid_layer_has_no_digest():
    with pytest.raises(BootLayerError):
        boot_layer_digest(layer_of(a_file(path="etc/relative")))
