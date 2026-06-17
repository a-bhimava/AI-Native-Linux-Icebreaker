#!/usr/bin/env bash
# test_first_boot.sh — Static verification for first-boot and safe-mode scripts.
#
# Usage:
#   bash test_first_boot.sh
#
# All checks are macOS-safe (no systemd, no chroot).
# Exit code 0 = all checks pass. Non-zero = at least one failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CX_DIR}/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf "${GREEN}PASS${NC}: %s\n" "$desc"
        PASS=$((PASS + 1))
    else
        printf "${RED}FAIL${NC}: %s\n" "$desc"
        FAIL=$((FAIL + 1))
    fi
}

skip() {
    printf "${YELLOW}SKIP${NC}: %s\n" "$1"
    SKIP=$((SKIP + 1))
}

FIRST_BOOT="${CX_DIR}/distro/first-boot"
SAFE_MODE="${CX_DIR}/distro/safe-mode"
UNIT="${REPO_ROOT}/dual-brain/controller/systemd/icebreaker-first-boot.service"

echo "=== First-boot / Safe-mode static checks ==="

# Shell syntax.
check "first-boot syntax" bash -n "${FIRST_BOOT}"
check "safe-mode syntax" bash -n "${SAFE_MODE}"

# Systemd unit properties.
check "first-boot.service has ConditionPathExists" \
    grep -q 'ConditionPathExists=!/var/lib/icebreaker/.first-boot-complete' "${UNIT}"
check "first-boot.service has Type=oneshot" \
    grep -q 'Type=oneshot' "${UNIT}"
check "first-boot.service has Before=icebreaker-controller.service" \
    grep -q 'Before=icebreaker-controller.service' "${UNIT}"
check "first-boot.service has NoNewPrivileges=yes" \
    grep -q 'NoNewPrivileges=yes' "${UNIT}"

# Script content checks.
check "first-boot references sentinel path" \
    grep -q '/var/lib/icebreaker/.first-boot-complete' "${FIRST_BOOT}"
check "first-boot sources locations.env" \
    bash -c 'grep -q "source" "$1" && grep -q "locations.env" "$1"' _ "${FIRST_BOOT}"
check "safe-mode checks for TTY" \
    grep -q '\[ -t 0 \]' "${SAFE_MODE}"

# Shellcheck (skip if unavailable).
if command -v shellcheck >/dev/null 2>&1; then
    check "first-boot passes shellcheck" shellcheck -S warning "${FIRST_BOOT}"
    check "safe-mode passes shellcheck" shellcheck -S warning "${SAFE_MODE}"
else
    skip "first-boot shellcheck (shellcheck not installed)"
    skip "safe-mode shellcheck (shellcheck not installed)"
fi

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
printf "Passed: ${GREEN}%d${NC}  Failed: ${RED}%d${NC}  Skipped: ${YELLOW}%d${NC}\n" \
    "$PASS" "$FAIL" "$SKIP"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
