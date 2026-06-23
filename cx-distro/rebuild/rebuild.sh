#!/usr/bin/env bash
# rebuild.sh — Rebuild the Icebreaker ISO on the GCP VM.
#
# Usage:
#   bash rebuild.sh [--no-models] [--skip-to=N] [--force]
#
# This script:
#   1. Starts the GCP VM (if stopped)
#   2. Waits for SSH to become available
#   3. Deploys local repo to VM (tarball upload)
#   4. Links ~/models/*.gguf into repo tree (PB + QB weights)
#   5. Installs build dependencies (if missing)
#   6. Installs Rust toolchain (if missing)
#   7. Runs build.sh natively (produces icebreaker.iso)
#
# The build runs detached via nohup so SSH drops don't kill it.
# Use monitor.sh to watch progress.
#
# Prerequisites:
#   - gcloud CLI authenticated (colabuser23@gmail.com)
#   - Working directory is the repo root (or parent of cx-distro/)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

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
            echo "Unknown arg: $arg (pass --no-models, --skip-to=N, or --force)" >&2
            exit 1
            ;;
    esac
done

SSH_CMD="gcloud compute ssh ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
SCP_CMD="gcloud compute scp --zone=${VM_ZONE} --project=${VM_PROJECT}"

echo "=== Icebreaker ISO Rebuild ==="
echo "VM:   ${VM_NAME} (${VM_ZONE})"
echo "Args: ${BUILD_ARGS:-<none>}"
echo ""

# ── Step 1: Start VM if stopped ────────────────────────────────────────
echo "[1/7] Checking VM status..."
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
echo "[2/7] Waiting for SSH..."
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

# ── Step 3: Deploy repo via tarball ────────────────────────────────────
echo "[3/7] Deploying repo to VM via tarball..."
TARBALL="/tmp/icebreaker-repo.tar.gz"
echo "  Creating tarball from ${REPO_ROOT}..."
git -C "${REPO_ROOT}" archive --format=tar.gz HEAD > "${TARBALL}"
TARBALL_SIZE=$(du -h "${TARBALL}" | awk '{print $1}')
echo "  Tarball: ${TARBALL_SIZE}"

echo "  Uploading to VM..."
${SCP_CMD} "${TARBALL}" "${VM_NAME}:/tmp/icebreaker-repo.tar.gz" 2>/dev/null

echo "  Extracting on VM..."
${SSH_CMD} --command="
    mkdir -p ${REPO_DIR_ON_VM}
    cd ${REPO_DIR_ON_VM}
    tar xzf /tmp/icebreaker-repo.tar.gz
    rm /tmp/icebreaker-repo.tar.gz
    echo '  Files: '\$(find . -type f | wc -l)
" 2>/dev/null
rm -f "${TARBALL}"
echo "  Deploy complete."

# ── Step 4: Link model files ──────────────────────────────────────────
echo "[4/7] Linking model files into repo tree..."
${SSH_CMD} --command="
    mkdir -p ${REPO_DIR_ON_VM}/models
    for gguf in ~/models/*.gguf; do
        [ -f \"\$gguf\" ] || continue
        BASENAME=\$(basename \"\$gguf\")
        TARGET=\"${REPO_DIR_ON_VM}/models/\${BASENAME}\"
        if [ ! -f \"\$TARGET\" ]; then
            ln -sf \"\$gguf\" \"\$TARGET\"
            echo \"  Linked: \${BASENAME}\"
        else
            echo \"  Already present: \${BASENAME}\"
        fi
    done
    if [ ! -f ${REPO_DIR_ON_VM}/models/checksums.sha256 ]; then
        echo '  Generating checksums.sha256...'
        cd ${REPO_DIR_ON_VM}/models
        sha256sum *.gguf > checksums.sha256 2>/dev/null || true
    fi
    echo '  Models:'
    ls -lh ${REPO_DIR_ON_VM}/models/ 2>/dev/null
" 2>/dev/null

# ── Step 5: Install build dependencies ─────────────────────────────────
echo "[5/7] Ensuring build dependencies..."
${SSH_CMD} --command="
    if ! command -v debootstrap >/dev/null 2>&1; then
        echo '  Installing build deps via apt...'
        sudo apt-get update -qq
        sudo apt-get install -y -qq \
            debootstrap squashfs-tools xorriso \
            isolinux syslinux-common \
            grub-efi-amd64-bin mtools dosfstools \
            build-essential cmake git python3-venv \
            2>&1 | tail -3
    else
        echo '  Build deps already installed.'
    fi
" 2>/dev/null

# ── Step 6: Install Rust toolchain ─────────────────────────────────────
echo "[6/7] Ensuring Rust toolchain..."
${SSH_CMD} --command="
    if [ -f ~/.cargo/env ]; then
        source ~/.cargo/env
    fi
    if command -v rustup >/dev/null 2>&1; then
        echo '  Rust already installed:'
        rustc --version
    else
        echo '  Installing Rust via rustup...'
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
        source ~/.cargo/env
        rustc --version
    fi
    rustup default stable 2>/dev/null
" 2>/dev/null

# ── Step 7: Run build (detached) ───────────────────────────────────────
echo "[7/7] Starting ISO build (detached via nohup)..."
${SSH_CMD} --command="
    cd ${REPO_DIR_ON_VM}/cx-distro && \
    nohup sudo env \
        RUSTUP_HOME=/home/aditya/.rustup \
        CARGO_HOME=/home/aditya/.cargo \
        PATH=/home/aditya/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        bash build.sh ${BUILD_ARGS} \
        > ${BUILD_LOG} 2>&1 &
    echo \"Build started (PID: \$!)\"
    echo \"Log: ${BUILD_LOG}\"
    sleep 2
    echo '--- First lines ---'
    head -10 ${BUILD_LOG}
" 2>/dev/null

echo ""
echo "=== Build launched on ${VM_NAME} ==="
echo "Use './monitor.sh' to watch progress."
echo "Use './download.sh' when complete to fetch the ISO."
echo ""
echo "REMINDER: The VM bills while running. Stop it when done:"
echo "  gcloud compute instances stop ${VM_NAME} --zone=${VM_ZONE} --project=${VM_PROJECT}"
