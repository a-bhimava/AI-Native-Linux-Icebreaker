#!/usr/bin/env bash
# test_installed_binaries.sh — v6.10 P5 / F-72 anti-hide regression guard.
#
# Every diagnostic + corpus helper script that ships in dual-brain/scripts/
# and is meant to be run in-guest MUST have an `install -Dm755` line in
# build.sh — otherwise it's absent from the ISO and users have to scp it
# from the host every time they debug a broken pipeline (which is exactly
# what happened for the entire v6.8 sprint).
#
# This test greps build.sh for the required install lines. Missing entry
# = ISO shipped without the tool.
#
# Exit code 0 = all checks pass. Non-zero = at least one script hidden.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_SH="${CX_DIR}/build.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local desc="$1"
    local script_name="$2"
    if grep -q "scripts/${script_name}" "${BUILD_SH}"; then
        printf "${GREEN}PASS${NC}: %s\n" "$desc"
        PASS=$((PASS + 1))
    else
        printf "${RED}FAIL${NC}: %s (build.sh missing install for %s)\n" \
            "$desc" "$script_name"
        FAIL=$((FAIL + 1))
    fi
}

if [[ ! -f "${BUILD_SH}" ]]; then
    printf "${RED}FAIL${NC}: build.sh not found at %s\n" "${BUILD_SH}"
    exit 1
fi

# F-72 regression guards. Every debug/corpus helper script that must
# be on the ISO. Adding a new script to dual-brain/scripts/ that is
# meant for in-guest use gets a new row here.
check "ib_debug.py    installed (baseline)"       "ib_debug.py"
check "ib_bundle.py   installed (baseline)"       "ib_bundle.py"
check "ib_debug_v68.py installed (F-72)"          "ib_debug_v68.py"
check "ib_run_v68.py   installed (F-72)"          "ib_run_v68.py"
check "ib_run_corpus.py installed (F-72)"         "ib_run_corpus.py"

echo
if [[ ${FAIL} -eq 0 ]]; then
    printf "${GREEN}All %d installed-binary checks passed.${NC}\n" "${PASS}"
    exit 0
else
    printf "${RED}%d of %d installed-binary checks failed.${NC}\n" \
        "${FAIL}" "$((PASS + FAIL))"
    exit 1
fi
