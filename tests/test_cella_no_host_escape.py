"""Two laws of the Cella converter, checked statically.

Narrow on purpose. This is not a policy scanner; it is two invariants that a
convenient patch could break silently, where the result would look like
working code.

**No host-side reader for a Cella disk.** The only sanctioned view into a
stopped machine is Cella's own ``inspect`` (``crates/cella-universe``). That
verb is currently interactive and lab-only, so the tempting shortcut is to read
``~/.cella/machines/<vm>/disk.img`` from the host -- it is a plain ext4 file the
invoking user owns, and ``debugfs`` would read it unprivileged. Doing so would
bypass Cella's evidence path entirely. The gap stays a named Cella gap.

**No runtime container engine.** Podman builds and flattens; then it is gone.
Cella boots a kernel and an ext4 (``docs/integration/TITANIUM.md``, rule 3).

The scan reads *code*, not prose: each module is parsed and re-emitted with its
docstrings removed, so comments and documentation naming a forbidden tool --
including this file's own explanations of why they are forbidden -- cannot
trip or, more importantly, mask the check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import titanium.environments.cella as cella_package

PACKAGE_DIR = Path(cella_package.__file__).parent
SOURCES = sorted(PACKAGE_DIR.glob("*.py"))

# Every one of these is a host-side route into a filesystem image, or an
# elevation. None has a legitimate use in this package.
FORBIDDEN = (
    "disk.img",
    "debugfs",
    "dumpe2fs",
    "losetup",
    "guestfish",
    "virt-copy-out",
    "/dev/loop",
    "sudo",
    "--privileged",
    "pkexec",
)

# Cella's runtime verbs. The converter builds artifacts; it does not run
# machines, and must not learn how.
RUNTIME_VERBS = (
    "cella create",
    "cella start",
    "cella stop",
    "cella destroy",
    "cella freeze",
    "cella thaw",
    "cella enter",
    "cella inspect",
    "cella gateway",
)


def _code_only(source: Path) -> str:
    """The module with every docstring and comment removed.

    ``ast`` never records comments, and the docstring bodies are dropped here,
    so what remains is the statements and the string literals that are actually
    values -- the only text that can reach a subprocess.
    """
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


CODE = {source: _code_only(source) for source in SOURCES}


def test_the_package_has_sources_to_check():
    assert SOURCES, f"no python sources under {PACKAGE_DIR}"


def test_the_scan_sees_code_and_not_prose():
    """The stripper must not be so aggressive it can never fail.

    A forbidden token in a real string literal has to be caught; the same
    token in a docstring has to be ignored.
    """
    probe = PACKAGE_DIR / "rootfs.py"
    assert "sudo" in probe.read_text(), "fixture assumption: prose mentions sudo"
    assert "sudo" not in CODE[probe]
    assert "mkfs.ext4" in CODE[probe], "the real command string must survive"


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_host_side_route_into_a_filesystem_image(source: Path):
    for token in FORBIDDEN:
        assert token not in CODE[source], (
            f"{source.name} uses {token!r} in code. A host-side reader for a "
            "Cella disk, or an elevation, is not this converter's to have."
        )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_cella_runtime_verb_is_invoked(source: Path):
    for verb in RUNTIME_VERBS:
        assert verb not in CODE[source], (
            f"{source.name} names {verb!r} in code. This package is build-time only."
        )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_only_the_ext4_builder_runs_a_container(source: Path):
    """Podman may build and flatten. It may not run the workload.

    The one ``podman run`` in this package is the ext4 builder: a pinned
    Alpine image, with no network, and never the task's image.
    """
    code = CODE[source]
    if "'run'" not in code:
        return
    assert source.name == "rootfs.py", (
        f"{source.name} runs a container; only the ext4 builder may."
    )
    assert "'--network=none'" in code
