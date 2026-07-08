#!/usr/bin/env bash
# build-base.sh — ONE-TIME base image builder for the Icebreaker incremental rebuild.
#
# Produces: incremental/.build/base-desktop-<hash>-<arch>.tar.zst
#   <hash> = sha256(packages-desktop.txt stripped + UBUNTU_BASE), first 12 chars.
#   <arch> = amd64 (default) or arm64. Cache is per-arch (V6.6+).
#
# The base contains: debootstrapped Ubuntu + kernel + live-boot + GNOME +
# GDM autologin + icebreaker user + virtio initramfs. ZERO Icebreaker code —
# version manifests overlay that in build-iso.sh.
#
# Run on the Linux build VM as root:
#   sudo bash incremental/build/build-base.sh                # default: amd64
#   sudo bash incremental/build/build-base.sh --arch arm64   # cross-arch
#   sudo ARCH=arm64 bash incremental/build/build-base.sh     # env-var form
#
# Cross-arch (host != target): uses qemu-user-static + binfmt to run the
# arm64 debootstrap second stage on an amd64 host. Prereq (one-time):
#   sudo apt-get install -y qemu-user-static binfmt-support debootstrap
#   sudo update-binfmts --enable qemu-aarch64
#
# Rebuild is skipped if a tarball with the current hash+arch already exists
# (use --force to rebuild anyway).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INC_ROOT}/.." && pwd)"
BUILD_DIR="${INC_ROOT}/.build"
PKG_FILE="${SCRIPT_DIR}/packages-desktop.txt"
UBUNTU_BASE="noble"

# V6.6: --arch flag defaults to amd64 for backwards compat with V0-V6.51.
ARCH="${ARCH:-amd64}"
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --arch)  ARCH="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

info() { echo -e "\033[0;32m[$(date +%H:%M:%S)]\033[0m $*"; }
die()  { echo -e "\033[0;31mFATAL:\033[0m $*" >&2; exit 1; }

# V6.6: source arch-specific config for ARCH, APT_MIRROR, etc.
CONF="${REPO_ROOT}/config/archs/${ARCH}.conf"
[ -f "$CONF" ] || die "unknown arch '$ARCH' — expected config at $CONF (only amd64, arm64 defined)"
# shellcheck disable=SC1090
source "$CONF"

[ "$(id -u)" = "0" ] || die "must run as root (sudo)"
command -v debootstrap >/dev/null || die "debootstrap not installed"
command -v zstd >/dev/null || die "zstd not installed (apt-get install zstd)"
[ -f "$PKG_FILE" ] || die "package list not found: $PKG_FILE"

# V6.6: cross-arch requires qemu-user-static + binfmt.
HOST_ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
CROSS=0
if [ "$ARCH" != "$HOST_ARCH" ]; then
    CROSS=1
    info "Cross-arch build: host=$HOST_ARCH → target=$ARCH (via qemu-user-static)"
    QEMU_STATIC="/usr/bin/qemu-$( [ "$ARCH" = "arm64" ] && echo aarch64 || echo "$ARCH" )-static"
    [ -x "$QEMU_STATIC" ] || die "$QEMU_STATIC missing — install qemu-user-static + binfmt-support (see file header)"
    # Verify binfmt registered so chroot into foreign-arch ELF works.
    if [ -d /proc/sys/fs/binfmt_misc ]; then
        MAGIC_CHECK="$(cat /proc/sys/fs/binfmt_misc/qemu-aarch64 2>/dev/null | head -1 || echo missing)"
        [ "$MAGIC_CHECK" != "missing" ] || die "binfmt_misc:qemu-aarch64 not registered — run: sudo update-binfmts --enable qemu-aarch64"
    fi
fi

# ── Compute base hash ───────────────────────────────────────────────────
PKG_LIST="$(grep -vE '^\s*(#|$)' "$PKG_FILE")"
BASE_HASH="$(printf '%s\n%s' "$PKG_LIST" "$UBUNTU_BASE" | sha256sum | cut -c1-12)"
# V6.6: per-arch base cache — one tarball per (packages, arch) combination.
BASE_TAR="${BUILD_DIR}/base-desktop-${BASE_HASH}-${ARCH}.tar.zst"

info "Base hash: ${BASE_HASH}"
if [ -f "$BASE_TAR" ] && [ "$FORCE" = "0" ]; then
    info "Base already cached: ${BASE_TAR} ($(du -h "$BASE_TAR" | awk '{print $1}'))"
    info "Nothing to do. Use --force to rebuild."
    exit 0
fi

CHROOT="${BUILD_DIR}/base-chroot"
mkdir -p "$BUILD_DIR"

