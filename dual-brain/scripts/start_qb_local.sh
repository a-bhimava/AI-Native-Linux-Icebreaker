#!/usr/bin/env bash
# Start the Quarantined Brain llama-server (local backend).
#
# Resolves model_id + draft_model_id + grammar_path from the active
# controller config; queries the model_registry for on-disk paths and
# sha256 verification; then launches llama-server with the resolved
# files. Registry-driven — zero hardcoded model names.
#
# Compatible shape: this script body is the seed of the future
# icebreaker-qbd.service unit's ExecStart line (Phase 6 distro
# packaging) — no interactive prompts, no cd to $HOME, all paths
# sourced from locations.env.
#
# Usage (dev):
#   source dual-brain/scripts/locations.env
#   bash dual-brain/scripts/start_qb_local.sh
#
# Usage (Phase 6):
#   systemctl --user start icebreaker-qb-local.service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=locations.env
source "${SCRIPT_DIR}/locations.env"

cd "${ICEBREAKER_PROJECT_ROOT}/dual-brain"

# Pull model_id + draft_model_id + grammar_path from the active config.
read -r MODEL_ID DRAFT_ID GRAMMAR_PATH <<EOF
$(PYTHONPATH=. "${ICEBREAKER_PYTHON}" -c "
import sys, pathlib
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
cfg = tomllib.loads(pathlib.Path('${ICEBREAKER_CONFIG}').read_text())
qb = cfg['qb']['local']
print(qb['model_id'], qb.get('draft_model_id', ''), qb['grammar_path'])
")
EOF

echo "[start_qb_local] model_id=${MODEL_ID}"
echo "[start_qb_local] draft_model_id=${DRAFT_ID:-<none>}"
echo "[start_qb_local] grammar=${GRAMMAR_PATH}"

# Registry resolves model_id -> on-disk file path, verifying sha256.
MODEL_FILE=$(
  PYTHONPATH=. "${ICEBREAKER_PYTHON}" -m controller.model_registry \
    resolve --id "${MODEL_ID}"
)
DRAFT_ARGS=()
if [ -n "${DRAFT_ID}" ]; then
  DRAFT_FILE=$(
    PYTHONPATH=. "${ICEBREAKER_PYTHON}" -m controller.model_registry \
      resolve --id "${DRAFT_ID}"
  )
  DRAFT_ARGS=(--draft-model "${DRAFT_FILE}" --draft 8)
  echo "[start_qb_local] speculative-decoding draft: ${DRAFT_FILE}"
fi

echo "[start_qb_local] launching llama-server on 127.0.0.1:${ICEBREAKER_PORT_QB}"
exec llama-server \
  --model "${MODEL_FILE}" \
  "${DRAFT_ARGS[@]}" \
  -ngl 99 \
  --port "${ICEBREAKER_PORT_QB}" \
  --host 127.0.0.1 \
  --grammar-file "${GRAMMAR_PATH}" \
  --no-warmup
