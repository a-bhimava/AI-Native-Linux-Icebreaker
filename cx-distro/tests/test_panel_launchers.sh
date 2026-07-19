#!/usr/bin/env bash
# test_panel_launchers.sh — v6.10 P4 / F-70 anti-hide regression guard.
#
# The XFCE panel XML is the source of truth for what icons appear on
# the desktop panel. Every icebreaker-*.desktop file we ship MUST be
# reachable from either the panel or the applications menu — otherwise
# it's built code the user cannot launch (the F-70 pattern).
#
# Prior to v6.10 P4:
#   - icebreaker-chatbot.desktop     → on panel  ✓
#   - icebreaker-settings.desktop    → on panel  ✓
#   - icebreaker-control.desktop     → SHIPPED, NOT ON PANEL  ✗
#   - icebreaker-audit.desktop       → SHIPPED, NOT ON PANEL  ✗
#
# Two whole GTK apps sat unreachable from the panel for weeks.
# This test fails loudly if a future panel refresh drops them again.
#
# Exit code 0 = all checks pass. Non-zero = at least one launcher hidden.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PANEL_XML="${CX_DIR}/distro/xfce4-panel.xml"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local desc="$1"
    local desktop_file="$2"
    if grep -q "value=\"${desktop_file}\"" "${PANEL_XML}"; then
        printf "${GREEN}PASS${NC}: %s\n" "$desc"
        PASS=$((PASS + 1))
    else
        printf "${RED}FAIL${NC}: %s (missing %s in xfce4-panel.xml)\n" \
            "$desc" "$desktop_file"
        FAIL=$((FAIL + 1))
    fi
}

if [[ ! -f "${PANEL_XML}" ]]; then
    printf "${RED}FAIL${NC}: panel XML not found at %s\n" "${PANEL_XML}"
    exit 1
fi

# F-70 regression guards. Add a new row here for every new
# icebreaker-*.desktop you ship AND want on the panel.
check "Chatbot launcher pinned to panel"       "icebreaker-chatbot.desktop"
check "Settings launcher pinned to panel"      "icebreaker-settings.desktop"
check "Control Center launcher pinned (F-70)"  "icebreaker-control.desktop"
check "Audit Viewer launcher pinned (F-70)"    "icebreaker-audit.desktop"

echo
if [[ ${FAIL} -eq 0 ]]; then
    printf "${GREEN}All %d panel launcher checks passed.${NC}\n" "${PASS}"
    exit 0
else
    printf "${RED}%d of %d panel launcher checks failed.${NC}\n" \
        "${FAIL}" "$((PASS + FAIL))"
    exit 1
fi
