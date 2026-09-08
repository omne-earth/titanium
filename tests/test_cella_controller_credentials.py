"""The controller must become the standard principal before it reads a byte.

No unit directive can subtract the supplementary groups an image's account
database confers (``systemd.exec(5)``: ``SupplementaryGroups=`` "does not
override, but extends"), so the controller sheds them itself. These tests prove
the result against the kernel rather than against the syscalls' return values.

The real drop runs inside a throwaway user namespace, so the suite never needs
to be root. When the namespace cannot be created the test says so by name
instead of passing quietly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CRATE = Path(__file__).resolve().parent.parent / "native" / "titanium-controller"
BINARY = CRATE / "target" / "debug" / "titanium-controller"


def _blockers() -> list[str]:
    blockers = []
    if shutil.which("cargo") is None:
        blockers.append("cargo is not on PATH")
    if shutil.which("unshare") is None:
        blockers.append("unshare(1) is not on PATH")
    return blockers


@pytest.fixture(scope="module")
def controller() -> Path:
    blockers = _blockers()
    if blockers:
        pytest.skip("credential bootstrap test not runnable: " + "; ".join(blockers))
    built = subprocess.run(
        ["cargo", "build", "--offline", "-q", "--bin", "titanium-controller"],
        cwd=CRATE,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(
            f"the controller does not build offline: {built.stderr.strip()[:300]}"
        )

    # A namespace with more than one uid mapped, so a drop to 1000 is possible.
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--map-auto", "--", "true"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            "unshare --map-auto is unavailable (needs newuidmap and a delegated "
            f"/etc/subuid range): {probe.stderr.decode(errors='replace').strip()[:200]}"
        )
    return BINARY


def drop_in_namespace(controller: Path, uid: int, gid: int):
    return subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--map-auto",
            "--",
            str(controller),
            "--selftest-credentials",
            str(uid),
            str(gid),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def parse(stdout: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in stdout.splitlines()
        if "=" in line and line != "OK"
    )


# ------------------------------------------------------------ the drop itself


def test_the_controller_becomes_exactly_the_standard_principal(controller):
    """Every property, read back from the kernel after the drop.

    The namespace this runs in starts as uid 0 *with* supplementary groups
    including GID 0, so the empty group list below is a real subtraction and
    not an accident of the starting state.
    """
    result = drop_in_namespace(controller, 1000, 1234)
    assert result.returncode == 0, result.stdout + result.stderr
    fields = parse(result.stdout)

    # All four of real, effective, saved and filesystem: no saved id remains
    # to return to, which is what makes the drop irreversible.
    assert fields["uid"] == "[1000, 1000, 1000, 1000]"
    assert fields["gid"] == "[1234, 1234, 1234, 1234]"
    assert fields["groups"] == "[]"
    assert fields["no_new_privs"] == "true"
    assert fields["dumpable"] == "false"
    assert result.stdout.strip().endswith("OK")


def test_the_primary_group_is_never_the_root_group(controller):
    result = drop_in_namespace(controller, 1000, 1234)
    gid = result.stdout.splitlines()[1]
    assert "0," not in gid and gid != "gid=[0, 0, 0, 0]"


@pytest.mark.parametrize("gid", [1, 100, 1234, 65533])
def test_any_non_root_primary_group_is_honoured_exactly(controller, gid):
    """The image's own GID is preserved, not replaced with 1000."""
    result = drop_in_namespace(controller, 1000, gid)
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse(result.stdout)["gid"] == f"[{gid}, {gid}, {gid}, {gid}]"


# ------------------------------------------------------------------ refusals


def test_the_root_group_is_refused_as_a_primary_group(controller):
    result = drop_in_namespace(controller, 1000, 0)
    assert result.returncode != 0
    assert "root group" in result.stdout


@pytest.mark.parametrize("uid", [0, 1, 1001, 65534])
def test_only_uid_1000_is_accepted(controller, uid):
    result = drop_in_namespace(controller, uid, 1234)
    assert result.returncode != 0
    assert "uid must be 1000" in result.stdout


def test_an_unprivileged_controller_refuses_rather_than_pretending(controller):
    """Outside the namespace the drop cannot work, and must not claim to."""
    result = subprocess.run(
        [str(controller), "--selftest-credentials", "1000", "1234"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout.startswith("FAILED")
    assert "OK" not in result.stdout


def test_the_controller_does_not_serve_without_the_bootstrap(controller):
    """Running it with no arguments must not start a request loop."""
    result = subprocess.run(
        [str(controller)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
