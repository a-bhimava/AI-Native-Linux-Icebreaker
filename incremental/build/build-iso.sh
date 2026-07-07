#!/usr/bin/env bash
# build-iso.sh — per-version ISO builder for the Icebreaker incremental rebuild.
#
# Usage (on the Linux build VM, as root):
#   sudo bash incremental/build/build-iso.sh <N> [--no-compress]
#
# Steps:
#   1. Untar cached base (build-base.sh must have run once)
#   2. Apply version manifests v1..vN CUMULATIVELY (each is a delta)
#   3. Run smoke-gate.sh — build ABORTS if any check fails (never ship a broken tree)
#   4. squashfs (zstd) + isolinux BIOS + GRUB EFI + xorriso hybrid ISO
#
# Output: incremental/.build/out/icebreaker-v<N>.iso  (+ .sha256)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INC_ROOT}/.." && pwd)"
BUILD_DIR="${INC_ROOT}/.build"
PKG_FILE="${SCRIPT_DIR}/packages-desktop.txt"
UBUNTU_BASE="noble"

info() { echo -e "\033[0;32m[$(date +%H:%M:%S)]\033[0m $*"; }
die()  { echo -e "\033[0;31mFATAL:\033[0m $*" >&2; exit 1; }

# ── Args ────────────────────────────────────────────────────────────────
[ $# -ge 1 ] || die "usage: build-iso.sh <version-number 0-8> [--no-compress] [--label vX.Y]"
VN="$1"; shift
[[ "$VN" =~ ^[0-8]$ ]] || die "version must be 0-8, got: $VN"
COMPRESS_ARGS=(-comp zstd -Xcompression-level 3)
LABEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-compress) COMPRESS_ARGS=(-noI -noD -noF -noX); shift ;;  # D-1 escape hatch
        --label)       LABEL="$2"; shift 2 ;;  # override version marker + ISO filename (e.g. "v6.1")
        *) die "unknown arg: $1" ;;
    esac
done
# LABEL defaults to plain vN when not overridden (backwards-compatible).
[ -z "$LABEL" ] && LABEL="v${VN}"
[[ "$LABEL" =~ ^v[0-9]+(\.[0-9]+)?$ ]] || die "--label must match ^v[0-9]+(\.[0-9]+)?$, got: $LABEL"

[ "$(id -u)" = "0" ] || die "must run as root (sudo)"
for tool in mksquashfs xorriso zstd grub-mkstandalone mkfs.fat mcopy; do
    command -v "$tool" >/dev/null || die "$tool not installed"
done

