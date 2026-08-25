#!/usr/bin/env bash
# Static regression checks for the incremental XFCE Frosted profile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${ROOT}/incremental/build"
CX="${ROOT}/cx-distro"

(cd "${CX}/vendor" && sha256sum -c appearance-sources.sha256 --quiet)

for file in profile-xfce-frosted.sh packages-xfce-frosted.txt build-base.sh build-iso.sh; do
    test -f "${BUILD}/${file}"
done

for file in build-base.sh build-iso.sh; do
    grep -Fq 'umount -l "$mountpoint"' "${BUILD}/${file}"
    grep -Fq 'mountpoint -q "$mountpoint"' "${BUILD}/${file}"
done

rg -q 'desktop\|xfce-frosted' "${BUILD}/profile-xfce-frosted.sh"
rg -q 'base-xfce-frosted' "${BUILD}/profile-xfce-frosted.sh"
rg -q 'lightdm-gtk-greeter' "${BUILD}/packages-xfce-frosted.txt"
rg -q 'xfce4-whiskermenu-plugin' "${BUILD}/packages-xfce-frosted.txt"
rg -q '^picom$' "${BUILD}/packages-xfce-frosted.txt"
rg -q 'profile_apply_overlay' "${BUILD}/build-iso.sh"
rg -q 'apt-get install -y --no-install-recommends bamfdaemon libgnome-menu-3-0' "${BUILD}/profile-xfce-frosted.sh"
grep -Fq 'dpkg-query -W -f="${db:Status-Status}" plank-reloaded' "${BUILD}/profile-xfce-frosted.sh"
rg -q 'Plank Reloaded installed' "${ROOT}/incremental/build/smoke-gate.sh"
rg -q 'icebreaker-build-profile' "${BUILD}/build-iso.sh"
rg -q 'PROFILE="\$\{PROFILE:-desktop\}"' "${ROOT}/incremental/build/smoke-gate.sh"
rg -q 'systemctl is-active lightdm' "${ROOT}/incremental/tests/qemu-gate.sh"
rg -q 'EDITION="\$\{EDITION:-current\}"' "${ROOT}/incremental/tests/qemu-gate.sh"
rg -q 'V67_PROFILE="\$\{V67_PROFILE:-desktop\}"' "${CX}/rebuild/rebuild-v67.sh"

# Picom stays opt-in and must not compete with XFWM's compositor.
rg -q 'enabled compositor_enabled false' "${CX}/distro/icebreaker-appearance-apply"
rg -q 'Never run it alongside XFWM' "${CX}/distro/icebreaker-appearance-apply"

echo "PASS: incremental XFCE Frosted profile is cache-isolated and safety-gated."
