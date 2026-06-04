#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build_iso.sh — Build a custom Ubuntu 22.04 ISO with Privileged Brain
#
# What it produces:
#   privileged-brain-os-1.0-amd64.iso
#   - Ubuntu 22.04 LTS base
#   - Ollama pre-installed
#   - privileged-brain model bundled
#   - pb-daemon systemd service auto-starts on boot
#   - Shell integration active in every terminal
#   - Custom branding (grub, /etc/os-release)
#
# Run on Ubuntu 22.04 (can be a VM or the GCP training VM).
# Requires: ~20 GB free disk, internet access.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
ISO_NAME="privileged-brain-os-1.0-amd64"
GGUF_PATH="${GGUF_PATH:-$PARENT_DIR/training/gguf/privileged-brain.gguf}"
DEB_PATH="${DEB_PATH:-$PARENT_DIR/pb-daemon/privileged-brain-daemon_1.0_amd64.deb}"
WORK_DIR="/tmp/pb-iso-build"

echo "========================================"
echo " Privileged Brain OS — ISO Builder"
echo "========================================"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: Must run as root (sudo bash build_iso.sh)"
  exit 1
fi

if [ ! -f "$GGUF_PATH" ]; then
  echo "ERROR: GGUF not found at: $GGUF_PATH"
  echo "Set GGUF_PATH=/path/to/privileged-brain.gguf"
  exit 1
fi

if [ ! -f "$DEB_PATH" ]; then
  echo "Building deb package first..."
  cd "$PARENT_DIR/pb-daemon"
  bash build_deb.sh
fi

echo "Installing live-build..."
apt-get install -y live-build squashfs-tools xorriso isolinux syslinux-common 2>/dev/null

# ── Set up live-build workspace ───────────────────────────────────────────────
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

lb config \
  --distribution jammy \
  --architectures amd64 \
  --binary-images iso-hybrid \
  --debian-installer none \
  --bootappend-live "boot=live components quiet splash" \
  --iso-volume "PrivilegedBrainOS" \
  --image-name "$ISO_NAME"

# ── Package list ──────────────────────────────────────────────────────────────
mkdir -p config/package-lists
cat > config/package-lists/pb-base.list.chroot << 'EOF'
# Base utilities
curl wget git python3 python3-pip socat
# systemd tools
systemd-journal-remote
# eBPF support
bpfcc-tools python3-bpfcc linux-headers-generic
# Ollama runtime deps
ca-certificates gnupg
# Python packages for pb-daemon
python3-systemd
EOF

# ── Hooks: run inside the chroot ──────────────────────────────────────────────
mkdir -p config/hooks/live

# Hook 1: Install Ollama
cat > config/hooks/live/0010-install-ollama.hook.chroot << 'EOF'
#!/bin/bash
set -e
echo "Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable ollama
EOF
chmod +x config/hooks/live/0010-install-ollama.hook.chroot

# Hook 2: Install pb-daemon .deb
cat > config/hooks/live/0020-install-pb-daemon.hook.chroot << 'EOF'
#!/bin/bash
set -e
echo "Installing pb-daemon..."
dpkg -i /tmp/pb-daemon.deb || apt-get install -f -y
EOF
chmod +x config/hooks/live/0020-install-pb-daemon.hook.chroot

# Hook 3: Import model into Ollama (run after boot from a startup script)
# The GGUF is too large to bake into the ISO chroot directly.
# Instead, we create a first-boot service that imports it from /opt/pb-model/
cat > config/hooks/live/0030-first-boot-model.hook.chroot << 'EOF'
#!/bin/bash
set -e
mkdir -p /opt/pb-model

# Create a one-shot systemd unit that imports the model on first boot
cat > /etc/systemd/system/pb-import-model.service << 'SVCEOF'
[Unit]
Description=Import Privileged Brain model into Ollama (first boot)
After=ollama.service
Wants=ollama.service
ConditionPathExists=!/var/lib/pb-model-imported

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 5
ExecStart=/bin/bash -c 'cd /opt/pb-model && ollama create privileged-brain -f Modelfile && touch /var/lib/pb-model-imported'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl enable pb-import-model.service
EOF
chmod +x config/hooks/live/0030-first-boot-model.hook.chroot

# Hook 4: Branding
cat > config/hooks/live/0040-branding.hook.chroot << 'EOF'
#!/bin/bash
set -e
cat > /etc/os-release << 'OSEOF'
NAME="Privileged Brain OS"
VERSION="1.0 LTS"
ID=privileged-brain-os
ID_LIKE=ubuntu
PRETTY_NAME="Privileged Brain OS 1.0 LTS"
VERSION_ID="1.0"
HOME_URL="https://github.com/privileged-brain"
SUPPORT_URL="https://github.com/privileged-brain"
BUG_REPORT_URL="https://github.com/privileged-brain/issues"
OSEOF

# MOTD
cat > /etc/motd << 'MOTDEOF'

  ██████╗ ██████╗ ██╗██╗   ██╗
  ██╔══██╗██╔══██╗██║██║   ██║
  ██████╔╝██████╔╝██║██║   ██║
  ██╔═══╝ ██╔══██╗██║╚██╗ ██╔╝
  ██║     ██║  ██║██║ ╚████╔╝
  ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═══╝

  Privileged Brain OS — AI-Native Linux

  Try:  ai "list all listening ports"
        ai "why is my disk full"
  Ctrl+G after a failed command → AI explains the error

MOTDEOF
EOF
chmod +x config/hooks/live/0040-branding.hook.chroot

# ── Copy files into the chroot ────────────────────────────────────────────────
mkdir -p config/includes.chroot/tmp
mkdir -p config/includes.chroot/opt/pb-model

# Copy the .deb
cp "$DEB_PATH" config/includes.chroot/tmp/pb-daemon.deb

# Copy the GGUF and Modelfile
cp "$GGUF_PATH" config/includes.chroot/opt/pb-model/privileged-brain.gguf
cat > config/includes.chroot/opt/pb-model/Modelfile << 'EOF'
FROM /opt/pb-model/privileged-brain.gguf

PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER num_ctx 4096
PARAMETER stop "<|im_end|>"

SYSTEM """You are Privileged Brain, an AI-native system administrator.
You give precise, runnable shell commands and brief explanations.
You are built into this Linux OS and have deep knowledge of system administration."""
EOF

# ── Build the ISO ─────────────────────────────────────────────────────────────
echo ""
echo "Building ISO (this takes 15–30 minutes)..."
lb build

OUTPUT="$SCRIPT_DIR/${ISO_NAME}.iso"
mv "$WORK_DIR/${ISO_NAME}.hybrid.iso" "$OUTPUT" 2>/dev/null || \
mv "$WORK_DIR/live-image-amd64.hybrid.iso" "$OUTPUT" 2>/dev/null

echo ""
echo "========================================"
echo " ✓ ISO built: $OUTPUT"
echo ""
echo " Test in a VM:"
echo "   VirtualBox: New VM → Use ISO as boot disk"
echo "   UTM (Mac):  New VM → Linux → Use ISO"
echo "   QEMU:"
echo "     qemu-system-x86_64 -m 4G -enable-kvm \\"
echo "       -cdrom $OUTPUT -boot d"
echo "========================================"
