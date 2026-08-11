#!/usr/bin/env bash
# Vendor every container image a task set can reference into one archive, so
# an airgapped host restores them with restore.sh and never pulls anything.
#
#   ./vendor.sh <tasks-dir> <archive.tar> [--prebuilt]
#
# Collected: FROM images of every task environment Dockerfile (multi-stage
# references excluded, qualification identical to build preparation), the
# egress proxy base, and — only with --prebuilt, matching
# TITANIUM_IMAGE_SOURCE=prebuilt — each task's docker_image. Image-only tasks
# (no Dockerfile) always contribute their docker_image: there is nothing
# else to build from.
set -ueo pipefail
TASKS_DIR=${1:?usage: vendor.sh <tasks-dir> <archive.tar> [--prebuilt]}
OUT=${2:?usage: vendor.sh <tasks-dir> <archive.tar> [--prebuilt]}
PREBUILT=${3:-}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

refs=$("$PY" - "$TASKS_DIR" "$PREBUILT" <<'EOF'
import re, sys
from pathlib import Path

from titanium.environments.agent_setup import (
    _AS_STAGE_RE,
    _FROM_LINE_RE,
    qualify_image_reference,
)

tasks_dir = Path(sys.argv[1])
include_prebuilt = sys.argv[2] == "--prebuilt"
refs: set[str] = set()

# The egress proxy base (titanium-owned, always in play for allowlist tasks).
refs.add("docker.io/library/alpine:3.22")

for toml in tasks_dir.rglob("task.toml"):
    task_dir = toml.parent
    dockerfile = task_dir / "environment" / "Dockerfile"
    match = re.search(r'^docker_image\s*=\s*"([^"]+)"', toml.read_text(), re.M)
    image = match.group(1) if match else None
    if image and (include_prebuilt or not dockerfile.exists()):
        refs.add(qualify_image_reference(image))
    if dockerfile.exists():
        stages: set[str] = set()
        for line in dockerfile.read_text().splitlines():
            m = _FROM_LINE_RE.match(line)
            if not m:
                continue
            suffix = m.group("suffix") or ""
            as_match = _AS_STAGE_RE.search(suffix)
            if as_match:
                stages.add(as_match.group("stage").lower())
            ref = m.group("image")
            if ref.lower() in stages or ref == "scratch" or ref.startswith("$"):
                continue
            refs.add(qualify_image_reference(ref))

print("\n".join(sorted(refs)))
EOF
)

[[ -n "$refs" ]] || { echo "no image references found under $TASKS_DIR"; exit 1; }
echo "vendoring:"
printf '  %s\n' $refs
for ref in $refs; do podman pull "$ref"; done
# shellcheck disable=SC2086
podman save --multi-image-archive -o "$OUT" $refs
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
