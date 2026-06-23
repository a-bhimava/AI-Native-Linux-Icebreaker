#!/usr/bin/env bash
# monitor.sh — Watch the ISO build progress on the GCP VM.
#
# Usage:
#   bash monitor.sh              # tail the build log (streaming)
#   bash monitor.sh --status     # one-shot: check if build is running + last 20 lines
#   bash monitor.sh --full       # dump the entire build log
set -euo pipefail

# ── GCP VM config ──────────────────────────────────────────────────────
VM_NAME="${VM_NAME:-icebreaker-phase2-vm}"
VM_ZONE="${VM_ZONE:-us-west4-b}"
VM_PROJECT="${VM_PROJECT:-project-12486d7e-4046-45bd-8b4}"

BUILD_LOG="/tmp/icebreaker-build.log"

SSH_CMD="gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"

MODE="tail"
case "${1:-}" in
    --status) MODE="status" ;;
    --full)   MODE="full" ;;
    --help|-h)
        echo "Usage: monitor.sh [--status|--full]"
        echo "  (default)   Stream build log via tail -f"
        echo "  --status    One-shot: check if build is running + last 20 lines"
        echo "  --full      Dump the entire build log"
        exit 0
        ;;
esac

if [ "$MODE" = "status" ]; then
    echo "=== Build Status ==="
    ${SSH_CMD} --command="
        echo '--- Docker containers ---'
        sudo docker ps --filter ancestor=icebreaker-build --format 'ID={{.ID}} Status={{.Status}} Created={{.CreatedAt}}' 2>/dev/null || echo '(none running)'
        echo ''
        echo '--- Last 20 lines of build log ---'
        if [ -f ${BUILD_LOG} ]; then
            tail -20 ${BUILD_LOG}
            echo ''
            echo '--- Log size ---'
            du -h ${BUILD_LOG}
        else
            echo '(no build log found)'
        fi
        echo ''
        echo '--- ISO file ---'
        if [ -f ~/icebreaker/cx-distro/icebreaker.iso ]; then
            ls -lh ~/icebreaker/cx-distro/icebreaker.iso
            echo 'BUILD COMPLETE'
        else
            echo '(no ISO yet)'
        fi
    " 2>/dev/null
elif [ "$MODE" = "full" ]; then
    echo "=== Full Build Log ==="
    ${SSH_CMD} --command="cat ${BUILD_LOG} 2>/dev/null || echo '(no build log found)'" 2>/dev/null
else
    echo "=== Streaming Build Log (Ctrl+C to stop) ==="
    echo "Connecting to ${VM_NAME}..."
    ${SSH_CMD} --command="tail -f ${BUILD_LOG} 2>/dev/null || echo '(no build log found — build may not have started)'" 2>/dev/null
fi
