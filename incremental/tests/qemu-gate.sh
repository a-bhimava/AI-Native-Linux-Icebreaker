#!/usr/bin/env bash
# qemu-gate.sh — headless boot gate for an incremental ISO.
#
# Usage: bash incremental/tests/qemu-gate.sh <iso-path> <version-level>
#
# Boots the ISO headless in QEMU with an ssh port-forward, waits for sshd,
# then runs level-gated assertions inside the guest. Requires sshpass on
# the host (apt-get install sshpass). Uses KVM when available, falls back
# to TCG emulation (slow — timeout auto-extends).
#
# Exit 0 = gate PASS. Non-zero = FAIL with the failed check named.

set -uo pipefail

ISO="${1:?usage: qemu-gate.sh <iso> <level>}"
LEVEL="${2:?usage: qemu-gate.sh <iso> <level>}"
[ -f "$ISO" ] || { echo "FATAL: ISO not found: $ISO" >&2; exit 2; }
command -v qemu-system-x86_64 >/dev/null || { echo "FATAL: qemu-system-x86_64 not installed" >&2; exit 2; }
command -v sshpass >/dev/null || { echo "FATAL: sshpass not installed (apt-get install sshpass)" >&2; exit 2; }

SSH_PORT=2299
GUEST_USER=icebreaker
GUEST_PASS=icebreaker
SERIAL_LOG="$(mktemp /tmp/qemu-gate-serial.XXXXXX.log)"
FAILURES=0

info() { echo -e "\033[0;32m[qemu-gate]\033[0m $*"; }
pass() { echo -e "  \033[0;32m[PASS]\033[0m $*"; }
fail() { echo -e "  \033[0;31m[FAIL]\033[0m $*"; FAILURES=$((FAILURES+1)); }

# ── KVM detection ───────────────────────────────────────────────────────
QEMU_ACCEL=()
BOOT_TIMEOUT=420          # 7 min with KVM
if [ -w /dev/kvm ] 2>/dev/null || [ -c /dev/kvm ]; then
    QEMU_ACCEL=(-enable-kvm -cpu host)
    info "KVM available"
else
    QEMU_ACCEL=(-cpu max)
    BOOT_TIMEOUT=1800     # 30 min under TCG emulation
    info "No KVM — falling back to TCG emulation (slow; timeout ${BOOT_TIMEOUT}s)"
fi

# ── Launch QEMU ─────────────────────────────────────────────────────────
info "Booting $(basename "$ISO") (ssh forward localhost:${SSH_PORT})..."
qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -m 8G -smp 4 \
    -boot d -cdrom "$ISO" \
    -display none \
    -serial "file:${SERIAL_LOG}" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
    -device virtio-net-pci,netdev=n0 \
    &
QEMU_PID=$!
cleanup() { kill "$QEMU_PID" 2>/dev/null || true; wait "$QEMU_PID" 2>/dev/null || true; }
trap cleanup EXIT

_ssh() {
    sshpass -p "$GUEST_PASS" ssh -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR \
        -p "$SSH_PORT" "${GUEST_USER}@127.0.0.1" "$@" 2>/dev/null
}

# ── Wait for sshd ───────────────────────────────────────────────────────
info "Waiting for guest sshd (up to ${BOOT_TIMEOUT}s)..."
START=$(date +%s)
BOOTED=0
while [ $(( $(date +%s) - START )) -lt "$BOOT_TIMEOUT" ]; do
    kill -0 "$QEMU_PID" 2>/dev/null || { echo "FATAL: QEMU exited early — see $SERIAL_LOG" >&2; exit 1; }
    if _ssh true; then BOOTED=1; break; fi
    sleep 10
done
if [ "$BOOTED" != "1" ]; then
    fail "guest never became ssh-reachable in ${BOOT_TIMEOUT}s (serial log: $SERIAL_LOG)"
    tail -30 "$SERIAL_LOG" || true
    exit 1
fi
info "Guest up after $(( $(date +%s) - START ))s"

# ═══ Level 0 ═══
echo "[L0] Boot assertions"
_ssh "systemctl is-active graphical.target" | grep -q active \
    && pass "graphical.target active" || fail "graphical.target not active"
_ssh "systemctl is-active gdm" | grep -q active \
    && pass "gdm active" || fail "gdm not active"
V="$(_ssh "cat /etc/icebreaker-version")"
[ "$V" = "v${LEVEL}" ] && pass "version marker = v${LEVEL}" || fail "version marker '$V' != expected 'v${LEVEL}'"
_ssh "systemctl --failed --no-legend" | grep -q . \
    && fail "failed units: $(_ssh 'systemctl --failed --no-legend' | awk '{print $1}' | tr '\n' ' ')" \
    || pass "no failed systemd units"

# ═══ Level 1 ═══
if [ "$LEVEL" -ge 1 ]; then
    echo "[L1] Diagnostics"
    _ssh "ib-debug snapshot --no-color" >/dev/null \
        && pass "ib-debug snapshot runs" || fail "ib-debug snapshot crashed"
fi

# ═══ Level 2 ═══
if [ "$LEVEL" -ge 2 ]; then
    echo "[L2] Controller daemon"
    _ssh "systemctl is-active icebreaker-controller" | grep -q active \
        && pass "controller active" || fail "icebreaker-controller not active: $(_ssh 'systemctl status icebreaker-controller --no-pager -n 5' | tail -5)"
    _ssh "test -S /run/icebreaker/controller.sock" \
        && pass "controller.sock exists" || fail "controller.sock missing (PKG-4?)"
    RESP="$(_ssh "echo '{\"jsonrpc\":\"2.0\",\"method\":\"status\",\"id\":1}' | timeout 5 socat - UNIX:/run/icebreaker/controller.sock" || true)"
    echo "$RESP" | grep -q '"result"' \
        && pass "daemon responds to status RPC" || fail "no RPC response from daemon (got: ${RESP:0:100})"
fi

# ═══ Level 6 ═══
if [ "$LEVEL" -ge 6 ]; then
    echo "[L6] PB + mcpd runtime"
    _ssh "systemctl is-active icebreaker-pbd" | grep -q active \
        && pass "pbd active" || fail "icebreaker-pbd not active"
    _ssh "test -S /run/icebreaker/pbd.sock" \
        && pass "pbd.sock exists" || fail "pbd.sock missing"
    _ssh "ss -tlnp 2>/dev/null | grep -q mcpd" \
        && fail "INV-3 VIOLATION: mcpd has a TCP listener" || pass "mcpd has no TCP listeners (INV-3)"
fi

# ── Verdict ─────────────────────────────────────────────────────────────
echo "──────────────────────────────"
if [ "$FAILURES" -gt 0 ]; then
    echo -e "\033[0;31mQEMU GATE: ${FAILURES} FAILURE(S) — v${LEVEL} stays RED\033[0m"
    echo "Serial log: $SERIAL_LOG"
    exit 1
fi
echo -e "\033[0;32mQEMU GATE: PASS (level ${LEVEL})\033[0m"
echo "Remember: UTM GUI verification is still required before marking GREEN (R1)."
exit 0
