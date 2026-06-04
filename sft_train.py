#!/usr/bin/env python3
"""
SFT fine-tuning — Google Cloud VM (T4 / V100 / A100).
Auto-detects GPU, sets fp16/bf16 and batch size accordingly.
Auto-resumes from latest checkpoint if training was interrupted.

Usage:
  python3 sft_train.py
  python3 sft_train.py --epochs 5 --lr 1e-4
"""

import argparse
import glob
import logging
import os
import sys
import time

import torch
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── Logging: both stdout and file ─────────────────────────────────────────────
os.makedirs(BASE_DIR / "training/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "training/logs/sft_train.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)


def detect_gpu():
    """Return (fp16, bf16, batch_size, grad_accum) based on detected GPU."""
    if not torch.cuda.is_available():
        log.warning("No CUDA GPU found — falling back to CPU (very slow).")
        return False, False, 1, 16

    name = torch.cuda.get_device_name(0)
    mem_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    log.info(f"GPU : {name}")
    log.info(f"VRAM: {mem_gb:.1f} GB")

    if "A100" in name and mem_gb > 60:
        log.info("→ A100 80GB: bf16, batch=16, grad_accum=1  (eff. batch=16)")
        return False, True, 16, 1
    elif "A100" in name:
        log.info("→ A100 40GB: bf16, batch=8, grad_accum=2   (eff. batch=16)")
        return False, True, 8, 2
    elif "V100" in name:
        log.info("→ V100: fp16, batch=4, grad_accum=4         (eff. batch=16)")
        return True, False, 4, 4
    elif "L4" in name:
        log.info("→ L4 (24GB): bf16, batch=8, grad_accum=2   (eff. batch=16)")
        return False, True, 8, 2
    elif "P100" in name:
        log.info("→ P100 (16GB): fp16, batch=4, grad_accum=4  (eff. batch=16)")
        return True, False, 4, 4
    elif "K80" in name:
        log.info("→ K80 (12GB): fp16, batch=2, grad_accum=8   (eff. batch=16, slow)")
        return True, False, 2, 8
    elif "P4" in name:
        log.info("→ P4 (8GB): fp16, batch=1, grad_accum=16    (eff. batch=16, tight VRAM)")
        return True, False, 1, 16
    else:
        # T4 (16GB) or unknown — conservative settings
        log.info(f"→ T4 / unknown: fp16, batch=4, grad_accum=4 (eff. batch=16)")
        return True, False, 4, 4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id",    default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    parser.add_argument("--train-file",  default=str(BASE_DIR / "data/train.jsonl"))
    parser.add_argument("--valid-file",  default=str(BASE_DIR / "data/valid.jsonl"))
    parser.add_argument("--output-dir",  default=str(BASE_DIR / "training/adapters/sft"))
    parser.add_argument("--epochs",      type=int,   default=3)
    parser.add_argument("--lr",          type=float, default=2e-4)
    parser.add_argument("--max-seq-len", type=int,   default=512)
    parser.add_argument("--lora-rank",   type=int,   default=8)
    parser.add_argument("--save-steps",  type=int,   default=200)
    parser.add_argument("--batch-size",  type=int,   default=None,
                        help="Override auto-detected batch size")
    parser.add_argument("--grad-accum",  type=int,   default=None,
                        help="Override auto-detected gradient accumulation steps")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    log.info("=" * 60)
    log.info("  Privileged Brain — SFT Training (GCP GPU)")
    log.info("=" * 60)

    # ── GPU config ────────────────────────────────────────────────────────────
    fp16, bf16, auto_batch, auto_accum = detect_gpu()
    batch_size = args.batch_size or auto_batch
    grad_accum = args.grad_accum or auto_accum
    log.info(f"Effective batch size: {batch_size * grad_accum}")

    # ── Data validation ───────────────────────────────────────────────────────
    for path in [args.train_file, args.valid_file]:
        if not os.path.exists(path):
            log.error(f"Data file not found: {path}")
            log.error("Run:  cp /path/to/train.jsonl data/train.jsonl")
            sys.exit(1)

    train_count = sum(1 for _ in open(args.train_file))
    valid_count = sum(1 for _ in open(args.valid_file))
    log.info(f"Train: {train_count:,} examples")
    log.info(f"Valid: {valid_count:,} examples")

    # ── Imports (after GPU env is set) ────────────────────────────────────────
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    # ── Tokenizer ─────────────────────────────────────────────────────────────
    log.info(f"Loading tokenizer: {args.model_id}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # ── Model ─────────────────────────────────────────────────────────────────
    log.info(f"Loading model: {args.model_id}  (~3 GB download on first run)")
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    model.enable_input_require_grads()
    log.info(f"Model params: {model.num_parameters():,}")

    # ── LoRA ──────────────────────────────────────────────────────────────────
    lora_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_rank * 2,
        target_modules=[
            "q_proj", "v_proj", "k_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── Dataset ───────────────────────────────────────────────────────────────
    log.info("Loading datasets...")
    train_ds = load_dataset("json", data_files=args.train_file, split="train")
    valid_ds = load_dataset("json", data_files=args.valid_file, split="train")

    def format_example(example):
        return {
            "text": tokenizer.apply_chat_template(
                example["messages"],
                tokenize=False,
                add_generation_prompt=False,
            )
        }

    train_ds = train_ds.map(format_example, remove_columns=["messages"])
    valid_ds = valid_ds.map(format_example, remove_columns=["messages"])

    # ── Checkpoint resume ─────────────────────────────────────────────────────
    existing = sorted(glob.glob(f"{args.output_dir}/checkpoint-*"))
    resume_from = existing[-1] if existing else None
    if resume_from:
        log.info(f"Resuming from checkpoint: {resume_from}")
    else:
        log.info("No checkpoint found — starting fresh.")

    # ── Training config ───────────────────────────────────────────────────────
    training_args = SFTConfig(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=args.lr,
        warmup_steps=264,
        lr_scheduler_type="cosine",
        max_length=args.max_seq_len,
        dataset_text_field="text",
        save_steps=args.save_steps,
        eval_steps=args.save_steps,
        eval_strategy="steps",
        logging_steps=50,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",
        dataloader_num_workers=4,
        fp16=fp16,
        bf16=bf16,
        optim="adamw_torch",
        weight_decay=0.01,
        packing=False,
    )

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        processing_class=tokenizer,
    )

    log.info("")
    log.info(f"  Epochs     : {args.epochs}")
    log.info(f"  Eff. batch : {batch_size * grad_accum}")
    log.info(f"  Seq length : {args.max_seq_len}")
    log.info(f"  LoRA rank  : {args.lora_rank}")
    log.info(f"  fp16/bf16  : {fp16}/{bf16}")
    log.info(f"  Checkpoints: every {args.save_steps} steps → {args.output_dir}/")
    log.info("")
    log.info("Expected loss: ~2.0 start  →  ~0.8 end of epoch 1  →  ~0.4 end of epoch 3")
    log.info("")

    start = time.time()
    trainer.train(resume_from_checkpoint=resume_from)
    elapsed = time.time() - start

    # ── Save ──────────────────────────────────────────────────────────────────
    final_path = f"{args.output_dir}/final"
    trainer.save_model(final_path)
    tokenizer.save_pretrained(final_path)

    log.info("")
    log.info(f"Training complete in {elapsed / 3600:.2f} hours")
    log.info(f"Adapter saved: {final_path}")
    log.info("Next: python3 generate_dpo_pairs.py && python3 dpo_train.py")


if __name__ == "__main__":
    main()
