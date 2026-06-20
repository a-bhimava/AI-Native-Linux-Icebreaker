#!/usr/bin/env bash
# test_rpa_bridge.sh — Static validation for RPA Bridge sandbox integrity.
#
# Usage:
#   bash test_rpa_bridge.sh [dual-brain-dir]
#
# All checks are macOS-safe (no systemd, no chroot).
# Exit code 0 = all checks pass. Non-zero = at least one failure.
set -euo pipefail

DUAL_BRAIN="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/dual-brain}"
cd "$DUAL_BRAIN"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

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

echo "=== RPA Bridge sandbox integrity checks ==="

# 1. Sandbox module exists.
check "rpa_bridge/sandbox.py exists" \
    test -f rpa_bridge/sandbox.py

# 2. Sandbox references Landlock (case-insensitive).
check "sandbox.py references Landlock" \
    grep -qi 'landlock' rpa_bridge/sandbox.py

# 3. RPA Bridge needs uinput access.
check "sandbox.py references /dev/uinput" \
    grep -q '/dev/uinput' rpa_bridge/sandbox.py

# 4. Timeout enforcement in bridge.py.
check "bridge.py has timeout enforcement" \
    grep -qE 'signal|SIGALRM|SIGKILL' rpa_bridge/bridge.py

# 5. Keyword allowlist in workflow_gen.py.
check "workflow_gen.py has keyword allowlist" \
    grep -qE 'ALLOWED_KEYWORDS|allowed_keywords' rpa_bridge/workflow_gen.py

# -- Summary ----------------------------------------------------------------
echo ""
echo "=== Results ==="
printf "Passed: ${GREEN}%d${NC}  Failed: ${RED}%d${NC}\n" "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
