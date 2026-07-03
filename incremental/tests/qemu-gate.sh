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

# ── Stale-QEMU cleanup + per-run port (F-18) ────────────────────────────
# A leftover gate QEMU holding the ssh forward makes the new QEMU launch
# WITHOUT forwarding (the hostfwd error is non-fatal), and ssh then reaches
# the ORPHANED guest — the gate silently tests the wrong ISO. Kill stale
# gate QEMUs and use a random ephemeral port per run.
pkill -f 'qemu-system-x86_64.*icebreaker-v[0-9]+\.iso' 2>/dev/null && \
    { info "Killed stale gate QEMU (F-18)"; sleep 2; } || true
SSH_PORT="${QEMU_GATE_PORT:-$((20000 + RANDOM % 20000))}"

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
# graphical.target keeps activating for minutes after sshd is up (especially
# under TCG) — poll with a grace period instead of a single instant check (F-13).
GRACE=120
[ "${QEMU_ACCEL[0]}" = "-cpu" ] && GRACE=600   # TCG: GNOME startup is ~5x slower
GT_OK=0
GT_START=$(date +%s)
while [ $(( $(date +%s) - GT_START )) -lt "$GRACE" ]; do
    if _ssh "systemctl is-active graphical.target" | grep -qx active; then
        GT_OK=1; break
    fi
    sleep 15
done
if [ "$GT_OK" = "1" ]; then
    pass "graphical.target active (after $(( $(date +%s) - GT_START ))s grace)"
else
    fail "graphical.target not active after ${GRACE}s (state: $(_ssh 'systemctl is-active graphical.target'; _ssh 'systemctl list-jobs --no-legend' | head -3))"
fi
_ssh "systemctl is-active gdm" | grep -q active \
    && pass "gdm active" || fail "gdm not active"
V="$(_ssh "cat /etc/icebreaker-version")"
# F-18: a marker mismatch means we are talking to the WRONG guest (stale
# QEMU / port collision) — every further check would be meaningless. Abort.
if [ "$V" = "v${LEVEL}" ]; then
    pass "version marker = v${LEVEL}"
else
    fail "version marker '$V' != expected 'v${LEVEL}' — WRONG GUEST (stale QEMU?). Aborting gate."
    exit 1
fi
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
    PERMS="$(_ssh "stat -c '%a %U %G' /run/icebreaker/controller.sock" || true)"
    echo "$PERMS" | grep -q "660 root icebreaker-users" \
        && pass "socket perms 0660 root:icebreaker-users (PKG-4)" \
        || fail "socket perms wrong: '${PERMS}' (expected 660 root icebreaker-users)"
    # F-17: keep stdin open while awaiting the reply — `echo | socat` half-closes
    # the socket on stdin EOF and the daemon tears the session down before the
    # response is delivered. `(printf …; sleep N) | socat` holds the connection.
    RESP="$(_ssh "(printf '%s\n' '{\"jsonrpc\":\"2.0\",\"method\":\"daemon.status\",\"id\":1}'; sleep 5) | timeout 8 socat -t 5 - UNIX:/run/icebreaker/controller.sock" || true)"
    echo "$RESP" | grep -q '"result"' \
        && pass "daemon.status RPC responds" || fail "no RPC response from daemon (got: ${RESP:0:100})"
    # turn.run must return a graceful structured error (QB unconfigured until V5),
    # never crash the daemon (R6 / F-5). Response streams notifications, then a
    # final message carrying "id":2. Param is 'input' (daemon.py _METHODS table).
    TURN="$(_ssh "(printf '%s\n' '{\"jsonrpc\":\"2.0\",\"method\":\"turn.run\",\"params\":{\"input\":\"hello\"},\"id\":2}'; sleep 25) | timeout 30 socat -t 25 - UNIX:/run/icebreaker/controller.sock" || true)"
    if echo "$TURN" | grep -q '"id": *2'; then
        echo "$TURN" | grep -qi "not available\|GEMINI_API_KEY\|unconfigured" \
            && pass "turn.run returns actionable QB-unconfigured error (R6)" \
            || pass "turn.run returned a final response"
    else
        fail "turn.run produced no final response (got: ${TURN:0:120})"
    fi
    _ssh "systemctl is-active icebreaker-controller" | grep -q active \
        && pass "controller still active after turn.run" || fail "controller crashed after turn.run"
    # TCG runs the daemon's Python startup ~5x slower than KVM/native (F-16);
    # a genuine wait-for-sockets stall would read 60s+, well above either limit.
    RESTART_MAX=15
    [ "${QEMU_ACCEL[0]}" = "-cpu" ] && RESTART_MAX=45
    RESTART_T0=$(date +%s)
    if _ssh "sudo systemctl restart icebreaker-controller && systemctl is-active icebreaker-controller" | grep -q active; then
        RESTART_DT=$(( $(date +%s) - RESTART_T0 ))
        [ "$RESTART_DT" -le "$RESTART_MAX" ] \
            && pass "restart → active in ${RESTART_DT}s (limit ${RESTART_MAX}s)" \
            || fail "restart took ${RESTART_DT}s (limit ${RESTART_MAX}s — check wait-for-sockets stall)"
    else
        fail "controller did not return to active after restart"
    fi
fi

