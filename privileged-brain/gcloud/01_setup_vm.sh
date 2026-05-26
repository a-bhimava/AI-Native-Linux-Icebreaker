#!/usr/bin/env bash
# Run this ONCE immediately after SSH-ing into the VM.
# Sets up all dependencies and directory structure.
set -euo pipefail

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

# ── Check CUDA ────────────────────────────────────────────────────────────────
python3 -c "import torch; print(f'PyTorch {torch.__version__}  |  CUDA available: {torch.cuda.is_available()}')" 2>/dev/null || {
  echo "PyTorch not found — will be installed via pip."
}

# ── Install Python packages ───────────────────────────────────────────────────
echo "Installing Python packages..."
pip install -q --upgrade pip
pip install -q \
  transformers \
  trl \
  peft \
  datasets \
  accelerate \
  huggingface_hub \
  torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

echo ""
echo "Verifying installation..."
python3 -c "
import torch
from transformers import __version__ as tv
from trl import __version__ as trlv
from peft import __version__ as pv
print(f'  torch       : {torch.__version__}')
print(f'  CUDA        : {torch.cuda.is_available()} ({torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"})')
print(f'  transformers: {tv}')
print(f'  trl         : {trlv}')
print(f'  peft        : {pv}')
"

# ── Directory structure ───────────────────────────────────────────────────────
echo ""
echo "Creating directory structure..."
mkdir -p data
mkdir -p training/adapters/sft
mkdir -p training/adapters/dpo
mkdir -p training/logs

echo ""
echo "========================================"
echo " Setup complete."
echo ""
echo " Next steps:"
echo "   1. Upload data (run mac_commands.sh section 2 on your Mac)"
echo "   2. Verify data:  wc -l data/train.jsonl data/valid.jsonl"
echo "   3. Start train:  bash 02_start_training.sh"
echo "========================================"
