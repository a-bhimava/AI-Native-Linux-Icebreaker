#!/usr/bin/env bash
# Start the Privileged Brain llama-server.
#
# Mirrors start_qb_local.sh; reads from [pb] section of the active
# controller config. PB is always local — this script has no
# backend-switching logic.
#
# PB constraints (whitepaper INV-1):
#   * NO mcpd attachment in this process (llama-server is pure inference)
#   * Uses mcp_tool_call.gbnf (Phase 1 grammar, kept untouched)
#   * Loopback HTTP only
#
# Compatible shape: seeds the future icebreaker-pbd.service ExecStart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=locations.env
source "${SCRIPT_DIR}/locations.env"

cd "${ICEBREAKER_PROJECT_ROOT}/dual-brain"

# [pb] section is OPTIONAL in M2.5 — we fall back to repo defaults if
# the user hasn't extended their config. Read from config if present,
# else use the in-repo defaults.
PB_INFO=$(PYTHONPATH=. "${ICEBREAKER_PYTHON}" -c "
import sys, pathlib
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
cfg = tomllib.loads(pathlib.Path('${ICEBREAKER_CONFIG}').read_text())
pb = cfg.get('pb', {})
model_id = pb.get('model_id', 'qwen-2.5-coder-1.5b-instruct-q4_k_m')
draft_id = pb.get('draft_model_id', 'qwen-2.5-coder-0.5b-instruct-q4_k_m')
grammar = pb.get('grammar_path', 'privileged-brain/inference/grammar/mcp_tool_call.gbnf')
print(model_id, draft_id, grammar)
")
read -r PB_MODEL_ID PB_DRAFT_ID PB_GRAMMAR <<EOF
${PB_INFO}
EOF

echo "[start_pb] model_id=${PB_MODEL_ID}"
echo "[start_pb] draft_model_id=${PB_DRAFT_ID}"
echo "[start_pb] grammar=${PB_GRAMMAR}"

MODEL_FILE=$(
  PYTHONPATH=. "${ICEBREAKER_PYTHON}" -m controller.model_registry \
    resolve --id "${PB_MODEL_ID}"
)
DRAFT_ARGS=()
if [ -n "${PB_DRAFT_ID}" ]; then
  DRAFT_FILE=$(
    PYTHONPATH=. "${ICEBREAKER_PYTHON}" -m controller.model_registry \
      resolve --id "${PB_DRAFT_ID}"
  )
  DRAFT_ARGS=(--draft-model "${DRAFT_FILE}" --draft 8)
  echo "[start_pb] speculative-decoding draft: ${DRAFT_FILE}"
fi

# Grammar path is RELATIVE to project root, not dual-brain — resolve.
ABS_GRAMMAR="${ICEBREAKER_PROJECT_ROOT}/${PB_GRAMMAR}"
if [ ! -f "${ABS_GRAMMAR}" ]; then
  # M7.0.2b (v6.16, 2026-08-06): degrade-not-crash preserved per user's
  # middle-ground design (belt-and-braces: smoke-gate refuses ISO builds
  # without the file; runtime warns but keeps PB alive for operator
  # recovery). WARNING escalated to a 3-line ERROR block so a boot-time
  # regression is impossible to miss in `journalctl -u icebreaker-pbd`.
  echo "[start_pb] ERROR: grammar file not found at ${ABS_GRAMMAR}" >&2
  echo "[start_pb] ERROR: PB will run WITHOUT grammar constraint —" >&2
  echo "[start_pb] ERROR:   whitepaper §6 non-compliant, audit row C-4 open." >&2
  echo "[start_pb] ERROR:   Restore the file OR set [run] pb_grammar_path to a real path." >&2
  ABS_GRAMMAR=""
fi

GRAMMAR_ARGS=()
if [ -n "${ABS_GRAMMAR}" ]; then
  GRAMMAR_ARGS=(--grammar-file "${ABS_GRAMMAR}")
fi

echo "[start_pb] launching llama-server on 127.0.0.1:${ICEBREAKER_PORT_PB}"
exec llama-server \
  --model "${MODEL_FILE}" \
  "${DRAFT_ARGS[@]}" \
  "${GRAMMAR_ARGS[@]}" \
  -ngl 99 \
  --port "${ICEBREAKER_PORT_PB}" \
  --host 127.0.0.1 \
  --no-warmup
