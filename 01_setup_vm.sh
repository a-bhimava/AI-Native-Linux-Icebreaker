#!/usr/bin/env bash
# Run this ONCE immediately after SSH-ing into the VM.
# Sets up all dependencies and directory structure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo " Privileged Brain — VM Setup"
echo "========================================"

# ── Verify GPU is present ─────────────────────────────────────────────────────
if ! command -v nvidia-smi &>/dev/null; then
  echo "ERROR: nvidia-smi not found. Make sure you selected a GPU instance."
  exit 1
fi

echo ""
echo "GPU detected:"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
echo ""

# ── Find the right Python + pip ───────────────────────────────────────────────
# GCP Deep Learning VMs ship Python under /opt/conda — always prefer it.
# Fall back to system python3 + install pip via apt if conda is absent.
if [ -x /opt/conda/bin/pip ]; then
  PIP=/opt/conda/bin/pip
  PYTHON=/opt/conda/bin/python
  echo "Using conda Python: $($PYTHON --version 2>&1)"
elif [ -x /opt/conda/bin/pip3 ]; then
  PIP=/opt/conda/bin/pip3
  PYTHON=/opt/conda/bin/python3
  echo "Using conda Python3: $($PYTHON --version 2>&1)"
else
  echo "conda pip not found — installing pip via apt..."
  sudo apt-get install -y python3-pip 2>/dev/null
  PIP=pip3
  PYTHON=python3
  echo "Using system Python: $($PYTHON --version 2>&1)"
fi

# Check if PyTorch is already installed with CUDA
$PYTHON -c "import torch; print(f'PyTorch {torch.__version__}  |  CUDA available: {torch.cuda.is_available()}')" 2>/dev/null \
  && TORCH_OK=1 || TORCH_OK=0

# ── Install Python packages ───────────────────────────────────────────────────
echo ""
echo "Installing Python packages..."
$PIP install -q --upgrade pip

# ── Install PyTorch FIRST so nothing else pulls in a different CUDA variant ───
if [ "$TORCH_OK" = "0" ]; then
  echo "Installing PyTorch with CUDA 12.4 support..."
  # cu124 wheel is stable and driver-forward-compatible (works with driver 580/CUDA 13.0)
  # We skip torchvision/torchaudio — not needed for text-only LLM training
  $PIP install -q torch --index-url https://download.pytorch.org/whl/cu124
  # Remove torchvision if it was pulled in as a dep (causes CUDA version mismatch)
  $PIP uninstall -y torchvision torchaudio 2>/dev/null || true
else
  echo "PyTorch already installed — skipping."
fi

# ── Install the rest (won't touch torch since it's already installed) ─────────
$PIP install -q \
  transformers \
  trl \
  peft \
  datasets \
  accelerate \
  huggingface_hub

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "Verifying installation..."
$PYTHON -c "
import torch
from transformers import __version__ as tv
from trl import __version__ as trlv
from peft import __version__ as pv
print(f'  python      : $('"$PYTHON"' --version 2>&1)')
print(f'  torch       : {torch.__version__}')
print(f'  CUDA        : {torch.cuda.is_available()} ({torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"})')
print(f'  transformers: {tv}')
print(f'  trl         : {trlv}')
print(f'  peft        : {pv}')
"

# ── Write a small wrapper so training scripts always use the right Python ─────
# Creates ~/python_env that the training scripts will source.
PYTHON_BIN="$PYTHON"
cat > "$SCRIPT_DIR/python_env" <<EOF
# Sourced by training scripts to ensure the right Python/pip is used.
export PYTHON="$PYTHON_BIN"
export PIP="$PIP"
export PATH="$(dirname "$PYTHON_BIN"):$PATH"
EOF
echo "Python env written to: $SCRIPT_DIR/python_env"

# ── Directory structure ───────────────────────────────────────────────────────
echo ""
echo "Creating directory structure..."
mkdir -p "$SCRIPT_DIR/data"
mkdir -p "$SCRIPT_DIR/training/adapters/sft"
mkdir -p "$SCRIPT_DIR/training/adapters/dpo"
mkdir -p "$SCRIPT_DIR/training/logs"

echo ""
echo "========================================"
echo " Setup complete."
echo ""
echo " Next steps:"
echo "   1. Verify data:  wc -l ~/data/train.jsonl ~/data/valid.jsonl"
echo "   2. Start train:  bash 02_start_training.sh"
echo "========================================"
