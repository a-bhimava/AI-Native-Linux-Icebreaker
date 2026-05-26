#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Privileged Brain — FEH Evaluation"
echo "========================================"
echo ""
echo "This runs the Functional Equivalence Heuristic against:"
echo "  1. Baseline: qwen2.5-coder:1.5b (stock, no fine-tuning)"
echo "  2. Fine-tuned: privileged-brain"
echo ""
echo "Requires Ollama to be running (bash 06_start_inference.sh option 1)"
echo ""

# --- Check Ollama is up ---
if ! curl -sf http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
  echo "ERROR: Ollama is not running. Start it with:"
  echo "       bash 06_start_inference.sh   (choose option 1)"
  exit 1
fi

# --- Baseline evaluation ---
echo "[1/2] Evaluating baseline model (qwen2.5-coder:1.5b)..."
python3 scripts/eval_feh.py --model qwen2.5-coder:1.5b --port 11434
echo ""

# --- Fine-tuned model evaluation ---
if ollama list | grep -q "privileged-brain"; then
  echo "[2/2] Evaluating fine-tuned model (privileged-brain)..."
  python3 scripts/eval_feh.py --model privileged-brain --port 11434
else
  echo "[2/2] SKIP: privileged-brain not found in Ollama."
  echo "           Run bash 05_convert_and_import.sh first."
fi

echo ""
echo "========================================"
echo " Results saved to eval/results/"
echo "========================================"
echo ""
echo "To compare:"
echo "  cat eval/results/feh_*.json | python3 -c \\"
echo "    \"import json,sys; [print(d['model'],f\\\"{d['mean_feh']*100:.1f}%\\\") for d in [json.load(open(f)) for f in __import__('glob').glob('eval/results/feh_*.json')]]\""
