#!/usr/bin/env bash
# build.sh — Master build script for the Icebreaker AI-Native OS ISO.
#
# Produces a bootable Ubuntu live ISO with both AI brains (llama-server),
# mcpd, the Controller, model weights, and system configuration.
#
# Usage:
#   cd cx-distro && sudo bash build.sh [OPTIONS]
#
# Options:
#   --profile=NAME  Build profile: vm (default) or desktop
#                     vm      — XFCE, LightDM, nomodeset, no toram (low RAM, emulation)
#                     desktop — GNOME, GDM, toram, full GPU (bare-metal / native virt)
#   --skip-to=N     Skip stages 0..N-1 (e.g. --skip-to=4 to skip binary builds)
#   --force         Allow running outside Docker
#   --no-models     Skip GGUF model embedding (fast ISO for testing boot flow)
#
# Stages:
#   0  Preflight    — verify repo state, checksums, dependencies
#   1  mcpd         — cargo build --release
#   2  llama-server — clone + build llama.cpp at pinned commit
#   3  venv         — create Python venv, pip install dual-brain/
#   4  chroot       — assemble config/includes.chroot/ tree
#   5  iso          — debootstrap + mksquashfs + xorriso (hybrid BIOS+EFI)
#
# SECURITY: This file is listed in CLAUDE.md Security-Critical Files.
# Any change requires human review from the module owner.
set -euo pipefail

# Workaround for dubious ownership in Docker builds
git config --global --add safe.directory '*'

# ── Constants ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${SCRIPT_DIR}/.build"
CHROOT="${SCRIPT_DIR}/config/includes.chroot"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# ── Helpers ─────────────────────────────────────────────────────────────

die() {
    printf "${RED}FATAL:${NC} %s\n" "$*" >&2
    exit 1
}

info() {
    printf "${GREEN}[%s]${NC} %s\n" "$(date +%H:%M:%S)" "$*"
}

warn() {
    printf "${YELLOW}WARN:${NC} %s\n" "$*" >&2
}

stage_banner() {
    printf "\n${BOLD}════ Stage %s: %s ════${NC}\n\n" "$1" "$2"
}

# ── Argument parsing ────────────────────────────────────────────────────
SKIP_TO=0
FORCE=0
NO_MODELS=0
PROFILE="vm"

for arg in "$@"; do
    case "$arg" in
        --profile=*)
            PROFILE="${arg#--profile=}"
            if [[ "$PROFILE" != "vm" && "$PROFILE" != "desktop" ]]; then
                die "--profile must be 'vm' or 'desktop', got: $PROFILE"
            fi
            ;;
        --skip-to=*)
            SKIP_TO="${arg#--skip-to=}"
            if ! [[ "$SKIP_TO" =~ ^[0-5]$ ]]; then
                die "--skip-to must be 0-5, got: $SKIP_TO"
            fi
            ;;
        --force)
            FORCE=1
            ;;
        --no-models)
            NO_MODELS=1
            ;;
        --help|-h)
            head -28 "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            die "unknown argument: $arg"
            ;;
    esac
done

info "Build profile: ${PROFILE}"

# ── Stage 0: Preflight ─────────────────────────────────────────────────

