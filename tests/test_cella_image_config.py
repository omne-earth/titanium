"""The image record is preserved, not interpreted.

The point of these tests is what is *not* dropped. A parser with a fixed field
list would silently lose HEALTHCHECK (which podman reports at the record's top
level, and only for a docker-format image) and any field a newer podman adds.
A lost field cannot be honored or refused -- it is silently ignored, which is
the failure the shim's OCI contract exists to prevent.
"""

from __future__ import annotations

import pytest

from titanium.environments.cella.image_config import parse_image_record

RECORD = {
    "Id": "c550cefd008c",
    "Digest": "sha256:95765f24",
    "RepoDigests": ["localhost/x@sha256:95765f24"],
    "Healthcheck": {"Test": ["CMD-SHELL", "/app/run.sh"], "Interval": 5000000000},
    "Config": {
        "User": "1234:5678",
        "Env": ["PATH=/usr/bin", "FOO=bar"],
        "Entrypoint": ["/app/run.sh"],
        "Cmd": ["x"],
        "WorkingDir": "/app",
        "Volumes": {"/data": {}},
        "ExposedPorts": {"8080/tcp": {}},
        "StopSignal": "SIGTERM",
        "Labels": {"a": "b"},
    },
    "SomeFieldPodmanAddedLater": {"nested": True},
}


def test_single_element_list_is_accepted():
    assert parse_image_record([RECORD]).image_id == "c550cefd008c"


def test_bare_object_is_accepted():
    assert parse_image_record(RECORD).image_id == "c550cefd008c"


def test_identity_fields_are_carried():
    record = parse_image_record([RECORD])
    assert record.digest == "sha256:95765f24"
    assert record.repo_digests == ("localhost/x@sha256:95765f24",)


def test_the_whole_record_survives_including_unknown_fields():
    record = parse_image_record([RECORD])
    assert record.inspect["SomeFieldPodmanAddedLater"] == {"nested": True}
    assert "SomeFieldPodmanAddedLater" in record.record_keys()


def test_healthcheck_is_reachable_even_though_it_is_not_in_config():
    record = parse_image_record([RECORD])
    assert "Healthcheck" not in record.config
    assert record.inspect["Healthcheck"]["Test"] == ["CMD-SHELL", "/app/run.sh"]


def test_config_fields_are_not_filtered_or_renamed():
    record = parse_image_record([RECORD])
    assert record.config == RECORD["Config"]
    assert record.config_keys() == tuple(sorted(RECORD["Config"]))


def test_an_absent_config_is_refused():
    """No empty stand-in: it reads exactly like an image that declared nothing."""
    with pytest.raises(ValueError, match="'Config' is absent"):
        parse_image_record({"Id": "abc"})


def test_a_non_object_config_is_refused():
    with pytest.raises(ValueError, match="not an object"):
        parse_image_record({"Id": "abc", "Config": "surprise"})
    with pytest.raises(ValueError, match="not an object"):
        parse_image_record({"Id": "abc", "Config": ["surprise"]})


def test_an_empty_but_real_config_is_valid():
    """An image that declared nothing is a different thing from an unreadable one."""
    record = parse_image_record({"Id": "abc", "Config": {}})
    assert record.config == {}
    assert record.config_keys() == ()


def test_the_top_level_view_cannot_be_mutated():
    record = parse_image_record([RECORD])
    with pytest.raises(TypeError):
        record.inspect["Id"] = "tampered"


def test_more_than_one_record_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        parse_image_record([RECORD, RECORD])


def test_empty_result_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        parse_image_record([])


def test_missing_id_is_refused():
    with pytest.raises(ValueError, match="'Id'"):
        parse_image_record({"Config": {}})


def test_non_object_record_is_refused():
    with pytest.raises(ValueError, match="not an object"):
        parse_image_record(["just-a-string"])
