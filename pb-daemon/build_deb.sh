#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build_deb.sh — Build a .deb package for pb-daemon
# Run on any Ubuntu/Debian system.
# Output: privileged-brain-daemon_1.0_amd64.deb
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_NAME="privileged-brain-daemon"
PKG_VER="1.0"
PKG_ARCH="amd64"
DEB_DIR="/tmp/${PKG_NAME}_${PKG_VER}"

echo "Building ${PKG_NAME}_${PKG_VER}_${PKG_ARCH}.deb ..."

# ── Directory structure ───────────────────────────────────────────────────────
rm -rf "$DEB_DIR"
mkdir -p \
  "$DEB_DIR/DEBIAN" \
  "$DEB_DIR/usr/local/bin" \
  "$DEB_DIR/usr/local/lib/pb-daemon" \
  "$DEB_DIR/etc/profile.d" \
  "$DEB_DIR/etc/systemd/system" \
  "$DEB_DIR/var/log"

# ── DEBIAN control file ───────────────────────────────────────────────────────
cat > "$DEB_DIR/DEBIAN/control" << EOF
Package: $PKG_NAME
Version: $PKG_VER
Architecture: $PKG_ARCH
Maintainer: Privileged Brain Project
Depends: python3 (>= 3.8), python3-systemd, ollama
Recommends: bpfcc-tools, python3-bpfcc, socat
Description: Privileged Brain AI System Daemon
 Integrates a fine-tuned LLM into the Linux OS.
 Monitors journald, /proc, and kernel events via eBPF.
 Provides shell integration (ai command, Ctrl+G, command_not_found).
EOF

# ── Post-install script ───────────────────────────────────────────────────────
cat > "$DEB_DIR/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e
systemctl daemon-reload
systemctl enable pb-daemon.service
systemctl start  pb-daemon.service || true
echo "pb-daemon installed and started."
echo "Shell integration: open a new terminal or run: source /etc/profile.d/pb-shell.sh"
EOF
chmod 755 "$DEB_DIR/DEBIAN/postinst"

# ── Pre-remove script ─────────────────────────────────────────────────────────
cat > "$DEB_DIR/DEBIAN/prerm" << 'EOF'
#!/bin/bash
set -e
systemctl stop    pb-daemon.service 2>/dev/null || true
systemctl disable pb-daemon.service 2>/dev/null || true
EOF
chmod 755 "$DEB_DIR/DEBIAN/prerm"

# ── Install files ─────────────────────────────────────────────────────────────
# Main daemon
cp "$SCRIPT_DIR/pb_daemon.py"     "$DEB_DIR/usr/local/bin/pb-daemon"
chmod 755 "$DEB_DIR/usr/local/bin/pb-daemon"

# CLI tool
cp "$SCRIPT_DIR/pb_ask.sh"        "$DEB_DIR/usr/local/bin/pb-ask"
chmod 755 "$DEB_DIR/usr/local/bin/pb-ask"

# eBPF module
cp "$SCRIPT_DIR/pb_ebpf.py"       "$DEB_DIR/usr/local/lib/pb-daemon/pb-ebpf"
chmod 755 "$DEB_DIR/usr/local/lib/pb-daemon/pb-ebpf"
# Symlink to PATH
ln -sf /usr/local/lib/pb-daemon/pb-ebpf "$DEB_DIR/usr/local/bin/pb-ebpf"

# Shell integration
cp "$SCRIPT_DIR/pb-shell.sh"      "$DEB_DIR/etc/profile.d/pb-shell.sh"
chmod 644 "$DEB_DIR/etc/profile.d/pb-shell.sh"

# systemd units
cp "$SCRIPT_DIR/pb-daemon.service"    "$DEB_DIR/etc/systemd/system/"
cp "$SCRIPT_DIR/pb-notify@.service"   "$DEB_DIR/etc/systemd/system/"

# Placeholder log file
touch "$DEB_DIR/var/log/pb-daemon.log"

# ── Build the .deb ────────────────────────────────────────────────────────────
OUTPUT="$SCRIPT_DIR/${PKG_NAME}_${PKG_VER}_${PKG_ARCH}.deb"
dpkg-deb --build "$DEB_DIR" "$OUTPUT"

echo ""
echo "✓ Built: $OUTPUT"
echo ""
echo "Install on Ubuntu/Debian:"
echo "  sudo dpkg -i $OUTPUT"
echo "  sudo apt-get install -f   # fix any missing deps"
