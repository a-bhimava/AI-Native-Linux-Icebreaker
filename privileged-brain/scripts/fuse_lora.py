#!/usr/bin/env python3
"""
Merge LoRA adapters into the base model and save as a standard HuggingFace model.
The merged model is then converted to GGUF by fuse_and_export.sh.

Usage:
  python3 scripts/fuse_lora.py                          # uses DPO adapter (recommended)
  python3 scripts/fuse_lora.py --adapter sft            # uses SFT adapter only
"""

import argparse
import torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adapter",
        choices=["sft", "dpo"],
        default="dpo",
        help="Which adapter to fuse (dpo = after DPO training, sft = after SFT only)",
    )
    parser.add_argument("--output-dir", default="conversion/fused_model")
    args = parser.parse_args()

    model_id = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    adapter_path = f"training/adapters/{args.adapter}/final"

    if not Path(adapter_path).exists():
        print(f"ERROR: Adapter not found at {adapter_path}")
        print(f"       Run 04_train.sh first.")
        raise SystemExit(1)

    print(f"Base model  : {model_id}")
    print(f"Adapter     : {adapter_path}")
    print(f"Output      : {args.output_dir}")
    print()

    # Load on CPU for merging (avoids MPS precision issues during merge)
    print("Loading base model on CPU for merging...")
    base_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        device_map="cpu",
        trust_remote_code=True,
    )

    print("Loading LoRA adapter...")
    model = PeftModel.from_pretrained(base_model, adapter_path)

    print("Merging LoRA weights into base model (this takes ~1 minute)...")
    model = model.merge_and_unload()
    model.eval()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Saving merged model to {output_dir}...")
    model.save_pretrained(str(output_dir), safe_serialization=True)

    print("Saving tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    tokenizer.save_pretrained(str(output_dir))

    size_gb = sum(f.stat().st_size for f in output_dir.glob("**/*") if f.is_file()) / 1e9
    print(f"\nFused model saved. Size: {size_gb:.1f} GB")
    print(f"Next: run bash 05_convert_and_import.sh")


if __name__ == "__main__":
    main()
