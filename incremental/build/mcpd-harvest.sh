#!/usr/bin/env bash
# mcpd-harvest.sh — F-33 systemic pre-build seccomp audit.
#
# Blocks build-iso.sh: if any mcpd tool call surfaces a syscall outside
# the allowlist (Phase A) or actually dies with SIGSYS (Phase B), the ISO
# is never produced. This is what should have caught F-33 (SYS_fsync
# missing from allowlist while safe_write calls file.sync_all()) BEFORE
# v6.3 was built and downloaded — instead we found it on UTM.
#
# Usage: sudo bash mcpd-harvest.sh [--mcpd /path/to/mcpd]
# Default binary: cx-distro/.build/mcpd (matches build-iso.sh convention)
#
# Requires: root (for dmesg -c), an mcpd binary built with seccomp
#           (real Linux; on macOS the sandbox is a no-op → script no-ops).
#
# The two-phase discovery procedure is documented in
# src/mcpd/src/sandbox/seccomp.rs:187-193. This script wires that comment
# into an enforced gate.

set -uo pipefail

# ── Style / plumbing ─────────────────────────────────────────────────────────
info() { echo -e "\033[0;36m[harvest]\033[0m $*"; }
pass() { echo -e "\033[0;32m[  OK  ]\033[0m $*"; }
warn() { echo -e "\033[1;33m[ WARN ]\033[0m $*"; }
fail() { echo -e "\033[0;31m[ FAIL ]\033[0m $*" >&2; }
die()  { fail "$*"; exit 1; }

# ── Skip on non-Linux (macOS dev loop: sandbox is a no-op) ───────────────────
if [ "$(uname -s)" != "Linux" ]; then
    warn "not on Linux — mcpd sandbox is a no-op here; skipping harvest gate"
    exit 0
fi

# ── Args ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MCPD="${REPO_ROOT}/cx-distro/.build/mcpd"

while [ $# -gt 0 ]; do
    case "$1" in
        --mcpd) MCPD="$2"; shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done

[ -x "$MCPD" ] || die "mcpd binary not executable: $MCPD (rebuild first: cd src/mcpd && cargo build --release)"
[ "$(id -u)" = "0" ] || die "must run as root (dmesg requires it)"

# ── Scratch env ──────────────────────────────────────────────────────────────
SCRATCH="$(mktemp -d /tmp/mcpd-harvest.XXXXXX)"
trap 'rm -rf "${SCRATCH}"' EXIT

# All fs.* calls target files inside $SCRATCH so we never touch real user data.
# HOME + MCPD_FS_READ_ROOTS both point to $SCRATCH so mcpd's Landlock+userspace
# allowlist admits reads/writes there. The seccomp filter — the layer under
# test — is unaffected by these env vars.
export HOME="$SCRATCH"
export MCPD_FS_READ_ROOTS="$SCRATCH"

# ── JSON-RPC exercise sequence ───────────────────────────────────────────────
# Every documented tool. fs.write is the specific call that crashed v6.3
# (fsync missing). fs.delete stays a Tier-3 op returning a COW preview, not
# an actual unlink, so no HITL is required.
build_requests() {
    local target_file="${SCRATCH}/harvest.txt"
    cat <<REQ
{"jsonrpc":"2.0","method":"system.status","params":{},"id":1}
{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}
{"jsonrpc":"2.0","method":"fs.list","params":{"path":"${SCRATCH}"},"id":3}
{"jsonrpc":"2.0","method":"fs.write","params":{"path":"${target_file}","content":"harvest-$(date +%s)"},"id":4}
{"jsonrpc":"2.0","method":"fs.read","params":{"path":"${target_file}"},"id":5}
{"jsonrpc":"2.0","method":"fs.stat","params":{"path":"${target_file}"},"id":6}
{"jsonrpc":"2.0","method":"fs.delete","params":{"path":"${target_file}"},"id":7}
{"jsonrpc":"2.0","method":"process.list","params":{},"id":8}
{"jsonrpc":"2.0","method":"package.query","params":{"name":"bash"},"id":9}
{"jsonrpc":"2.0","method":"system.unsupported","params":{"requested_intent":"harvest gate exercise","suggestion":"echo — this is F-35's landing pad","alternative_actions":["fs.list"]},"id":10}
REQ
}

# Run mcpd with a request sequence on stdin, capture stdout + stderr, return exit code.
run_mcpd_session() {
    local out_file="$1" err_file="$2" pid_file="$3"
    build_requests | timeout 60 "$MCPD" >"$out_file" 2>"$err_file" &
    echo $! > "$pid_file"
    wait "$(cat "$pid_file")"
    return $?
}

# ── Phase A — discovery under MCPD_SECCOMP_LOG_ONLY=1 ────────────────────────
info "═══ Phase A: seccomp discovery (LOG_ONLY=1) ═══"

# Clear kernel audit buffer so we only see this run's records.
dmesg -c >/dev/null 2>&1 || warn "dmesg -c returned nonzero (buffer already empty is OK)"

