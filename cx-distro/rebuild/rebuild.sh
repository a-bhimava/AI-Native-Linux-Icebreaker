#!/usr/bin/env bash
# rebuild.sh — Rebuild the Icebreaker ISO on the GCP VM.
#
# Usage:
#   bash rebuild.sh [--no-models] [--skip-to=N]
#
# This script:
#   1. Starts the GCP VM (if stopped)
#   2. Waits for SSH to become available
#   3. Pushes local changes to the remote repo
#   4. Pulls latest code on the VM
#   5. Rebuilds the Docker image (picks up Dockerfile.build changes)
#   6. Runs build.sh inside Docker (produces icebreaker.iso)
#
# The build runs detached via nohup so SSH drops don't kill it.
# Use monitor.sh to watch progress.
#
# Prerequisites:
#   - gcloud CLI authenticated
#   - Git remote configured and pushable
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── GCP VM config ──────────────────────────────────────────────────────
VM_NAME="${VM_NAME:-icebreaker-phase2-vm}"
VM_ZONE="${VM_ZONE:-us-west4-b}"
VM_PROJECT="${VM_PROJECT:-project-12486d7e-4046-45bd-8b4}"

REPO_DIR_ON_VM="~/icebreaker"
BUILD_LOG="/tmp/icebreaker-build.log"

# ── Parse args ─────────────────────────────────────────────────────────
BUILD_ARGS=""
for arg in "$@"; do
    case "$arg" in
        --no-models|--skip-to=*|--force)
            BUILD_ARGS="${BUILD_ARGS} ${arg}"
            ;;
        --help|-h)
            head -20 "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg (pass --no-models or --skip-to=N)" >&2
            exit 1
            ;;
    esac
done

SSH_CMD="gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"

echo "=== Icebreaker ISO Rebuild ==="
echo "VM:   ${VM_NAME} (${VM_ZONE})"
echo "Args: ${BUILD_ARGS:-<none>}"
echo ""

# ── Step 1: Start VM if stopped ────────────────────────────────────────
echo "[1/6] Checking VM status..."
VM_STATUS=$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${VM_ZONE}" --project="${VM_PROJECT}" \
    --format="get(status)" 2>/dev/null || echo "UNKNOWN")

if [ "$VM_STATUS" = "TERMINATED" ] || [ "$VM_STATUS" = "STOPPED" ]; then
    echo "  VM is ${VM_STATUS}, starting..."
    gcloud compute instances start "${VM_NAME}" \
        --zone="${VM_ZONE}" --project="${VM_PROJECT}"
    echo "  Waiting 30s for boot..."
    sleep 30
elif [ "$VM_STATUS" = "RUNNING" ]; then
    echo "  VM is already running."
else
    echo "  VM status: ${VM_STATUS}" >&2
    exit 1
fi

# ── Step 2: Wait for SSH ───────────────────────────────────────────────
echo "[2/6] Waiting for SSH..."
for i in $(seq 1 30); do
    if ${SSH_CMD} --command="echo ok" 2>/dev/null | grep -q ok; then
        echo "  SSH ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  SSH not available after 30 attempts." >&2
        exit 1
    fi
    sleep 5
done

# ── Step 3: Push local changes ─────────────────────────────────────────
echo "[3/6] Pushing local changes to remote..."
BRANCH=$(git branch --show-current)
git push origin "${BRANCH}" 2>&1 | tail -3
echo "  Pushed branch: ${BRANCH}"

# ── Step 4: Pull on VM ─────────────────────────────────────────────────
echo "[4/6] Pulling latest code on VM..."
${SSH_CMD} --command="
    cd ${REPO_DIR_ON_VM} && \
    git fetch origin && \
    git checkout ${BRANCH} 2>/dev/null || git checkout -b ${BRANCH} origin/${BRANCH} && \
    git pull origin ${BRANCH}
" 2>&1 | tail -5

# ── Step 5: Rebuild Docker image ───────────────────────────────────────
echo "[5/6] Rebuilding Docker build image (picks up Dockerfile.build changes)..."
${SSH_CMD} --command="
    cd ${REPO_DIR_ON_VM} && \
    sudo docker build -t icebreaker-build -f cx-distro/Dockerfile.build . 2>&1 | tail -5
" 2>&1

# ── Step 6: Run build (detached) ───────────────────────────────────────
echo "[6/6] Starting ISO build (detached via nohup)..."
${SSH_CMD} --command="
    cd ${REPO_DIR_ON_VM} && \
    nohup sudo docker run --privileged --rm \
        -v \$(pwd):/build \
        icebreaker-build ${BUILD_ARGS} \
        > ${BUILD_LOG} 2>&1 &
    echo \"Build started (PID: \$!)\"
    echo \"Log: ${BUILD_LOG}\"
    echo \"Monitor with: tail -f ${BUILD_LOG}\"
"

echo ""
echo "=== Build launched on ${VM_NAME} ==="
echo "Use './monitor.sh' to watch progress."
echo "Use './download.sh' when complete to fetch the ISO."
echo ""
echo "REMINDER: The VM bills while running. Stop it when done:"
echo "  gcloud compute instances stop ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
