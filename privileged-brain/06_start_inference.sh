#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Privileged Brain — Inference"
echo "========================================"
echo ""
echo "Choose inference mode:"
echo "  1) Ollama          — simple, port 11434 (no speculative decoding)"
echo "  2) llama.cpp server — speculative decoding + grammar, port 8080"
echo ""
MODE=${1:-""}

if [ -z "$MODE" ]; then
  read -rp "Enter 1 or 2: " MODE
fi

if [ "$MODE" = "1" ]; then
  # ============================
  # Option 1: Ollama
  # ============================
  echo ""
  echo "Starting Ollama server..."

  # Start ollama serve in background if not already running
  if ! pgrep -x "ollama" > /dev/null; then
    ollama serve &
    OLLAMA_PID=$!
    sleep 2
    echo "  Ollama started (PID $OLLAMA_PID)"
  else
    echo "  Ollama already running"
  fi

  echo ""
  echo "Testing fine-tuned model..."
  ollama run privileged-brain "list all listening TCP ports"

  echo ""
  echo "========================================"
  echo " Ollama is running"
  echo "========================================"
  echo ""
  echo " API endpoint : http://127.0.0.1:11434"
  echo " Model        : privileged-brain"
  echo ""
  echo " Test queries:"
  echo "   python3 scripts/test_inference.py --model privileged-brain"
  echo ""
  echo " Compare vs baseline:"
  echo "   python3 scripts/test_inference.py --model qwen2.5-coder:1.5b"

elif [ "$MODE" = "2" ]; then
  # ============================
  # Option 2: llama.cpp with speculative decoding
  # ============================
  echo ""

  MAIN_MODEL="conversion/models/privileged-brain-q4_k_m.gguf"
  DRAFT_MODEL="inference/draft_models/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"
  GRAMMAR="inference/grammar/mcp_tool_call.gbnf"

  if [ ! -f "$MAIN_MODEL" ]; then
    echo "ERROR: $MAIN_MODEL not found. Run bash 05_convert_and_import.sh first."
    exit 1
  fi

  # Download draft model if not present
  if [ ! -f "$DRAFT_MODEL" ]; then
    echo "Downloading Qwen 2.5 Coder 0.5B (draft model for speculative decoding)..."
    mkdir -p inference/draft_models
    huggingface-cli download \
      Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF \
      qwen2.5-coder-0.5b-instruct-q4_k_m.gguf \
      --local-dir inference/draft_models
    echo "  Draft model downloaded."
  fi

  echo "Starting llama.cpp server with speculative decoding..."
  echo ""
  echo "  Main model   : $MAIN_MODEL"
  echo "  Draft model  : $DRAFT_MODEL"
  echo "  Draft tokens : 8 per pass"
  echo "  GPU layers   : 99 (all on Metal)"
  echo "  Port         : 8080"
  echo ""
  echo "Press Ctrl+C to stop the server."
  echo ""

  llama-server \
    -m "$MAIN_MODEL" \
    --draft-model "$DRAFT_MODEL" \
    --draft 8 \
    -ngl 99 \
    --host 127.0.0.1 \
    --port 8080 \
    -c 4096 \
    --temp 0.1 \
    --log-disable

else
  echo "Invalid option: $MODE. Enter 1 or 2."
  exit 1
fi
