#!/bin/bash
# Build Cella's lab flavor from the pinned rev, for the smokes that must
# watch a guest console. The field install (scripts/init/cella.sh) is the
# production posture and never has a console; Cella's own ruling is that
# the lab flavor never installs -- "the lab is the checkout" -- so this
# script builds it *in* the pinned checkout and leaves it there:
#
#   ${XDG_CACHE_HOME:-~/.cache}/titanium/cella-src/target/lab/cella
#
# Same clone, same CELLA_GIT_REV as the field install: the lab binary a
# smoke observes through and the field binary a run ships on are the same
# Cella, differing only in whether the console exists. No digest pin: this
# is observation tooling scoped to the checkout, not an installed runtime.
#
# Cheap when current -- the checkout is already at the pin and cargo
# rebuilds nothing -- so callers run it unconditionally rather than
# guessing staleness from the outside.
set -ueo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/runtime.env" ]] || {
  echo "missing $REPO_ROOT/runtime.env — the checked-in runtime dependency pins" >&2
  exit 1
}
source "$REPO_ROOT/runtime.env"

# The same pinned-source block as scripts/init/cella.sh, kept verbatim so
# either script can run first and the other finds the clone converged.
SRC="${XDG_CACHE_HOME:-$HOME/.cache}/titanium/cella-src"
if [[ ! -d "$SRC/.git" ]]; then
  mkdir -p "$(dirname "$SRC")"
  git clone "$CELLA_GIT_URL" "$SRC"
fi
if ! git -C "$SRC" cat-file -e "$CELLA_GIT_REV^{commit}" 2>/dev/null; then
  git -C "$SRC" fetch origin
  git -C "$SRC" cat-file -e "$CELLA_GIT_REV^{commit}" 2>/dev/null || {
    echo "rev $CELLA_GIT_REV is not reachable from $CELLA_GIT_URL" >&2
    exit 1
  }
fi
git -C "$SRC" checkout --force --detach "$CELLA_GIT_REV"

# Cella's own build target for the flavor (cargo under the hood), so what
# a lab build means stays Cella's decision. cargo needs to be present; the
# field install's package step provides it, hence the hint.
command -v cargo >/dev/null || {
  echo "cargo is not on PATH — run scripts/init/cella.sh (or make .cella) first" >&2
  exit 1
}
make -C "$SRC" build-lab

LAB="$SRC/target/lab/cella"
[[ -x "$LAB" ]] || {
  echo "build-lab did not produce $LAB" >&2
  exit 1
}
"$LAB" doctor check 2>/dev/null | grep -E 'flavor:.*the lab' \
  || { echo "$LAB does not report the lab flavor" >&2; exit 1; }
echo "cella lab flavor ready: $LAB ($CELLA_GIT_REV)"
