#!/usr/bin/env bash
# Regression checks for the v1 installer-first account flow (F-118).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE="${ROOT}/incremental/build/profile-xfce-frosted.sh"
ONBOARDING="${ROOT}/cx-distro/distro/icebreaker-onboarding"
INSTALLER="${ROOT}/cx-distro/distro/icebreaker-installer-first"
AUTOSTART="${ROOT}/cx-distro/distro/icebreaker-installer-first.desktop"
MANIFEST="${ROOT}/incremental/versions/v6.manifest"

bash -n "$PROFILE" "$ONBOARDING" "$INSTALLER" "${ROOT}/cx-distro/distro/ice"
rg -q 'useradd .* -p "!" ubuntu' "$PROFILE"
rg -q 'exec ubiquity gtk_ui' "$INSTALLER"
rg -q '^Exec=/usr/libexec/icebreaker/icebreaker-installer-first$' "$AUTOSTART"
rg -q '/run/live/medium' "$ONBOARDING"
if rg -q '^OnlyShowIn=' "${ROOT}/cx-distro/distro/icebreaker-onboarding.desktop"; then
    echo "FAIL: account onboarding must run on both GNOME and XFCE" >&2
    exit 1
fi
rg -q 'icebreaker-installer-first.desktop' "$MANIFEST"
rg -q 'usr/local/bin/ice' "$MANIFEST"

echo "PASS: installer-first flow keeps the live account disposable and onboards only an installed user."