A_OUT="$(mktemp)"; A_ERR="$(mktemp)"; A_PID="$(mktemp)"
export MCPD_SECCOMP_LOG_ONLY=1
run_mcpd_session "$A_OUT" "$A_ERR" "$A_PID" || warn "Phase A mcpd exited nonzero (may be fine — reads happen on all method calls)"
unset MCPD_SECCOMP_LOG_ONLY

RESP_COUNT_A=$(grep -c '"jsonrpc"' "$A_OUT" || true)
info "Phase A: mcpd emitted ${RESP_COUNT_A} JSON-RPC responses (expected 10 — 9 real tools + F-35 system.unsupported)"

# F-35: verify system.unsupported specifically responded correctly under LOG_ONLY.
# It's a passthrough tool so the only failure mode is "response missing" (which
# would indicate schema rejection or dispatch bug) — not a seccomp gap.
if ! grep -q '"id":10' "$A_OUT"; then
    fail "Phase A: system.unsupported (id=10) did not respond — F-35 landing pad broken"
    fail "stderr tail:"
    tail -20 "$A_ERR" >&2
    rm -f "$A_OUT" "$A_ERR" "$A_PID"
    exit 1
fi

# Any syscall in the allowlist mismatch becomes 'Log' → shows up in dmesg
# as: audit: type=1326 audit(...): auid=... syscall=NNN ...
# In LOG_ONLY mode the syscall itself still executes, so mcpd doesn't die
# from this — but we now know N syscalls need to be added to the allowlist.
MISSING_SYSCALLS="$(dmesg 2>/dev/null | grep -E 'audit.*type=1326.*syscall=' | \
    grep -oE 'syscall=[0-9]+' | sort -u | cut -d= -f2)"

if [ -z "${MISSING_SYSCALLS}" ]; then
    pass "Phase A: no missing syscalls under LOG_ONLY (allowlist covers exercised surface)"
else
    fail "Phase A: allowlist is missing the following syscalls:"
    for nr in $MISSING_SYSCALLS; do
        name="$(ausyscall "$nr" 2>/dev/null || echo "syscall_${nr}")"
        fail "    syscall #${nr}  =  ${name}"
    done
    fail ""
    fail "Add each to allowed_syscalls() in src/mcpd/src/sandbox/seccomp.rs,"
    fail "rebuild mcpd, and re-run this gate. Do NOT ship an ISO with this list non-empty."
    rm -f "$A_OUT" "$A_ERR" "$A_PID"
    exit 1
fi

# ── Phase B — enforcement (real production filter) ───────────────────────────
info "═══ Phase B: seccomp enforcement (real allowlist, default KillProcess) ═══"

B_OUT="$(mktemp)"; B_ERR="$(mktemp)"; B_PID="$(mktemp)"
# MCPD_SECCOMP_LOG_ONLY is now unset → allowlist mismatch = KillProcess = SIGSYS.
run_mcpd_session "$B_OUT" "$B_ERR" "$B_PID"
B_EXIT=$?

# SIGSYS is signal 31 → exit code 128+31 = 159 (bash convention)
# timeout(1)'s own 124 = script wall-clock timeout
if [ "$B_EXIT" = "159" ]; then
    fail "Phase B: mcpd killed with SIGSYS (exit 159) — a syscall in the exercised path is not in the allowlist"
    fail "Phase A should have caught this; check that dmesg is readable and audit records are enabled"
    fail ""
    fail "stderr tail:"
    tail -20 "$B_ERR" >&2
    rm -f "$B_OUT" "$B_ERR" "$B_PID"
    exit 1
fi

RESP_COUNT_B=$(grep -c '"jsonrpc"' "$B_OUT" || true)
if [ "$RESP_COUNT_B" -lt "$RESP_COUNT_A" ]; then
    fail "Phase B: mcpd emitted only ${RESP_COUNT_B} responses (Phase A had ${RESP_COUNT_A})"
    fail "Some tool call was killed mid-response — inspect stderr:"
    tail -20 "$B_ERR" >&2
    rm -f "$B_OUT" "$B_ERR" "$B_PID"
    exit 1
fi

pass "Phase B: mcpd survived full tool exercise under real seccomp (${RESP_COUNT_B} responses, exit ${B_EXIT})"

# F-35 Phase C: verify the landing pad also survived real seccomp (already
# checked via RESP_COUNT_B against RESP_COUNT_A above, but call it out).
if grep -q '"id":10' "$B_OUT"; then
    pass "Phase C (F-35): system.unsupported landing pad routed cleanly under real seccomp"
else
    fail "Phase C (F-35): system.unsupported (id=10) missing from real-seccomp responses"
    tail -20 "$B_ERR" >&2
    rm -f "$B_OUT" "$B_ERR" "$B_PID"
    exit 1
fi

# ── Report + exit ────────────────────────────────────────────────────────────
info "═══ Harvest gate GREEN — ISO build may proceed ═══"
info "  Phase A: 0 missing syscalls after full harvest"
info "  Phase B: ${RESP_COUNT_B}/10 tool calls returned responses; no SIGSYS"
info "  Phase C (F-35): system.unsupported landing pad routes cleanly"

rm -f "$A_OUT" "$A_ERR" "$A_PID" "$B_OUT" "$B_ERR" "$B_PID"
exit 0
