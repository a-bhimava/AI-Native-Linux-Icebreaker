#!/usr/bin/env bash
# Frosted Graphite packaging and safety regression checks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CX="${ROOT}/cx-distro"

(cd "${CX}/vendor" && sha256sum -c appearance-sources.sha256 --quiet)
grep -q 'whiskermenu' "${CX}/distro/xfce4-panel.xml"
grep -q 'autohide-behavior' "${CX}/distro/xfce4-panel.xml"
grep -q 'MacTahoe-Dark' "${CX}/distro/xsettings.xml"
grep -q 'background-image: none' "${ROOT}/dual-brain/gui/theme.py"
grep -q 'ib-security-surface' "${ROOT}/dual-brain/gui/hitl/dialog.py"
grep -q 'xfwm4.xml xfce4-keyboard-shortcuts.xml' "${CX}/build.sh"
grep -q 'rofi xfce4-whiskermenu-plugin dunst xfdashboard picom' "${CX}/build.sh"
grep -q 'appearance artifact checksum failure' "${CX}/build.sh"
grep -q 'plank-reloaded_0.11.172_arm64.deb' "${CX}/build.sh"
grep -q 'Never run it alongside XFWM' "${CX}/distro/icebreaker-appearance-apply"
grep -q 'command -v plank' "${CX}/distro/icebreaker-appearance-apply"
grep -q 'pkill -xu.*picom' "${CX}/distro/icebreaker-appearance-apply"
test -x "${ROOT}/incremental/tests/test_xfce_frosted_profile.sh"
if rg -n 'zquestz.github.io|add-apt-repository' "${CX}/build.sh" "${CX}/rebuild/deploy.sh" "${CX}/distro"; then
    echo "FAIL: mutable appearance PPA introduced" >&2
    exit 1
fi
echo "PASS: Frosted Graphite XFCE shell configuration is pinned and safety-gated."
