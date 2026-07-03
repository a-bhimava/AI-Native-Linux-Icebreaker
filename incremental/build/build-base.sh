#!/usr/bin/env bash
# build-base.sh — ONE-TIME base image builder for the Icebreaker incremental rebuild.
#
# Produces: incremental/.build/base-desktop-<hash>.tar.zst
#   <hash> = sha256(packages-desktop.txt stripped + UBUNTU_BASE), first 12 chars.
#
# The base contains: debootstrapped Ubuntu + kernel + live-boot + GNOME +
# GDM autologin + icebreaker user + virtio initramfs. ZERO Icebreaker code —
# version manifests overlay that in build-iso.sh.
#
# Run on the Linux build VM as root:
#   sudo bash incremental/build/build-base.sh
#
# Rebuild is skipped if a tarball with the current hash already exists
# (use --force to rebuild anyway).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INC_ROOT}/.." && pwd)"
BUILD_DIR="${INC_ROOT}/.build"
PKG_FILE="${SCRIPT_DIR}/packages-desktop.txt"
UBUNTU_BASE="noble"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

info() { echo -e "\033[0;32m[$(date +%H:%M:%S)]\033[0m $*"; }
die()  { echo -e "\033[0;31mFATAL:\033[0m $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root (sudo)"
command -v debootstrap >/dev/null || die "debootstrap not installed"
command -v zstd >/dev/null || die "zstd not installed (apt-get install zstd)"
[ -f "$PKG_FILE" ] || die "package list not found: $PKG_FILE"

# ── Compute base hash ───────────────────────────────────────────────────
PKG_LIST="$(grep -vE '^\s*(#|$)' "$PKG_FILE")"
BASE_HASH="$(printf '%s\n%s' "$PKG_LIST" "$UBUNTU_BASE" | sha256sum | cut -c1-12)"
BASE_TAR="${BUILD_DIR}/base-desktop-${BASE_HASH}.tar.zst"

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
info "Debootstrapping ${UBUNTU_BASE} (minbase)..."
debootstrap --variant=minbase "$UBUNTU_BASE" "$CHROOT" \
    http://archive.ubuntu.com/ubuntu

cat > "${CHROOT}/etc/apt/sources.list" <<SOURCES
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE} main restricted universe
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE}-updates main restricted universe
deb http://security.ubuntu.com/ubuntu ${UBUNTU_BASE}-security main restricted universe
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
info "Next: sudo bash incremental/build/build-iso.sh 0"