cleanup() {
    umount "${CHROOT}/dev/pts" 2>/dev/null || true
    umount "${CHROOT}/dev"     2>/dev/null || true
    umount "${CHROOT}/proc"    2>/dev/null || true
    umount "${CHROOT}/sys"     2>/dev/null || true
}
trap cleanup EXIT

rm -rf "$CHROOT"
mkdir -p "$CHROOT"

# ── Debootstrap ─────────────────────────────────────────────────────────
# V6.6: --arch selects the target ISA. Cross-arch uses two-phase
# --foreign + --second-stage (Debian's standard pattern for arm64 on
# an amd64 host) so the first stage's chroot doesn't try to exec
# foreign-arch binaries before binfmt is set up inside the chroot.
info "Debootstrapping ${UBUNTU_BASE} (minbase, arch=${ARCH}, mirror=${APT_MIRROR})..."
if [ "$CROSS" = "1" ]; then
    debootstrap --arch="$ARCH" --foreign --variant=minbase \
        "$UBUNTU_BASE" "$CHROOT" "$APT_MIRROR"
    cp "$QEMU_STATIC" "$CHROOT/usr/bin/"
    chroot "$CHROOT" /debootstrap/debootstrap --second-stage
else
    debootstrap --arch="$ARCH" --variant=minbase \
        "$UBUNTU_BASE" "$CHROOT" "$APT_MIRROR"
fi

# V6.6: sources.list uses ${APT_MIRROR} — amd64 uses archive.ubuntu.com,
# arm64 uses ports.ubuntu.com (arm64 is a "ports" architecture).
# security repo is on security.ubuntu.com for amd64 but on the same
# ports mirror for arm64.
SECURITY_MIRROR="$APT_MIRROR"
if [ "$ARCH" = "amd64" ]; then
    SECURITY_MIRROR="http://security.ubuntu.com/ubuntu"
fi
cat > "${CHROOT}/etc/apt/sources.list" <<SOURCES
deb ${APT_MIRROR} ${UBUNTU_BASE} main restricted universe
deb ${APT_MIRROR} ${UBUNTU_BASE}-updates main restricted universe
deb ${SECURITY_MIRROR} ${UBUNTU_BASE}-security main restricted universe
SOURCES

mount --bind /dev  "${CHROOT}/dev"
mount --bind /proc "${CHROOT}/proc"
mount --bind /sys  "${CHROOT}/sys"
mount -t devpts devpts "${CHROOT}/dev/pts"

# ── Install packages ────────────────────────────────────────────────────
info "Installing $(echo "$PKG_LIST" | wc -l | tr -d ' ') packages (this is the slow part, ~40 min)..."
# shellcheck disable=SC2086
chroot "$CHROOT" bash -c "
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y $(echo $PKG_LIST | tr '\n' ' ')
    locale-gen en_US.UTF-8
"

# ── Virtio modules for QEMU/UTM ─────────────────────────────────────────
info "Adding virtio initramfs modules..."
cat >> "${CHROOT}/etc/initramfs-tools/modules" <<'VIRTIO'
virtio
virtio_pci
virtio_net
virtio_blk
virtio_scsi
9p
9pnet_virtio
e1000
e1000e
VIRTIO
chroot "$CHROOT" bash -c "update-initramfs -u -k all 2>&1 | tail -3"

# ── User + GDM autologin (stable across versions — D-3) ─────────────────
info "Creating icebreaker user + GDM autologin..."
chroot "$CHROOT" bash -c "
    set -e
    systemctl enable gdm
    systemctl enable NetworkManager
    systemctl enable ssh
    groupadd -rf icebreaker-users
    useradd -m -s /bin/bash -G sudo,icebreaker-users icebreaker
    echo 'icebreaker:icebreaker' | chpasswd
    echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/icebreaker
    chmod 440 /etc/sudoers.d/icebreaker
    mkdir -p /etc/gdm3
    cat > /etc/gdm3/custom.conf <<'GDMCFG'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=icebreaker
WaylandEnable=false
[security]
[xdmcp]
[chooser]
[debug]
GDMCFG
"

echo "icebreaker" > "${CHROOT}/etc/hostname"
chroot "$CHROOT" bash -c "apt-get clean && rm -rf /var/lib/apt/lists/*"

cleanup
trap - EXIT

# ── Tar the base ────────────────────────────────────────────────────────
info "Compressing base image (zstd)..."
tar -C "$CHROOT" -cf - . | zstd -3 -T0 -o "$BASE_TAR"
sha256sum "$BASE_TAR" > "${BASE_TAR}.sha256"

# The tarball is the artifact — reclaim the ~5 GB working chroot (F-15).
rm -rf "$CHROOT"

info "Base image ready: ${BASE_TAR} ($(du -h "$BASE_TAR" | awk '{print $1}'))"
info "Next: sudo ARCH=${ARCH} bash incremental/build/build-iso.sh 0"