if [ "$SKIP_TO" -le 0 ]; then
    stage_banner 0 "Preflight"

    # Check we are inside Docker (or --force).
    if [ "$FORCE" -eq 0 ] && [ ! -f /.dockerenv ]; then
        die "build.sh must run inside Docker. Use: docker build -t icebreaker-build -f Dockerfile.build .. && docker run --privileged -v \$(pwd)/..:/build icebreaker-build   (or --force for local builds)"
    fi

    # Verify repo structure.
    [ -f "${REPO_ROOT}/models/checksums.sha256" ] || \
        die "models/checksums.sha256 not found (INV-7)"
    [ -d "${REPO_ROOT}/src/mcpd" ] || \
        die "src/mcpd/ not found"
    [ -f "${REPO_ROOT}/dual-brain/pyproject.toml" ] || \
        die "dual-brain/pyproject.toml not found"

    # INV-7: Verify all model checksums.
    if [ "$NO_MODELS" -eq 0 ]; then
        info "Verifying model checksums (INV-7)..."
        CHECKSUM_FAILURES=0
        while IFS= read -r line; do
            # Skip empty lines and comments.
            [ -z "$line" ] && continue
            [[ "$line" == \#* ]] && continue

            EXPECTED_HASH=$(echo "$line" | awk '{print $1}')
            RAW_PATH=$(echo "$line" | awk '{print $2}')
            FILENAME=$(basename "$RAW_PATH")

            MODEL_FILE="${REPO_ROOT}/models/${FILENAME}"
            if [ ! -f "$MODEL_FILE" ]; then
                warn "model file not found: ${FILENAME} (listed in checksums.sha256)"
                CHECKSUM_FAILURES=$((CHECKSUM_FAILURES + 1))
                continue
            fi

            ACTUAL_HASH=$(sha256sum "$MODEL_FILE" | awk '{print $1}')
            if [ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]; then
                warn "checksum MISMATCH for ${FILENAME}"
                warn "  expected: ${EXPECTED_HASH}"
                warn "  actual:   ${ACTUAL_HASH}"
                CHECKSUM_FAILURES=$((CHECKSUM_FAILURES + 1))
            else
                info "  ${FILENAME}: checksum OK"
            fi
        done < "${REPO_ROOT}/models/checksums.sha256"

        if [ "$CHECKSUM_FAILURES" -gt 0 ]; then
            die "INV-7 violated: ${CHECKSUM_FAILURES} checksum failure(s). Aborting."
        fi
        info "All model checksums verified."
    else
        warn "--no-models: skipping GGUF checksum verification"
    fi

    # Create build directory.
    mkdir -p "${BUILD_DIR}"

    # Write build manifest.
    cat > "${BUILD_DIR}/manifest.json" <<MANIFEST
{
  "build_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_sha": "$(cd "${REPO_ROOT}" && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "git_dirty": $(cd "${REPO_ROOT}" && git diff --quiet 2>/dev/null && echo false || echo true),
  "ubuntu_base": "$(cat "${SCRIPT_DIR}/UBUNTU_BASE")",
  "llama_cpp_commit": "$(cat "${SCRIPT_DIR}/LLAMA_CPP_COMMIT")",
  "no_models": ${NO_MODELS},
  "profile": "${PROFILE}"
}
MANIFEST
    info "Build manifest written to .build/manifest.json"
fi

# ── Stage 1: Build mcpd ────────────────────────────────────────────────

if [ "$SKIP_TO" -le 1 ]; then
    stage_banner 1 "Build mcpd"

    cd "${REPO_ROOT}/src/mcpd"
    info "Building mcpd (release)..."
    cargo build --release 2>&1 | tail -5

    MCPD_BIN="target/release/mcpd"
    [ -f "$MCPD_BIN" ] || die "mcpd binary not found after build"

    # Security check: no test-only features compiled in.
    if strings "$MCPD_BIN" | grep -q MCPD_FS_TEST_ROOTS; then
        die "mcpd compiled with test-only feature fs-test-roots (CLAUDE.md § Test-Only Knobs)"
    fi
    info "mcpd binary OK ($(du -h "$MCPD_BIN" | awk '{print $1}'), no test features)"

    cp "$MCPD_BIN" "${BUILD_DIR}/mcpd"
    cd "${SCRIPT_DIR}"
fi

# ── Stage 2: Build llama-server ─────────────────────────────────────────

if [ "$SKIP_TO" -le 2 ]; then
    stage_banner 2 "Build llama-server"

    LLAMA_COMMIT=$(cat "${SCRIPT_DIR}/LLAMA_CPP_COMMIT" | tr -d '[:space:]')
    LLAMA_SRC="${BUILD_DIR}/llama.cpp"

    # Clone or update.
    if [ -d "${LLAMA_SRC}/.git" ]; then
        info "llama.cpp source exists, fetching..."
        cd "${LLAMA_SRC}" && git fetch origin
    else
        info "Cloning llama.cpp..."
        rm -rf "${LLAMA_SRC}"
        git clone --filter=blob:none https://github.com/ggerganov/llama.cpp "${LLAMA_SRC}"
        cd "${LLAMA_SRC}"
    fi

    info "Checking out commit ${LLAMA_COMMIT}..."
    git checkout "${LLAMA_COMMIT}" || die "failed to checkout llama.cpp commit ${LLAMA_COMMIT}"

    info "Building llama-server (CPU-only)..."
    cmake -B build \
        -DGGML_CUDA=OFF \
        -DGGML_METAL=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        2>&1 | tail -3
    cmake --build build --target llama-server -j"$(nproc)" 2>&1 | tail -5

    LLAMA_BIN="build/bin/llama-server"
    [ -f "$LLAMA_BIN" ] || die "llama-server binary not found after build"
    info "llama-server OK ($(du -h "$LLAMA_BIN" | awk '{print $1}'))"

    cp "$LLAMA_BIN" "${BUILD_DIR}/llama-server"
    cd "${SCRIPT_DIR}"
fi

# ── Stage 3: Build Python venv ──────────────────────────────────────────

if [ "$SKIP_TO" -le 3 ]; then
    stage_banner 3 "Build Python venv"

    VENV_DIR="${BUILD_DIR}/venv"
    rm -rf "${VENV_DIR}"
    info "Creating venv..."
    python3 -m venv --system-site-packages "${VENV_DIR}"

    info "Installing icebreaker-controller..."
    "${VENV_DIR}/bin/pip" install --no-cache-dir "${REPO_ROOT}/dual-brain/" 2>&1 | tail -5

    # pip doesn't ship data files — copy them into the installed package.
    CONTROLLER_PKG=$(find "${VENV_DIR}" -path '*/site-packages/controller/model_registry.py' \
        -exec dirname {} \; | head -1)
    if [ -n "$CONTROLLER_PKG" ]; then
        cp "${REPO_ROOT}/dual-brain/controller/catalogue.toml" "${CONTROLLER_PKG}/catalogue.toml"
        cp -a "${REPO_ROOT}/dual-brain/controller/schemas"  "${CONTROLLER_PKG}/schemas"
        cp -a "${REPO_ROOT}/dual-brain/controller/prompts"  "${CONTROLLER_PKG}/prompts"
        cp -a "${REPO_ROOT}/dual-brain/controller/grammars" "${CONTROLLER_PKG}/grammars"
        info "Data files copied into venv package"
    fi

    # Verify entry point.
    "${VENV_DIR}/bin/python3" -m controller --help >/dev/null 2>&1 || \
        die "venv broken: python3 -m controller --help failed"
    info "Venv OK ($(du -sh "${VENV_DIR}" | awk '{print $1}'))"
fi

# ── Stage 4: Assemble chroot tree ──────────────────────────────────────

if [ "$SKIP_TO" -le 4 ]; then
    stage_banner 4 "Assemble chroot tree"

    # Clean previous chroot assembly.
    rm -rf "${CHROOT}"
    mkdir -p "${CHROOT}"

    # ── /etc/icebreaker/ ────────────────────────────────────────────────
    install -Dm644 "${SCRIPT_DIR}/distro/controller.toml" \
        "${CHROOT}/etc/icebreaker/controller.toml"
    install -Dm644 "${SCRIPT_DIR}/distro/locations.env" \
        "${CHROOT}/etc/icebreaker/locations.env"

    # ── /etc/systemd/system/ ────────────────────────────────────────────
    for unit in "${REPO_ROOT}"/dual-brain/controller/systemd/*.service \
                "${REPO_ROOT}"/dual-brain/controller/systemd/*.socket; do
        [ -f "$unit" ] || continue
        install -Dm644 "$unit" "${CHROOT}/etc/systemd/system/$(basename "$unit")"
    done

    # ── /etc/sysusers.d/ and /etc/tmpfiles.d/ ──────────────────────────
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/systemd/icebreaker.sysusers.d.conf" \
        "${CHROOT}/etc/sysusers.d/icebreaker.conf"
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/systemd/icebreaker.tmpfiles.d.conf" \
        "${CHROOT}/etc/tmpfiles.d/icebreaker.conf"

    # ── /usr/bin/ ───────────────────────────────────────────────────────
    install -Dm755 "${SCRIPT_DIR}/distro/icebreaker-cli" \
        "${CHROOT}/usr/bin/icebreaker"
    install -Dm755 "${SCRIPT_DIR}/distro/mount-mac-share" \
        "${CHROOT}/usr/bin/mount-mac-share"

    # ── /usr/libexec/icebreaker/ ────────────────────────────────────────
    install -Dm755 "${BUILD_DIR}/mcpd" \
        "${CHROOT}/usr/libexec/icebreaker/mcpd"
    install -Dm755 "${BUILD_DIR}/llama-server" \
        "${CHROOT}/usr/libexec/icebreaker/llama-server"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-pbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-pbd"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-qbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-qbd"
    install -Dm755 "${SCRIPT_DIR}/distro/first-boot" \
        "${CHROOT}/usr/libexec/icebreaker/first-boot"
    install -Dm755 "${SCRIPT_DIR}/distro/safe-mode" \
        "${CHROOT}/usr/libexec/icebreaker/safe-mode"
    install -Dm755 "${SCRIPT_DIR}/distro/wait-for-sockets" \
        "${CHROOT}/usr/libexec/icebreaker/wait-for-sockets"

    # ── /usr/share/icebreaker/ ──────────────────────────────────────────
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/catalogue.toml" \
        "${CHROOT}/usr/share/icebreaker/catalogue.toml"
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/grammars/qb_intent.gbnf" \
        "${CHROOT}/usr/share/icebreaker/grammars/qb_intent.gbnf"

    # Schemas: controller + mcpd tool schemas in one directory.
    for schema in "${REPO_ROOT}"/dual-brain/controller/schemas/*.json \
                  "${REPO_ROOT}"/src/mcpd/schemas/*.json; do
        [ -f "$schema" ] || continue
        install -Dm644 "$schema" \
            "${CHROOT}/usr/share/icebreaker/schemas/$(basename "$schema")"
    done

    # System prompts.
    for prompt in "${REPO_ROOT}"/dual-brain/controller/prompts/*.txt; do
        [ -f "$prompt" ] || continue
        install -Dm644 "$prompt" \
            "${CHROOT}/usr/share/icebreaker/prompts/$(basename "$prompt")"
    done

    # ── /opt/icebreaker/venv/ ───────────────────────────────────────────
    mkdir -p "${CHROOT}/opt/icebreaker"
    cp -a "${BUILD_DIR}/venv" "${CHROOT}/opt/icebreaker/venv"

    # ── /var/lib/icebreaker/models/ ─────────────────────────────────────
    install -Dm644 "${REPO_ROOT}/models/checksums.sha256" \
        "${CHROOT}/var/lib/icebreaker/models/checksums.sha256"

    if [ "$NO_MODELS" -eq 0 ]; then
        for gguf in "${REPO_ROOT}"/models/*.gguf; do
            [ -f "$gguf" ] || continue
            info "Embedding model: $(basename "$gguf") ($(du -h "$gguf" | awk '{print $1}'))"
            install -Dm644 "$gguf" \
                "${CHROOT}/var/lib/icebreaker/models/$(basename "$gguf")"
        done
    else
        warn "--no-models: skipping GGUF embedding"
    fi

    # ── .desktop files ─────────────────────────────────────────────────
    for desktop_file in "${SCRIPT_DIR}"/distro/*.desktop; do
        [ -f "$desktop_file" ] || continue
        install -Dm644 "$desktop_file" \
            "${CHROOT}/usr/share/applications/$(basename "$desktop_file")"
    done

    # ── Wallpaper + GNOME defaults ─────────────────────────────────────
    install -Dm644 "${SCRIPT_DIR}/distro/icebreaker-wallpaper.png" \
        "${CHROOT}/usr/share/backgrounds/icebreaker-wallpaper.png"
    install -Dm644 "${SCRIPT_DIR}/distro/99_icebreaker.gschema.override" \
        "${CHROOT}/usr/share/glib-2.0/schemas/99_icebreaker.gschema.override"

    # ── XFCE defaults (vm profile) ────────────────────────────────────
    for xfconf_file in xfce4-desktop.xml xfce4-panel.xml xsettings.xml; do
        if [ -f "${SCRIPT_DIR}/distro/${xfconf_file}" ]; then
            install -Dm644 "${SCRIPT_DIR}/distro/${xfconf_file}" \
                "${CHROOT}/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/${xfconf_file}"
        fi
    done

    # ── Terminal autostart ─────────────────────────────────────────────
    if [ -f "${SCRIPT_DIR}/distro/icebreaker-terminal-autostart.desktop" ]; then
        install -Dm644 "${SCRIPT_DIR}/distro/icebreaker-terminal-autostart.desktop" \
            "${CHROOT}/etc/xdg/autostart/icebreaker-terminal.desktop"
    fi

    # ── Build manifest ──────────────────────────────────────────────────
    install -Dm644 "${BUILD_DIR}/manifest.json" \
        "${CHROOT}/usr/share/icebreaker/build-manifest.json"

    # Count installed files.
    FILE_COUNT=$(find "${CHROOT}" -type f | wc -l | tr -d ' ')
    info "Chroot assembled: ${FILE_COUNT} files"
fi

# ── Stage 5: Build ISO ─────────────────────────────────────────────────
# Uses debootstrap + mksquashfs + xorriso directly.
# live-build (lb) 3.0 on Noble is broken: its grub and syslinux bootloader
# paths reference packages removed from Ubuntu years ago (grub-legacy,
# syslinux-themes-ubuntu-oneiric). Manual assembly avoids that entirely.

if [ "$SKIP_TO" -le 5 ]; then
    stage_banner 5 "Build ISO"

    cd "${SCRIPT_DIR}"
    UBUNTU_BASE=$(cat "${SCRIPT_DIR}/UBUNTU_BASE" | tr -d '[:space:]')
    ISO_WORK="${BUILD_DIR}/iso-work"
    ISO_CHROOT="${ISO_WORK}/chroot"
    ISO_STAGING="${ISO_WORK}/staging"
    ISO_FILE="${SCRIPT_DIR}/icebreaker.iso"

    rm -rf "${ISO_WORK}"
    mkdir -p "${ISO_CHROOT}" "${ISO_STAGING}/live" "${ISO_STAGING}/isolinux"

    # ── 5a: Debootstrap base system ────────────────────────────────────
    info "Debootstrapping ${UBUNTU_BASE}..."
    debootstrap --variant=minbase "${UBUNTU_BASE}" "${ISO_CHROOT}" \
        http://archive.ubuntu.com/ubuntu

    cat > "${ISO_CHROOT}/etc/apt/sources.list" <<SOURCES
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE} main restricted universe
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE}-updates main restricted universe
deb http://security.ubuntu.com/ubuntu ${UBUNTU_BASE}-security main restricted universe
SOURCES

    # ── 5b: Install kernel + live-boot inside chroot ───────────────────
    info "Installing kernel and live-boot packages..."
    mount --bind /dev  "${ISO_CHROOT}/dev"
    mount --bind /proc "${ISO_CHROOT}/proc"
    mount --bind /sys  "${ISO_CHROOT}/sys"
    mount -t devpts devpts "${ISO_CHROOT}/dev/pts"

    # ── Profile-specific desktop/DM setup ─────────────────────────────
    if [ "$PROFILE" = "desktop" ]; then
        _DESKTOP_PKGS="ubuntu-desktop ubuntu-standard gdm3 libreoffice vlc gimp thunderbird"
    else
        _DESKTOP_PKGS="xfce4 xfce4-terminal lightdm lightdm-gtk-greeter thunar mousepad zenity"
    fi

    chroot "${ISO_CHROOT}" bash -c "
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq

        # Core system packages.
        apt-get install -y --no-install-recommends \
            linux-generic \
            live-boot \
            systemd-sysv \
            sudo bash coreutils python3 python3-venv python3-pip \
            curl ca-certificates \
            net-tools iproute2 iputils-ping \
            openssh-client openssh-server less vim-tiny locales \
            dbus-x11

        # X server — required for any display manager (LightDM/GDM) to launch.
        # xfce4/gdm3 packages do NOT pull this in with --no-install-recommends.
        # Without xserver-xorg the system falls back to a bare VT/terminal.
        apt-get install -y --no-install-recommends \
            xserver-xorg \
            xserver-xorg-core \
            xserver-xorg-video-all \
            xinit \
            x11-xserver-utils \
            spice-vdagent \
            qemu-guest-agent

        # Network management — provides DHCP client and NIC management at boot.
        # Required for internet connectivity in the live session.
        apt-get install -y --no-install-recommends \
            network-manager \
            isc-dhcp-client

        # Desktop environment (profile-selected). We want the full themes and icons!
        apt-get install -y ${_DESKTOP_PKGS}

        # GTK4 + LibAdwaita for the Icebreaker GUI apps.
        apt-get install -y --no-install-recommends \
            python3-gi \
            gir1.2-gtk-4.0 \
            gir1.2-adw-1 \
            libadwaita-1-0 \
            adwaita-icon-theme

        locale-gen en_US.UTF-8

        # Virtio kernel modules for QEMU/UTM emulated NICs and disk.
        # Without these in the initramfs, virtio-net / virtio-blk devices are
        # not detected during live-boot and the network interface is absent.
        cat >> /etc/initramfs-tools/modules << 'VIRTIO'
virtio
virtio_pci
virtio_net
virtio_blk
virtio_scsi
9p
9pnet_virtio
VIRTIO
        update-initramfs -u -k all 2>&1 | tail -3
    "

    # ── Display manager enablement + auto-login ────────────────────────
    if [ "$PROFILE" = "desktop" ]; then
        chroot "${ISO_CHROOT}" bash -c "systemctl enable gdm || true"
        chroot "${ISO_CHROOT}" bash -c "systemctl enable NetworkManager || true"

        chroot "${ISO_CHROOT}" bash -c "
            groupadd -rf icebreaker-users
            useradd -m -s /bin/bash -G sudo,icebreaker-users icebreaker
            echo 'icebreaker:icebreaker' | chpasswd
            echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/icebreaker
            # sudo refuses any sudoers file that is not mode 440 (world-readable = broken sudo).
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
    else
        chroot "${ISO_CHROOT}" bash -c "systemctl enable lightdm || true"
        chroot "${ISO_CHROOT}" bash -c "systemctl enable NetworkManager || true"

        chroot "${ISO_CHROOT}" bash -c "
            groupadd -rf autologin
            groupadd -rf icebreaker-users
            useradd -m -s /bin/bash -G sudo,autologin,icebreaker-users icebreaker
            echo 'icebreaker:icebreaker' | chpasswd
            echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/icebreaker
            # sudo refuses any sudoers file that is not mode 440 (world-readable = broken sudo).
            chmod 440 /etc/sudoers.d/icebreaker
            mkdir -p /etc/lightdm/lightdm.conf.d
            cat > /etc/lightdm/lightdm.conf.d/50-autologin.conf <<'LDMCFG'
[Seat:*]
autologin-user=icebreaker
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
LDMCFG
        "
    fi

    chroot "${ISO_CHROOT}" bash -c "apt-get clean && rm -rf /var/lib/apt/lists/*"
    chroot "${ISO_CHROOT}" bash -c "mkdir -p /var/log/icebreaker && chmod 777 /var/log/icebreaker"
    echo "icebreaker" > "${ISO_CHROOT}/etc/hostname"
    cat <<EOF > "${ISO_CHROOT}/etc/hosts"
127.0.0.1 localhost
127.0.1.1 icebreaker

# The following lines are desirable for IPv6 capable hosts
::1     ip6-localhost ip6-loopback
fe00::0 ip6-localnet
ff00::0 ip6-mcastprefix
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF

    # ── 5c: Overlay Icebreaker artifacts from Stage 4 ──────────────────
    info "Overlaying Icebreaker artifacts..."
    [ -d "${CHROOT}" ] || die "includes.chroot not found — run Stage 4 first"
    cp -a "${CHROOT}"/* "${ISO_CHROOT}/"

    # ── 5c2: Compile GSettings schemas (wallpaper override) ─────────────
    if [ -f "${ISO_CHROOT}/usr/share/glib-2.0/schemas/99_icebreaker.gschema.override" ]; then
        chroot "${ISO_CHROOT}" glib-compile-schemas /usr/share/glib-2.0/schemas/ 2>/dev/null || true
        info "GSettings schemas compiled (wallpaper override applied)"
    fi

    # ── 5c3: Enable Icebreaker systemd services ──────────────────────
    info "Enabling Icebreaker systemd services..."
    mkdir -p "${ISO_CHROOT}/etc/systemd/system/multi-user.target.wants"
    mkdir -p "${ISO_CHROOT}/etc/systemd/system/sockets.target.wants"
    # Note: icebreaker-qbd.service is intentionally excluded so only the Privileged Brain loads
    for svc in icebreaker-first-boot.service icebreaker-pbd.service \
               icebreaker-controller.service; do
        ln -sf "/etc/systemd/system/${svc}" \
            "${ISO_CHROOT}/etc/systemd/system/multi-user.target.wants/${svc}"
    done
    ln -sf /etc/systemd/system/icebreaker-controller.socket \
        "${ISO_CHROOT}/etc/systemd/system/sockets.target.wants/icebreaker-controller.socket"

    # ── 5d: Extract kernel + initrd ────────────────────────────────────
    info "Extracting kernel and initrd..."
    VMLINUZ=$(ls "${ISO_CHROOT}/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1)
    INITRD=$(ls "${ISO_CHROOT}/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1)
    [ -n "$VMLINUZ" ] || die "no vmlinuz found in chroot"
    [ -n "$INITRD" ]  || die "no initrd found in chroot"
    cp "$VMLINUZ" "${ISO_STAGING}/live/vmlinuz"
    cp "$INITRD"  "${ISO_STAGING}/live/initrd"
    info "Kernel: $(basename "$VMLINUZ")"

    # ── 5e: Unmount + create squashfs ──────────────────────────────────
    info "Unmounting chroot filesystems..."
    umount "${ISO_CHROOT}/dev/pts" 2>/dev/null || true
    umount "${ISO_CHROOT}/dev"     2>/dev/null || true
    umount "${ISO_CHROOT}/proc"    2>/dev/null || true
    umount "${ISO_CHROOT}/sys"     2>/dev/null || true

    info "Creating squashfs (this takes several minutes)..."
    mksquashfs "${ISO_CHROOT}" "${ISO_STAGING}/live/filesystem.squashfs" \
        -noI -noD -noF -noX -no-duplicates \
        -e boot/vmlinuz-\* boot/initrd.img-\* \
        2>&1 | tail -5
    info "Squashfs: $(du -h "${ISO_STAGING}/live/filesystem.squashfs" | awk '{print $1}')"

    # ── 5f: Set up isolinux bootloader ─────────────────────────────────
    info "Setting up isolinux bootloader..."

    ISOLINUX_BIN=""
    for p in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/syslinux/isolinux.bin; do
        [ -f "$p" ] && ISOLINUX_BIN="$p" && break
    done
    [ -n "$ISOLINUX_BIN" ] || die "isolinux.bin not found — install isolinux package"
    cp "$ISOLINUX_BIN" "${ISO_STAGING}/isolinux/"

    SYSLINUX_MOD=""
    for p in /usr/lib/syslinux/modules/bios /usr/lib/syslinux; do
        [ -f "${p}/ldlinux.c32" ] && SYSLINUX_MOD="$p" && break
    done
    [ -n "$SYSLINUX_MOD" ] || die "syslinux modules not found — install syslinux-common"
    for mod in ldlinux.c32 libcom32.c32 vesamenu.c32 libutil.c32; do
        [ -f "${SYSLINUX_MOD}/${mod}" ] && cp "${SYSLINUX_MOD}/${mod}" "${ISO_STAGING}/isolinux/"
    done

    ISOHDPFX=""
    for p in /usr/lib/ISOLINUX/isohdpfx.bin /usr/lib/syslinux/isohdpfx.bin \
             /usr/lib/syslinux/mbr/isohdpfx.bin; do
        [ -f "$p" ] && ISOHDPFX="$p" && break
    done
    [ -n "$ISOHDPFX" ] || die "isohdpfx.bin not found — install isolinux package"

    # Boot parameters differ by profile.
    if [ "$PROFILE" = "desktop" ]; then
        _LIVE_APPEND="boot=live toram quiet splash"
        _SAFE_APPEND="boot=live toram single nomodeset"
    else
        _LIVE_APPEND="boot=live nomodeset quiet splash"
        _SAFE_APPEND="boot=live nomodeset"
    fi

    cat > "${ISO_STAGING}/isolinux/isolinux.cfg" <<BOOTMENU
UI vesamenu.c32
PROMPT 0
TIMEOUT 50

MENU TITLE Icebreaker AI-Native OS

DEFAULT live

LABEL live
  MENU LABEL ^Icebreaker AI-Native OS (Live)
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_LIVE_APPEND}

LABEL live-safe
  MENU LABEL ^Safe Mode
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_SAFE_APPEND}
BOOTMENU

    # ── 5f2: Set up GRUB EFI bootloader ──────────────────────────────────
    # The isolinux setup above (5f) handles BIOS boot. This sub-stage adds
    # EFI boot support so the ISO works on UEFI firmware (VirtualBox on
    # macOS, OVMF/QEMU, post-2012 hardware). Both paths load the same
    # kernel with the same parameters.
    info "Setting up GRUB EFI bootloader..."

    command -v grub-mkstandalone >/dev/null 2>&1 || \
        die "grub-mkstandalone not found — install grub-efi-amd64-bin"

    GRUB_CFG_DIR="${ISO_WORK}/grub-embed"
    mkdir -p "${GRUB_CFG_DIR}"

    cat > "${GRUB_CFG_DIR}/grub.cfg" <<GRUBCFG
search --no-floppy --set=root --label ICEBREAKER

set default=0
set timeout=5

menuentry "Icebreaker AI-Native OS (Live)" {
    linux /live/vmlinuz ${_LIVE_APPEND}
    initrd /live/initrd
}

menuentry "Safe Mode" {
    linux /live/vmlinuz ${_SAFE_APPEND}
    initrd /live/initrd
}
GRUBCFG

    mkdir -p "${ISO_STAGING}/boot/grub"
    grub-mkstandalone \
        --format=x86_64-efi \
        --output="${ISO_STAGING}/boot/grub/BOOTX64.EFI" \
        --modules="part_gpt part_msdos fat iso9660 search search_label linux normal all_video test" \
        "boot/grub/grub.cfg=${GRUB_CFG_DIR}/grub.cfg"
    [ -f "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ] || \
        die "grub-mkstandalone failed to produce BOOTX64.EFI"
    info "GRUB EFI binary: $(du -h "${ISO_STAGING}/boot/grub/BOOTX64.EFI" | awk '{print $1}')"

    EFI_IMG="${ISO_STAGING}/boot/grub/efi.img"
    GRUB_SIZE_KB=$(( $(stat -c%s "${ISO_STAGING}/boot/grub/BOOTX64.EFI" 2>/dev/null || stat -f%z "${ISO_STAGING}/boot/grub/BOOTX64.EFI") / 1024 ))
    EFI_IMG_SIZE_KB=$(( GRUB_SIZE_KB + 1024 ))  # GRUB binary + 1 MB headroom for FAT metadata

    dd if=/dev/zero of="${EFI_IMG}" bs=1K count="${EFI_IMG_SIZE_KB}" 2>/dev/null
    mkfs.fat "${EFI_IMG}" >/dev/null
    mmd -i "${EFI_IMG}" ::EFI
    mmd -i "${EFI_IMG}" ::EFI/BOOT
    mcopy -i "${EFI_IMG}" "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ::EFI/BOOT/BOOTX64.EFI

    info "EFI image: $(du -h "${EFI_IMG}" | awk '{print $1}')"

    # ── 5g: Assemble ISO with xorriso ──────────────────────────────────
    info "Creating ISO image..."
    xorriso -as mkisofs \
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
        -o "${ISO_FILE}" \
        "${ISO_STAGING}/" 2>&1 | tail -5

    [ -f "${ISO_FILE}" ] || die "ISO file not created"

    sha256sum "$ISO_FILE" > "${BUILD_DIR}/iso.sha256"
    ISO_SIZE=$(du -h "$ISO_FILE" | awk '{print $1}')
    ISO_HASH=$(awk '{print $1}' "${BUILD_DIR}/iso.sha256")

    info "ISO built: ${ISO_FILE} (${ISO_SIZE})"
    info "SHA-256: ${ISO_HASH}"
    info "Build complete."
fi
