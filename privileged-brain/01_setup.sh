#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Privileged Brain — Environment Setup"
echo "========================================"

# --- 1. Ollama ---
if ! command -v ollama &>/dev/null; then
  echo "[1/5] Installing Ollama..."
  brew install ollama
else
  echo "[1/5] Ollama already installed: $(ollama --version 2>/dev/null || echo 'ok')"
fi

# --- 2. llama.cpp (for speculative decoding server + GGUF conversion) ---
if ! command -v llama-server &>/dev/null; then
  echo "[2/5] Installing llama.cpp..."
  brew install llama.cpp
else
  echo "[2/5] llama.cpp already installed"
fi

# --- 3. Python dependencies ---
echo "[3/5] Installing Python dependencies..."

# Ensure cargo/rust is on PATH if installed (needed transitively by some packages)
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

pip3 install --quiet --upgrade pip

pip3 install --quiet \
  torch torchvision torchaudio \
  transformers \
  datasets \
  "trl>=0.8" \
  peft \
  anthropic \
  huggingface_hub \
  accelerate \
  sentencepiece \
  protobuf \
  scipy \
  evaluate

# outlines requires Python <=3.12 for pre-built wheels and fails to compile on 3.14.
# Grammar-constrained decoding is handled natively via llama.cpp --grammar-file (GBNF) instead.
echo "  Note: outlines skipped (incompatible with Python 3.14) — using llama.cpp GBNF grammar"

# --- 4. Create directory structure ---
echo "[4/5] Creating directory structure..."
mkdir -p data/raw data/processed data/synthetic
mkdir -p training/adapters/sft/final training/adapters/dpo/final
mkdir -p conversion/models conversion/fused_model
mkdir -p inference/grammar inference/draft_models
mkdir -p eval/results

# --- 5. Pull base model + verify ---
echo "[5/5] Pulling Qwen 2.5 Coder 1.5B via Ollama (skips if already cached)..."
ollama pull qwen2.5-coder:1.5b

python3 - <<'PYEOF'
import torch
print(f"  PyTorch : {torch.__version__}")
print(f"  MPS     : {torch.backends.mps.is_available()}")
try:
    import transformers, datasets, trl, peft, anthropic
    print("  Packages: OK")
except ImportError as e:
    print(f"  MISSING : {e}")
PYEOF

echo ""
echo "Setup complete. Next: bash 02_get_data.sh"
