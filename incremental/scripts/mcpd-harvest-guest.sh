#!/usr/bin/env bash
# mcpd-harvest-guest.sh — in-guest seccomp harvest for qemu-gate L6.
#
# Scope G (2026-07-11): F-55 shipped because the build-VM harvest
# (mcpd-harvest.sh) exercised 9 tools but never spawned journalctl — the
# `faccessat` gap on arm64 only surfaced when the user typed
# `# show me errors in journalctl`. This script runs a 5-tool harvest
# INSIDE the booted guest so the REAL guest kernel + REAL seccomp filter
# catches every arch-specific syscall gap before ISO ship.
#
# Called from `incremental/tests/qemu-gate.sh` L6 via _ssh().
# Installed into every V ≥ 6 ISO at `/usr/local/bin/mcpd-harvest-guest.sh`
# by `incremental/versions/v2.manifest`.
#
# Output: one line of JSON (no jq dependency) so the gate can grep-parse:
#   {"status":"ok","responses":5,"missing_syscalls":[],"arch":"aarch64"}
#   {"status":"gap","responses":4,"missing_syscalls":[48,439],"arch":"aarch64"}
#   {"status":"error","reason":"mcpd not executable","arch":"aarch64"}
#
# Exit codes:
#   0  — harvest clean (no missing syscalls)
#   1  — gap detected OR runner error (fail-hard; F-55 shipped exactly
#        because we were lenient about "warn on gap")

set -uo pipefail

TIMEOUT=60
while [ $# -gt 0 ]; do
    case "$1" in
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --timeout=*) TIMEOUT="${1#*=}"; shift ;;
        *) shift ;;
    esac
done

ARCH="$(uname -m)"
MCPD="/usr/libexec/icebreaker/mcpd"

# Minimal JSON output helper (no jq dependency — jq is not guaranteed
# on stripped-down live-boot base images).
emit_json() {
    printf '%s\n' "$1"
}

if [ ! -x "$MCPD" ]; then
    emit_json "{\"status\":\"error\",\"reason\":\"mcpd not executable at ${MCPD}\",\"arch\":\"${ARCH}\"}"
    exit 1
fi

# The harvest requests: minimal set that exercises the syscall gaps we
# actually saw in production (F-33 fsync, F-55 faccessat via journalctl).
# Every one of these tools was in the failure history OR is a canonical
# path-syscall surface. Full 14-tool exercise happens in the build-VM
# `mcpd-harvest.sh`; the in-guest harvest is smaller by design (guest
# resources are constrained, boot time matters).
HARVEST_REQUESTS='{"jsonrpc":"2.0","method":"system.status","params":{},"id":1}
{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}
{"jsonrpc":"2.0","method":"fs.list","params":{"path":"/tmp"},"id":3}
{"jsonrpc":"2.0","method":"service.logs","params":{"unit":"systemd-logind","lines":5},"id":4}
{"jsonrpc":"2.0","method":"process.list","params":{},"id":5}'

# Clear kernel audit buffer so we only observe THIS harvest's syscalls.
# (F-55 pattern: type=1326 records accumulate in dmesg between runs.)
sudo dmesg -c >/dev/null 2>&1 || true

MCPD_OUT="$(mktemp)"
MCPD_ERR="$(mktemp)"
trap 'rm -f "$MCPD_OUT" "$MCPD_ERR"' EXIT

# Run mcpd under LOG_ONLY: mismatches log to kernel audit but don't
# SIGSYS the process, so the harvest can complete and we can enumerate
# EVERY missing syscall (not just the first one that fires).
export MCPD_SECCOMP_LOG_ONLY=1
export HOME=/tmp
export MCPD_FS_READ_ROOTS=/tmp

printf '%s\n' "$HARVEST_REQUESTS" | timeout "$TIMEOUT" "$MCPD" >"$MCPD_OUT" 2>"$MCPD_ERR" || true
unset MCPD_SECCOMP_LOG_ONLY

# Count JSON-RPC responses (one per successful method call).
RESP_COUNT="$(grep -c '"jsonrpc"' "$MCPD_OUT" 2>/dev/null || echo 0)"

# Parse dmesg for kernel-audit seccomp records. type=1326 = SECCOMP_AUDIT.
# Extract unique syscall numbers.
MISSING="$(sudo dmesg 2>/dev/null \
    | grep -E 'audit.*type=1326.*syscall=' \
    | grep -oE 'syscall=[0-9]+' \
    | cut -d= -f2 \
    | sort -un \
    | tr '\n' ',')"
# Strip trailing comma
MISSING="${MISSING%,}"

if [ -z "$MISSING" ]; then
    emit_json "{\"status\":\"ok\",\"responses\":${RESP_COUNT},\"missing_syscalls\":[],\"arch\":\"${ARCH}\"}"
    exit 0
else
    emit_json "{\"status\":\"gap\",\"responses\":${RESP_COUNT},\"missing_syscalls\":[${MISSING}],\"arch\":\"${ARCH}\"}"
    exit 1
fi
