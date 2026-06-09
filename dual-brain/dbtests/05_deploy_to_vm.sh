#!/usr/bin/env bash
# 05_deploy_to_vm.sh — tar dual-brain, scp to VM, extract, run pytest.
#
# The canonical "I made changes, push them to the VM and confirm pytest
# is still green" cycle, condensed. Useful between milestones.
#
# Steps:
#   (a) tar dual-brain/ (excluding __pycache__, .venv, *.pyc, .pytest_cache)
#   (b) scp tarball to VM home
#   (c) ssh: remove old dual-brain, extract, pip install -q -r requirements.txt
#   (d) ssh: run pytest controller/tests/ -q
#
# Env overrides:
#   VM_NAME    default: instance-20260528-030421
#   VM_ZONE    default: us-central1-a
#   VM_VENV    default: ~/dual-brain-venv

set -uo pipefail

VM_NAME="${VM_NAME:-instance-20260528-030421}"
VM_ZONE="${VM_ZONE:-us-central1-a}"
VM_VENV="${VM_VENV:-\$HOME/dual-brain-venv}"

CYN=$'\033[36m'; GRN=$'\033[32m'; RED=$'\033[31m'; YLW=$'\033[33m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; RST=$'\033[0m'

if ! command -v gcloud >/dev/null 2>&1; then
  echo "${RED}FAIL${RST}  gcloud not on PATH" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
TARBALL="/tmp/dual-brain-$(date +%Y%m%d-%H%M%S).tar.gz"

# ── (a) tar ────────────────────────────────────────────────────────────────

echo "${BOLD}${CYN}(1/4)${RST} tar dual-brain/  →  ${TARBALL}"
cd "${REPO_ROOT}"
tar czf "${TARBALL}" \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  dual-brain/
SIZE=$(du -h "${TARBALL}" | awk '{print $1}')
echo "  ${GRN}✓${RST} ${SIZE} archive"

# ── (b) scp ────────────────────────────────────────────────────────────────

echo
echo "${BOLD}${CYN}(2/4)${RST} scp to ${VM_NAME}"
if gcloud compute scp "${TARBALL}" "${VM_NAME}:~/$(basename "${TARBALL}")" \
     --zone="${VM_ZONE}" --quiet 2>&1 | tail -5; then
  echo "  ${GRN}✓${RST} uploaded"
else
  echo "  ${RED}✗${RST} scp failed"
  exit 1
fi

# ── (c) extract + pip install ──────────────────────────────────────────────

echo
echo "${BOLD}${CYN}(3/4)${RST} extract + pip install -r requirements.txt"
if gcloud compute ssh "${VM_NAME}" --zone="${VM_ZONE}" --quiet --command="
  set -e
  cd \$HOME
  rm -rf dual-brain
  tar xzf $(basename "${TARBALL}")
  source ${VM_VENV}/bin/activate
  pip install -q -r dual-brain/requirements.txt
  echo INSTALL_OK
" 2>&1 | tail -8; then
  echo "  ${GRN}✓${RST} deps installed"
else
  echo "  ${RED}✗${RST} install step failed"
  exit 1
fi

# ── (d) pytest ─────────────────────────────────────────────────────────────

echo
echo "${BOLD}${CYN}(4/4)${RST} pytest controller/tests/"
echo
if gcloud compute ssh "${VM_NAME}" --zone="${VM_ZONE}" --quiet --command="
  source ${VM_VENV}/bin/activate
  cd \$HOME/dual-brain
  PYTHONPATH=. pytest controller/tests/ --timeout=180 -q
"; then
  echo
  echo "${GRN}${BOLD}PASS${RST}  pytest green on VM."
  rm -f "${TARBALL}"
  exit 0
else
  echo
  echo "${RED}${BOLD}FAIL${RST}  pytest failed on VM. Tarball preserved: ${TARBALL}"
  exit 1
fi
