#!/usr/bin/env python3
"""
DPO training — Google Cloud VM (T4 / V100 / A100).
Loads the SFT adapter as starting point and trains on preference pairs.
Auto-resumes from latest checkpoint.

Usage:
  python3 dpo_train.py
"""

import glob
import logging
import os
import sys
import time

import torch

os.makedirs("training/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("training/logs/dpo_train.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)


def detect_gpu():
    if not torch.cuda.is_available():
        return False, False, 1, 16
    name = torch.cuda.get_device_name(0)
    mem_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
    if "A100" in name and mem_gb > 60:
        return False, True, 8, 2
    elif "A100" in name:
        return False, True, 4, 4
    elif "V100" in name:
        return True, False, 2, 4
    else:  # T4
        return True, False, 2, 4


def main():
    model_id    = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    sft_adapter = "training/adapters/sft/final"
    output_dir  = "training/adapters/dpo"
    dpo_file    = "data/dpo_pairs.jsonl"

    os.makedirs(output_dir, exist_ok=True)
    log.info("=" * 60)
    log.info("  Privileged Brain — DPO Training (GCP GPU)")
    log.info("=" * 60)

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not os.path.exists(sft_adapter):
        log.error(f"SFT adapter not found: {sft_adapter}")
        log.error("Run sft_train.py first.")
        sys.exit(1)
    if not os.path.exists(dpo_file):
        log.error(f"DPO pairs not found: {dpo_file}")
        log.error("Run generate_dpo_pairs.py first.")
        sys.exit(1)

    fp16, bf16, batch_size, grad_accum = detect_gpu()
    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    log.info(f"GPU: {gpu_name}  |  fp16={fp16}  bf16={bf16}  batch={batch_size}")

    from datasets import load_dataset
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import DPOConfig, DPOTrainer

    # ── Tokenizer ─────────────────────────────────────────────────────────────
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # ── Model: base + SFT adapter ─────────────────────────────────────────────
    log.info(f"Loading base model: {model_id}")
    base_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    log.info(f"Loading SFT adapter: {sft_adapter}")
    model = PeftModel.from_pretrained(base_model, sft_adapter)
    model.config.use_cache = False
    model.enable_input_require_grads()

    # ── Dataset ───────────────────────────────────────────────────────────────
    dpo_data = load_dataset("json", data_files=dpo_file, split="train")
    log.info(f"DPO pairs: {len(dpo_data)}")

    def format_prompt(example):
        messages = [{"role": "user", "content": example["prompt"]}]
        return {
            "prompt": tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            ),
            "chosen":   example["chosen"]   + tokenizer.eos_token,
            "rejected": example["rejected"] + tokenizer.eos_token,
        }

    dpo_data = dpo_data.map(format_prompt)
    split = dpo_data.train_test_split(test_size=0.1, seed=42)

    # ── Checkpoint resume ─────────────────────────────────────────────────────
    existing = sorted(glob.glob(f"{output_dir}/checkpoint-*"))
    resume_from = existing[-1] if existing else None
    if resume_from:
        log.info(f"Resuming from checkpoint: {resume_from}")

    # ── DPO config ────────────────────────────────────────────────────────────
    dpo_args = DPOConfig(
        output_dir=output_dir,
        num_train_epochs=2,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=5e-5,
        beta=0.1,
        max_length=512,
        max_prompt_length=256,
        warmup_steps=50,
        lr_scheduler_type="cosine",
        save_steps=100,
        eval_steps=100,
        eval_strategy="steps",
        logging_steps=20,
        save_total_limit=2,
        report_to="none",
        dataloader_num_workers=4,
        fp16=fp16,
        bf16=bf16,
        remove_unused_columns=False,
    )

    trainer = DPOTrainer(
        model=model,
        ref_model=None,   # implicit reference via PEFT disabled adapters
        args=dpo_args,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        processing_class=tokenizer,
    )

    log.info("Starting DPO training...")
    start = time.time()
    trainer.train(resume_from_checkpoint=resume_from)
    elapsed = time.time() - start

    final_path = f"{output_dir}/final"
    trainer.save_model(final_path)
    tokenizer.save_pretrained(final_path)

    log.info(f"DPO complete in {elapsed / 60:.1f} min")
    log.info(f"Adapter saved: {final_path}")
    log.info("Next: download adapters to Mac → bash 05_convert_and_import.sh")


if __name__ == "__main__":
    main()
