#!/usr/bin/env bash
# test_gui_agent.sh — Static validation for GUI Agent sandbox integrity.
#
# Usage:
#   bash test_gui_agent.sh [dual-brain-dir]
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

echo "=== GUI Agent sandbox integrity checks ==="

# 1. Sandbox module exists.
check "gui_agent/sandbox.py exists" \
    test -f gui_agent/sandbox.py

# 2. Sandbox references Landlock.
check "sandbox.py references Landlock" \
    grep -qE 'landlock_create_ruleset|Landlock' gui_agent/sandbox.py

# 3. No raw X11 imports (excluding tests and __pycache__).
check "no raw X11 imports in gui_agent/" \
    bash -c '! grep -rn "import Xlib\|from Xlib\|xdotool" gui_agent/ --include="*.py" | grep -v __pycache__ | grep -v "/tests/" | grep -q .'

# 4. No /dev/uinput access (excluding tests and __pycache__).
check "no /dev/uinput access in gui_agent/" \
    bash -c '! grep -rn "/dev/uinput" gui_agent/ --include="*.py" | grep -v __pycache__ | grep -v "/tests/" | grep -q .'

# 5. Protocol module exists.
check "gui_agent/protocol.py exists" \
    test -f gui_agent/protocol.py

# 6. App API registry exists.
check "gui_agent/app_apis/registry.py exists" \
    test -f gui_agent/app_apis/registry.py

# -- Summary ----------------------------------------------------------------
echo ""
echo "=== Results ==="
printf "Passed: ${GREEN}%d${NC}  Failed: ${RED}%d${NC}\n" "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