# Fail fast, not at xorriso 15 minutes in (F-15): chroot ~6G + squashfs ~2.5G + ISO ~2.5G.
FREE_GB=$(df -BG --output=avail "$INC_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
[ "${FREE_GB:-0}" -ge 12 ] || die "only ${FREE_GB}G free under ${BUILD_DIR} — need ≥12G. Clean old chroots/ISOs first (see GROUND_TRUTH F-15)."

# ── R8 / F-33: mcpd seccomp harvest gate — MUST PASS before any cloud cost ──
# Runs the mcpd binary against a scratch $HOME with the real seccomp filter
# AND with MCPD_SECCOMP_LOG_ONLY=1, exercising every documented tool. If any
# syscall is outside the allowlist or mcpd dies with SIGSYS, the build aborts
# before we spend 15 min of ISO packaging + 15 min of QEMU gate time.
#
# For V ≥ 6 (mcpd shipped in the ISO) this is mandatory. Below V6 mcpd isn't
# installed so the gate is a no-op — check binary presence first.
HARVEST_MCPD="${REPO_ROOT}/cx-distro/.build/mcpd"
if [ "${VN}" -ge 6 ] && [ -x "${HARVEST_MCPD}" ]; then
    info "Running mcpd seccomp harvest gate (F-33, R8)..."
    bash "${SCRIPT_DIR}/mcpd-harvest.sh" --mcpd "${HARVEST_MCPD}" || \
        die "HARVEST GATE FAILED — refusing to ship an ISO with a broken mcpd syscall surface (F-33 / R8). Fix the allowlist in src/mcpd/src/sandbox/seccomp.rs, rebuild mcpd, and retry."
elif [ "${VN}" -ge 6 ]; then
    info "WARN: V${VN} expects mcpd at ${HARVEST_MCPD} but binary missing — harvest gate skipped (build will fail later in v6.manifest overlay)"
fi

# ── Locate cached base ──────────────────────────────────────────────────
PKG_LIST="$(grep -vE '^\s*(#|$)' "$PKG_FILE")"
BASE_HASH="$(printf '%s\n%s' "$PKG_LIST" "$UBUNTU_BASE" | sha256sum | cut -c1-12)"
BASE_TAR="${BUILD_DIR}/base-desktop-${BASE_HASH}.tar.zst"
[ -f "$BASE_TAR" ] || die "no cached base for current package list (hash ${BASE_HASH}).
Run: sudo bash incremental/build/build-base.sh"

# ── Fresh chroot from base ──────────────────────────────────────────────
ISO_WORK="${BUILD_DIR}/iso-work"
CHROOT="${ISO_WORK}/chroot"
STAGING="${ISO_WORK}/staging"
OUT_DIR="${BUILD_DIR}/out"
ISO_FILE="${OUT_DIR}/icebreaker-${LABEL}.iso"

cleanup() {
    umount "${CHROOT}/dev/pts" 2>/dev/null || true
    umount "${CHROOT}/dev"     2>/dev/null || true
    umount "${CHROOT}/proc"    2>/dev/null || true
    umount "${CHROOT}/sys"     2>/dev/null || true
}
trap cleanup EXIT

info "Extracting base ${BASE_HASH} (v${VN} build)..."
rm -rf "$ISO_WORK"
mkdir -p "$CHROOT" "${STAGING}/live" "${STAGING}/isolinux" "$OUT_DIR"
zstd -dc "$BASE_TAR" | tar -C "$CHROOT" -xf -

mount --bind /dev  "${CHROOT}/dev"
mount --bind /proc "${CHROOT}/proc"
mount --bind /sys  "${CHROOT}/sys"
mount -t devpts devpts "${CHROOT}/dev/pts"

# ── Apply manifests v1..vN cumulatively ────────────────────────────────
# Each manifest defines: VERSION_PACKAGES (optional) and version_overlay().
# v0 is the bare base — no manifest work.
for (( i=1; i<=VN; i++ )); do
    MANIFEST="${INC_ROOT}/versions/v${i}.manifest"
    [ -f "$MANIFEST" ] || die "manifest missing: $MANIFEST"
    info "── Applying v${i} manifest ──"
    VERSION_PACKAGES=""
    # shellcheck disable=SC1090
    source "$MANIFEST"
    if [ -n "${VERSION_PACKAGES}" ]; then
        info "Installing v${i} packages: ${VERSION_PACKAGES}"
        chroot "$CHROOT" bash -c "
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y ${VERSION_PACKAGES}
            apt-get clean && rm -rf /var/lib/apt/lists/*
        "
    fi
    version_overlay "$CHROOT" "$REPO_ROOT"
    unset -f version_overlay
done

echo "${LABEL}" > "${CHROOT}/etc/icebreaker-version"

# ── Smoke gate — build aborts on failure ────────────────────────────────
info "Running smoke gate (level ${VN})..."
bash "${SCRIPT_DIR}/smoke-gate.sh" "$CHROOT" "$VN" || \
    die "SMOKE GATE FAILED — not producing an ISO from a broken tree (R6). Fix and rebuild."

# ── Kernel + initrd ─────────────────────────────────────────────────────
VMLINUZ=$(ls "${CHROOT}/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1)
INITRD=$(ls "${CHROOT}/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1)
[ -n "$VMLINUZ" ] || die "no vmlinuz in chroot"
[ -n "$INITRD" ]  || die "no initrd in chroot"
cp "$VMLINUZ" "${STAGING}/live/vmlinuz"
cp "$INITRD"  "${STAGING}/live/initrd"
info "Kernel: $(basename "$VMLINUZ")"

# ── Squashfs ────────────────────────────────────────────────────────────
cleanup
trap - EXIT
info "Creating squashfs (${COMPRESS_ARGS[0]} ${COMPRESS_ARGS[1]:-})..."
mksquashfs "$CHROOT" "${STAGING}/live/filesystem.squashfs" \
    "${COMPRESS_ARGS[@]}" -b 1M -no-duplicates \
    -e boot/vmlinuz-\* boot/initrd.img-\* \
    2>&1 | tail -3
info "Squashfs: $(du -h "${STAGING}/live/filesystem.squashfs" | awk '{print $1}')"

# ── isolinux (BIOS) ─────────────────────────────────────────────────────
ISOLINUX_BIN=""
for p in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/syslinux/isolinux.bin; do
    [ -f "$p" ] && ISOLINUX_BIN="$p" && break
done
[ -n "$ISOLINUX_BIN" ] || die "isolinux.bin not found — apt-get install isolinux"
cp "$ISOLINUX_BIN" "${STAGING}/isolinux/"

SYSLINUX_MOD=""
for p in /usr/lib/syslinux/modules/bios /usr/lib/syslinux; do
    [ -f "${p}/ldlinux.c32" ] && SYSLINUX_MOD="$p" && break
done
[ -n "$SYSLINUX_MOD" ] || die "syslinux modules not found — apt-get install syslinux-common"
for mod in ldlinux.c32 libcom32.c32 vesamenu.c32 libutil.c32; do
    [ -f "${SYSLINUX_MOD}/${mod}" ] && cp "${SYSLINUX_MOD}/${mod}" "${STAGING}/isolinux/"
done

ISOHDPFX=""
for p in /usr/lib/ISOLINUX/isohdpfx.bin /usr/lib/syslinux/isohdpfx.bin \
         /usr/lib/syslinux/mbr/isohdpfx.bin; do
    [ -f "$p" ] && ISOHDPFX="$p" && break
done
[ -n "$ISOHDPFX" ] || die "isohdpfx.bin not found"

_LIVE_APPEND="boot=live toram quiet splash"
_SAFE_APPEND="boot=live toram single nomodeset"

cat > "${STAGING}/isolinux/isolinux.cfg" <<BOOTMENU
UI vesamenu.c32
PROMPT 0
TIMEOUT 50

MENU TITLE Icebreaker Incremental v${VN}

DEFAULT live

LABEL live
  MENU LABEL ^Icebreaker v${VN} (Live)
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_LIVE_APPEND}

LABEL live-safe
  MENU LABEL ^Safe Mode
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_SAFE_APPEND}
BOOTMENU

# ── GRUB EFI ────────────────────────────────────────────────────────────
info "Building GRUB EFI..."
GRUB_CFG_DIR="${ISO_WORK}/grub-embed"
mkdir -p "$GRUB_CFG_DIR" "${STAGING}/boot/grub"

cat > "${GRUB_CFG_DIR}/grub.cfg" <<GRUBCFG
search --no-floppy --set=root --label ICEBREAKER

set default=0
set timeout=5

menuentry "Icebreaker v${VN} (Live)" {
    linux /live/vmlinuz ${_LIVE_APPEND}
    initrd /live/initrd
}

menuentry "Safe Mode" {
    linux /live/vmlinuz ${_SAFE_APPEND}
    initrd /live/initrd
}
GRUBCFG

grub-mkstandalone \
    --format=x86_64-efi \
    --output="${STAGING}/boot/grub/BOOTX64.EFI" \
    --modules="part_gpt part_msdos fat iso9660 search search_label linux normal all_video test" \
    "boot/grub/grub.cfg=${GRUB_CFG_DIR}/grub.cfg"
[ -f "${STAGING}/boot/grub/BOOTX64.EFI" ] || die "grub-mkstandalone failed"

EFI_IMG="${STAGING}/boot/grub/efi.img"
GRUB_SIZE_KB=$(( $(stat -c%s "${STAGING}/boot/grub/BOOTX64.EFI") / 1024 ))
dd if=/dev/zero of="$EFI_IMG" bs=1K count=$(( GRUB_SIZE_KB + 1024 )) 2>/dev/null
mkfs.fat "$EFI_IMG" >/dev/null
mmd -i "$EFI_IMG" ::EFI
mmd -i "$EFI_IMG" ::EFI/BOOT
mcopy -i "$EFI_IMG" "${STAGING}/boot/grub/BOOTX64.EFI" ::EFI/BOOT/BOOTX64.EFI

# ── xorriso ─────────────────────────────────────────────────────────────
info "Assembling hybrid ISO..."
xorriso -as mkisofs \
    -iso-level 3 \
    -isohybrid-mbr "$ISOHDPFX" \
    -c isolinux/boot.cat \
    -b isolinux/isolinux.bin \
    -no-emul-boot \
    -boot-load-size 4 \
    -boot-info-table \
    -eltorito-alt-boot \
    -e boot/grub/efi.img \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    -V "ICEBREAKER" \
    -o "$ISO_FILE" \
    "${STAGING}/" 2>&1 | tail -3

[ -f "$ISO_FILE" ] || die "ISO not created"
sha256sum "$ISO_FILE" > "${ISO_FILE}.sha256"

info "════════════════════════════════════════════════"
info "ISO: ${ISO_FILE} ($(du -h "$ISO_FILE" | awk '{print $1}'))"
info "SHA: $(awk '{print $1}' "${ISO_FILE}.sha256")"
info "Next: bash incremental/tests/qemu-gate.sh ${ISO_FILE} ${VN}"
info "════════════════════════════════════════════════"
