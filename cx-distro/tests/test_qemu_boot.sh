#!/usr/bin/env bash
# test_qemu_boot.sh — Manual QEMU boot smoke-test checklist.
#
# Usage:
#   bash test_qemu_boot.sh                 # print checklist only
#   bash test_qemu_boot.sh path/to/iso     # print + offer to launch QEMU
#
# This is NOT an automated test. ISO boot testing requires serial console +
# expect scripts + VNC — out of scope for Phase 6. Phase 7 exit criteria
# require bare-metal testing on 3 hardware configs (P6-R4).
set -euo pipefail

BOLD='\033[1m'
NC='\033[0m'

ISO_PATH="${1:-}"

printf "\n${BOLD}═══ Icebreaker QEMU Boot Smoke Test ═══${NC}\n\n"

if [ -n "$ISO_PATH" ]; then
    if [ ! -f "$ISO_PATH" ]; then
        echo "ERROR: ISO file not found: ${ISO_PATH}" >&2
        exit 1
    fi
    printf "ISO: %s (%s)\n\n" "$ISO_PATH" "$(du -h "$ISO_PATH" | awk '{print $1}')"
fi

cat <<'CHECKLIST'
Checklist (verify manually after boot):

 1. [ ] System boots to login prompt within 60 seconds
 2. [ ] Login as default user (icebreaker/icebreaker)
 3. [ ] icebreaker --help prints usage
 4. [ ] systemctl is-active icebreaker-pbd → active
 5. [ ] systemctl is-active icebreaker-qbd → active
 6. [ ] systemctl is-active icebreaker-controller → active
 7. [ ] journalctl -u icebreaker-first-boot shows completion
 8. [ ] id | grep icebreaker-users
 9. [ ] icebreaker "show disk usage" returns valid response
10. [ ] journalctl --priority=err | grep icebreaker → no errors

CHECKLIST

if [ -n "$ISO_PATH" ] && [ -t 0 ]; then
    QEMU_CMD="qemu-system-x86_64 -m 8G -boot d -cdrom \"${ISO_PATH}\" -enable-kvm -device virtio-net-pci,netdev=net0 -netdev user,id=net0 -display gtk"

    printf "${BOLD}QEMU command:${NC}\n"
    echo "  ${QEMU_CMD}"
    echo ""

    printf "Launch QEMU now? [y/N] "
    read -r answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        echo "Launching..."
        qemu-system-x86_64 \
            -m 8G \
            -boot d \
            -cdrom "$ISO_PATH" \
            -enable-kvm \
            -device virtio-net-pci,netdev=net0 \
            -netdev user,id=net0 \
            -display gtk
    fi
elif [ -z "$ISO_PATH" ]; then
    echo "Tip: pass an ISO path to offer a QEMU launch:"
    echo "  bash test_qemu_boot.sh path/to/live-image-amd64.iso"
fi
