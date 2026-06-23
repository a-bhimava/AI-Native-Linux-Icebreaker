#!/usr/bin/env bash
# download.sh — Download the built ISO from the GCP VM.
#
# Usage:
#   bash download.sh [--dest DIR]
#
# Downloads in 200 MB chunks (GCP SSH drops on large transfers).
# Reassembles locally and verifies SHA-256.
set -euo pipefail

# ── GCP VM config ──────────────────────────────────────────────────────
VM_NAME="${VM_NAME:-icebreaker-phase2-vm}"
VM_ZONE="${VM_ZONE:-us-west4-b}"
VM_PROJECT="${VM_PROJECT:-project-12486d7e-4046-45bd-8b4}"

REPO_DIR_ON_VM="~/icebreaker"
ISO_REMOTE="${REPO_DIR_ON_VM}/cx-distro/icebreaker.iso"
CHUNK_SIZE="200M"

# ── Parse args ─────────────────────────────────────────────────────────
DEST_DIR="."
for arg in "$@"; do
    case "$arg" in
        --dest=*) DEST_DIR="${arg#--dest=}" ;;
        --help|-h)
            echo "Usage: download.sh [--dest=DIR]"
            echo "  Downloads the built ISO from the GCP VM in chunks."
            echo "  Default destination: current directory."
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

mkdir -p "${DEST_DIR}"

SSH_CMD="gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
SCP_CMD="gcloud compute scp --zone=${VM_ZONE} --project=${VM_PROJECT}"

echo "=== Icebreaker ISO Download ==="
echo "VM:   ${VM_NAME}"
echo "Dest: ${DEST_DIR}"
echo ""

# ── Step 1: Check ISO exists ──────────────────────────────────────────
echo "[1/5] Checking ISO on VM..."
ISO_INFO=$(${SSH_CMD} --command="
    if [ -f ${ISO_REMOTE} ]; then
        ls -lh ${ISO_REMOTE} | awk '{print \$5}'
        sha256sum ${ISO_REMOTE} | awk '{print \$1}'
    else
        echo 'NOT_FOUND'
    fi
" 2>/dev/null)

if echo "$ISO_INFO" | grep -q "NOT_FOUND"; then
    echo "  ERROR: No ISO found at ${ISO_REMOTE} on VM." >&2
    echo "  Run rebuild.sh first." >&2
    exit 1
fi

ISO_SIZE=$(echo "$ISO_INFO" | head -1)
REMOTE_SHA=$(echo "$ISO_INFO" | tail -1)
echo "  ISO size: ${ISO_SIZE}"
echo "  SHA-256:  ${REMOTE_SHA}"

# ── Step 2: Split on VM ───────────────────────────────────────────────
echo "[2/5] Splitting ISO into ${CHUNK_SIZE} chunks on VM..."
${SSH_CMD} --command="
    cd /tmp && \
    rm -f iso_part_* && \
    split -b ${CHUNK_SIZE} ${ISO_REMOTE} iso_part_
    echo \"Chunks: \$(ls iso_part_* | wc -l)\"
    ls -lh iso_part_*
" 2>/dev/null

# ── Step 3: Download chunks ───────────────────────────────────────────
echo "[3/5] Downloading chunks..."
CHUNK_DIR="${DEST_DIR}/.iso_chunks"
mkdir -p "${CHUNK_DIR}"
rm -f "${CHUNK_DIR}"/iso_part_*

CHUNKS=$(${SSH_CMD} --command="ls /tmp/iso_part_*" 2>/dev/null)
TOTAL=$(echo "$CHUNKS" | wc -l | tr -d ' ')
COUNT=0

for chunk in $CHUNKS; do
    BASENAME=$(basename "$chunk")
    COUNT=$((COUNT + 1))
    echo "  [${COUNT}/${TOTAL}] Downloading ${BASENAME}..."
    ${SCP_CMD} "${VM_NAME}:${chunk}" "${CHUNK_DIR}/${BASENAME}" 2>/dev/null
done

# ── Step 4: Reassemble ────────────────────────────────────────────────
echo "[4/5] Reassembling ISO..."
ISO_LOCAL="${DEST_DIR}/icebreaker.iso"
cat "${CHUNK_DIR}"/iso_part_* > "${ISO_LOCAL}"
rm -rf "${CHUNK_DIR}"
echo "  Written: ${ISO_LOCAL} ($(du -h "${ISO_LOCAL}" | awk '{print $1}'))"

# ── Step 5: Verify ────────────────────────────────────────────────────
echo "[5/5] Verifying SHA-256..."
if command -v sha256sum >/dev/null 2>&1; then
    LOCAL_SHA=$(sha256sum "${ISO_LOCAL}" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    LOCAL_SHA=$(shasum -a 256 "${ISO_LOCAL}" | awk '{print $1}')
else
    echo "  WARNING: No sha256sum/shasum found. Skipping verification."
    LOCAL_SHA="SKIP"
fi

if [ "$LOCAL_SHA" = "SKIP" ]; then
    echo "  Verification skipped."
elif [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "  MATCH: ${LOCAL_SHA}"
    echo ""
    echo "=== Download complete ==="
    echo "ISO: ${ISO_LOCAL}"
else
    echo "  MISMATCH!" >&2
    echo "  Remote: ${REMOTE_SHA}" >&2
    echo "  Local:  ${LOCAL_SHA}" >&2
    echo "  The download may be corrupted. Try again." >&2
    exit 1
fi

# ── Cleanup on VM ──────────────────────────────────────────────────────
echo ""
echo "Cleaning up chunks on VM..."
${SSH_CMD} --command="rm -f /tmp/iso_part_*" 2>/dev/null
echo "Done."
echo ""
echo "REMINDER: Stop the VM when you're done to save cost:"
echo "  gcloud compute instances stop ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