# ═══ Level 3 ═══
if [ "$LEVEL" -ge 3 ]; then
    echo "[L3] Terminal TUI (headless checks — visual gate is UTM, R1)"
    _ssh "/opt/icebreaker/venv/bin/python3 -m terminal --help 2>&1" | grep -qi "usage" \
        && pass "python3 -m terminal --help" || fail "terminal module crashes on --help"
    _ssh "test -f /usr/share/applications/icebreaker-terminal.desktop" \
        && pass "terminal .desktop installed" || fail "icebreaker-terminal.desktop missing"
    _ssh "test -f /etc/xdg/autostart/icebreaker-terminal-autostart.desktop" \
        && pass "autostart .desktop installed" || fail "autostart .desktop missing"
    _ssh "test -x /usr/libexec/icebreaker/ib-wait-sock" \
        && pass "ib-wait-sock helper installed" || fail "ib-wait-sock missing (F-9)"
    # F-7 behavioral check: with the daemon STOPPED, the TUI must print the
    # explicit warning to stderr. Capture stderr ONLY (2>&1 >/dev/null): the
    # Textual UI floods stdout with escape codes and drowned the warning in
    # the first attempt. Timeout scales with accelerator (F-16 rule) — python
    # + textual imports take ~30-50 s under TCG before the warning appears.
    F7_TIMEOUT=45
    [ "${QEMU_ACCEL[0]}" = "-cpu" ] && F7_TIMEOUT=150
    F7_OUT="$(_ssh "sudo systemctl stop icebreaker-controller; timeout ${F7_TIMEOUT} /opt/icebreaker/venv/bin/python3 -m terminal --sock /run/icebreaker/controller.sock --connect-timeout 3 2>&1 >/dev/null | head -3; sudo systemctl start icebreaker-controller" || true)"
    echo "$F7_OUT" | grep -q "Daemon unreachable" \
        && pass "F-7: explicit daemon-unreachable warning printed" \
        || fail "F-7: no visible warning when daemon is down (got: ${F7_OUT:0:100})"
fi

# ═══ Level 4 ═══
if [ "$LEVEL" -ge 4 ]; then
    echo "[L4] # trigger"
    # F-19: test through a REAL pty (script -qec) — `bash -ic` bypasses
    # readline/prompt machinery and passed while Enter was completely broken.
    # The expected output 'plain-42' is computed at runtime so it can never
    # match the input echo.
    PLAIN="$(_ssh "printf 'echo plain-\$((40+2))\r' | timeout 30 script -qec bash /dev/null" || true)"
    echo "$PLAIN" | grep -q "plain-42" \
        && pass "plain commands execute through pty (F-19)" \
        || fail "F-19: interactive Enter broken — command not executed (got: ${PLAIN:0:100})"
    _ssh "grep -qF 'source /usr/share/icebreaker/shell/ib_trigger.bash' /home/icebreaker/.bashrc" \
        && pass "trigger sourced in user .bashrc (F-6)" || fail "F-6: trigger not wired into /home/icebreaker/.bashrc"
    # Behavioral: run the SHIPPED runner (same file the trigger calls, F-17).
    # Timeout scales with accelerator (F-16 rule).
    L4_TIMEOUT=30
    [ "${QEMU_ACCEL[0]}" = "-cpu" ] && L4_TIMEOUT=120
    IBRUN="$(_ssh "timeout ${L4_TIMEOUT} /opt/icebreaker/venv/bin/python3 /usr/share/icebreaker/shell/ib_run.py 'hello' /run/icebreaker/controller.sock 2>&1" || true)"
    if [ "$LEVEL" -eq 4 ]; then
        # QB has no key until V5 — the actionable error IS the pass condition (R6).
        echo "$IBRUN" | grep -qi "GEMINI_API_KEY\|Quarantined Brain" \
            && pass "ib_run.py returns actionable QB-unconfigured error" \
            || fail "ib_run.py gave no actionable error (got: ${IBRUN:0:120})"
    else
        [ -n "$IBRUN" ] \
            && pass "ib_run.py returned output" \
            || fail "ib_run.py produced no output"
    fi
    # End-to-end trigger through a real pty: '# <query>' typed at a live
    # prompt must produce the [icebreaker] echo AND a daemon response (F-19).
    TRIG="$(_ssh "printf '# gate-ping\r' | timeout ${L4_TIMEOUT} script -qec bash /dev/null 2>/dev/null" || true)"
    if echo "$TRIG" | grep -q "icebreaker\]"; then
        if [ "$LEVEL" -eq 4 ]; then
            echo "$TRIG" | grep -qi "GEMINI_API_KEY\|Quarantined Brain" \
                && pass "# trigger end-to-end via pty (echo + daemon error)" \
                || fail "# trigger echoed but no daemon response (got: $(echo "$TRIG" | tail -2 | head -c 120))"
        else
            pass "# trigger end-to-end via pty"
        fi
    else
        fail "F-19: # trigger did not fire through pty (got: $(echo "$TRIG" | tail -2 | head -c 120))"
    fi
fi

# ═══ Level 5 ═══
if [ "$LEVEL" -ge 5 ]; then
    echo "[L5] QB key flow (no live key in CI — with-key half is the UTM gate)"
    _ssh "test -x /usr/local/bin/ib-setup-key" \
        && pass "ib-setup-key installed" || fail "ib-setup-key missing"
    _ssh "sudo stat -c '%a %U' /etc/icebreaker/locations.env" | grep -q "600 root" \
        && pass "locations.env 0600 root (BP-8)" || fail "locations.env perms/owner wrong (BP-8)"
    _ssh "sudo grep -qE '^GEMINI_API_KEY=' /etc/icebreaker/locations.env" \
        && fail "BP-8 VIOLATION: live key present in booted image" \
        || pass "no live key in image (BP-8)"
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
