#!/usr/bin/env bash
# build-kernel.sh <inner-env> — build one inner environment's guest kernel
# for `cella-runner`.
#
# Self-contained by design (docs/runners/CELLA-RUNNER.md §4): titanium fetches
# its own kernel source and merges its own fragments -- the base every guest
# needs, plus the named environment's own. Each environment gets its own
# kernel, so the guest carries only that environment's surface. It reads
# nothing from the cella repo, its source cache, or its build outputs — no
# cross-repo dependency. cella only consumes the bzImage this produces (it
# checks the file exists at create; it never builds or re-hashes it).
#
# The build runs in a toolbox, the way cella builds its kernels: a fedora
# toolbox named cella-build, provisioned here with the kernel toolchain
# (idempotent). The toolbox is a host build container, not a repo.
#
# Output (titanium-owned, staged into the run's CELLA_HOME by cella-runner.sh):
#   ${XDG_CACHE_HOME:-~/.cache}/titanium/cella-runner-kernel/<inner-env>/bzImage
# One source tree, one out-of-tree build directory per environment.
#
#   exit 0  the kernel is built and current
#   exit 1  a real failure
#   exit 2  a precondition is missing
set -ueo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/runtime.env" ]] || { echo "missing runtime.env" >&2; exit 1; }
source "$REPO_ROOT/runtime.env"

INNER_ENV="${1:-}"
case "$INNER_ENV" in
    docker|cella) ;;
    *) echo "usage: build-kernel.sh docker|cella" >&2; exit 2 ;;
esac
FRAGMENTS="$REPO_ROOT/scripts/cella-runner"
BASE="$FRAGMENTS/kernel-fragment-base.config"
OVERLAY="$FRAGMENTS/kernel-fragment-$INNER_ENV.config"
for f in "$BASE" "$OVERLAY"; do [[ -f "$f" ]] || { echo "missing $f" >&2; exit 1; }; done

VERSION="${CELLA_RUNNER_KERNEL_VERSION:?set in runtime.env}"
MAJOR="${VERSION%%.*}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/titanium/cella-runner-kernel"
SRC="$CACHE/linux-$VERSION"
BUILD="$CACHE/$INNER_ENV/build"
OUT="$CACHE/$INNER_ENV/bzImage"
STAMP="$CACHE/$INNER_ENV/fragments.sha256"
TOOLBOX="cella-build"

# Kernel toolchain. Titanium's own list (a subset of cella's), so this does
# not depend on cella having provisioned the toolbox first.
PACKAGES=(gcc make bc bison flex elfutils-libelf-devel openssl-devel
          perl-interpreter perl-generators xz findutils diffutils)

command -v toolbox >/dev/null 2>&1 || { echo "toolbox is not installed" >&2; exit 2; }

# Idempotent: a current bzImage whose fragments' digest is unchanged is done.
frag_digest="$(cat "$BASE" "$OVERLAY" | sha256sum | cut -d' ' -f1)"
if [[ -f "$OUT" && -f "$STAMP" && "$(cat "$STAMP")" == "$frag_digest" ]]; then
    echo "cella-runner kernel ($INNER_ENV) current: $OUT"
    exit 0
fi

# --- sentinel the toolbox -------------------------------------------------
if ! toolbox list -c 2>/dev/null | awk '{print $2}' | grep -qx "$TOOLBOX"; then
    echo "creating the $TOOLBOX toolbox"
    toolbox create -y "$TOOLBOX"
fi
echo "provisioning the $TOOLBOX toolchain (idempotent)"
toolbox run -c "$TOOLBOX" sudo dnf install -y "${PACKAGES[@]}" >/dev/null

# --- fetch the source (titanium's own cache) ------------------------------
mkdir -p "$CACHE"
if [[ ! -d "$SRC" ]]; then
    TARBALL="$CACHE/linux-$VERSION.tar.xz"
    URL="https://cdn.kernel.org/pub/linux/kernel/v${MAJOR}.x/linux-${VERSION}.tar.xz"
    echo "fetching $URL"
    curl -SL "$URL" -o "$TARBALL"
    tar -C "$CACHE" -xf "$TARBALL"
fi

# --- configure: defconfig + the base + the environment's fragment ---------
# Out of tree (O=): the environments share the source and each owns its
# build directory. kbuild refuses O= while the source tree holds a build of
# its own, so an in-tree build from before the split is cleaned first.
[[ -f "$SRC/.config" ]] && toolbox run -c "$TOOLBOX" make -C "$SRC" mrproper
mkdir -p "$BUILD"
echo "configuring $INNER_ENV (x86_64_defconfig + base + $INNER_ENV fragments)"
toolbox run -c "$TOOLBOX" make -C "$SRC" O="$BUILD" x86_64_defconfig
toolbox run -c "$TOOLBOX" bash -c \
    "cd '$SRC' && ./scripts/kconfig/merge_config.sh -m -O '$BUILD' '$BUILD/.config' '$BASE' '$OVERLAY'"
toolbox run -c "$TOOLBOX" make -C "$SRC" O="$BUILD" olddefconfig

# The configs that must survive: the boot-critical ones for every guest,
# then the environment's own load-bearing ones.
SURVIVE="CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES CONFIG_VIRTIO_BLK
         CONFIG_SERIAL_8250_CONSOLE CONFIG_EXT4_FS CONFIG_DEVTMPFS_MOUNT
         CONFIG_USER_NS CONFIG_CGROUPS"
case "$INNER_ENV" in
    docker) SURVIVE="$SURVIVE CONFIG_CGROUP_BPF CONFIG_MEMCG CONFIG_OVERLAY_FS
                     CONFIG_NETFILTER_XT_TARGET_MASQUERADE CONFIG_NF_NAT CONFIG_BRIDGE
                     CONFIG_NF_TABLES_IPV4 CONFIG_NFT_NAT CONFIG_NFT_COMPAT
                     CONFIG_NETFILTER_XT_NAT CONFIG_NETFILTER_XT_TARGET_REDIRECT" ;;
    cella)  SURVIVE="$SURVIVE CONFIG_KVM CONFIG_TUN" ;;
esac
missing=""
for sym in $SURVIVE; do
    grep -qx "$sym=y" "$BUILD/.config" || missing="$missing $sym"
done
[[ -z "$missing" ]] || { echo "these did not survive olddefconfig:$missing" >&2; exit 1; }

# --- build ----------------------------------------------------------------
echo "building bzImage ($INNER_ENV)"
toolbox run -c "$TOOLBOX" make -C "$SRC" O="$BUILD" -j"$(nproc)" bzImage

BUILT="$BUILD/arch/x86/boot/bzImage"
[[ -f "$BUILT" ]] || { echo "build produced no bzImage at $BUILT" >&2; exit 1; }
cp "$BUILT" "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
echo "$frag_digest" > "$STAMP"
echo "cella-runner kernel ($INNER_ENV) built: $OUT ($VERSION, $(du -h "$OUT" | cut -f1))"
