#!/usr/bin/env bash
# deploy_to_vm.sh — Pack dual-brain/, upload to a GCP VM, and run tests.
#
# Usage:
#   bash dual-brain/scripts/deploy_to_vm.sh [VM_NAME] [ZONE] [PROJECT]
#
# All parameters can also be set via environment variables:
#   VM_NAME    VM_ZONE    VM_PROJECT    REMOTE_DIR    VENV_PATH    RUN_TESTS
#
# Examples:
#   # Default (the Phase 2 dev VM):
#   bash dual-brain/scripts/deploy_to_vm.sh
#
#   # Different VM:
#   VM_NAME=my-new-vm VM_ZONE=us-east1-b bash dual-brain/scripts/deploy_to_vm.sh
#
#   # Upload only, skip tests:
#   RUN_TESTS=0 bash dual-brain/scripts/deploy_to_vm.sh
#
#   # Point at any instance in any project:
#   bash dual-brain/scripts/deploy_to_vm.sh my-vm europe-west1-b my-project-id

set -euo pipefail

# ── Deploy config ─────────────────────────────────────────────────────────────
# Source the gitignored deploy.env (if present) so VM_NAME / VM_ZONE / VM_PROJECT
# and the API keys come from one declarative file rather than being hardcoded.
# Template: scripts/deploy.env.example. The file also travels in the upload
# tarball below, so the keys reach the VM for ci.sh gates G9/G10.
_DEPLOY_ENV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy.env"
# shellcheck source=/dev/null
[ -f "$_DEPLOY_ENV" ] && source "$_DEPLOY_ENV"

# ── Config ────────────────────────────────────────────────────────────────────
VM_NAME="${1:-${VM_NAME:-instance-20260528-030421}}"
VM_ZONE="${2:-${VM_ZONE:-us-central1-a}}"
VM_PROJECT="${3:-${VM_PROJECT:-fiery-artifact-344713}}"
REMOTE_DIR="${REMOTE_DIR:-~/dual-brain}"
VENV_PATH="${VENV_PATH:-~/dual-brain-venv}"
RUN_TESTS="${RUN_TESTS:-1}"

# Derive local source root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUAL_BRAIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DUAL_BRAIN_DIR/.." && pwd)"
PACKAGE_NAME="$(basename "$DUAL_BRAIN_DIR")"   # "dual-brain"

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"
info()    { printf "${BOLD}==> ${RESET}%s\n" "$*"; }
success() { printf "${GREEN}✓${RESET}  %s\n" "$*"; }
warn()    { printf "${YELLOW}⚠${RESET}  %s\n" "$*"; }
die()     { printf "${RED}✗${RESET}  %s\n" "$*" >&2; exit 1; }

# ── SSH helper ────────────────────────────────────────────────────────────────
ssh_cmd() {
  gcloud compute ssh "$VM_NAME" \
    --zone="$VM_ZONE" \
    --project="$VM_PROJECT" \
    --command="$1" \
    -- -o LogLevel=ERROR 2>&1
}

scp_to_vm() {
  local src="$1" dest="$2"
  gcloud compute scp \
    --zone="$VM_ZONE" \
    --project="$VM_PROJECT" \
    --quiet \
    "$src" \
    "${VM_NAME}:${dest}" 2>&1
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}Icebreaker — deploy to VM${RESET}\n"
echo "  VM:      $VM_NAME  ($VM_ZONE / $VM_PROJECT)"
echo "  Source:  $DUAL_BRAIN_DIR"
echo "  Dest:    $REMOTE_DIR"
echo "  Tests:   $([ "$RUN_TESTS" = "1" ] && echo yes || echo no)"
echo ""

# ── Step 1: Pack ──────────────────────────────────────────────────────────────
info "Packing $PACKAGE_NAME/ …"
TMPFILE="$(mktemp /tmp/dual-brain-deploy-XXXXXX.tar.gz)"
trap 'rm -f "$TMPFILE"' EXIT

tar -czf "$TMPFILE" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.pytest_cache' \
  --exclude='*.gguf' \
  --exclude='backups' \
  -C "$REPO_ROOT" \
  "$PACKAGE_NAME/"

SIZE="$(du -sh "$TMPFILE" | cut -f1)"
success "Packed ${SIZE} → $TMPFILE"

# ── Step 2: Upload ────────────────────────────────────────────────────────────
info "Uploading to VM …"
scp_to_vm "$TMPFILE" "/tmp/dual-brain-deploy.tar.gz"
success "Upload complete"

# ── Step 3: Extract ───────────────────────────────────────────────────────────
info "Extracting on VM …"
ssh_cmd "
  set -e
  # Remove old tree, then extract fresh copy
  rm -rf ${REMOTE_DIR}
  tar -xzf /tmp/dual-brain-deploy.tar.gz -C ~ --warning=no-unknown-keyword 2>/dev/null
  rm -f /tmp/dual-brain-deploy.tar.gz
  echo 'extracted'
"
success "Extracted to ${REMOTE_DIR}"

# ── Step 4: Install / sync deps ───────────────────────────────────────────────
info "Syncing Python dependencies …"
ssh_cmd "
  set -e
  if [ ! -d ${VENV_PATH} ]; then
    python3 -m venv ${VENV_PATH}
    echo 'created venv'
  fi
  source ${VENV_PATH}/bin/activate
  pip install -q --upgrade pip
  pip install -q -r ${REMOTE_DIR}/requirements.txt
  echo 'deps ok'
"
success "Dependencies up to date"

# ── Step 5: Run tests (optional) ─────────────────────────────────────────────
if [[ "$RUN_TESTS" == "1" ]]; then
  echo ""
  info "Running test suite on VM …"
  echo ""

  # hypothesis + anthropic SDK are available on this VM, so run the full suite.
  # Only skip tests that require a live llama-server or mcpd binary.
  ssh_cmd "
    set -e
    source ${VENV_PATH}/bin/activate
    cd ${REMOTE_DIR}
    PYTHONPATH=. python3 -m pytest controller/tests/ \
      --ignore=controller/tests/test_mcpd_client_integration.py \
      --ignore=controller/tests/test_llama_local_backend.py \
      -q --tb=short 2>&1
  " && echo "" && success "All tests passed on Linux" || {
    echo ""
    die "Tests failed on VM — see output above"
  }
fi

echo ""
success "Deploy complete → ${VM_NAME}:${REMOTE_DIR}"
echo ""
