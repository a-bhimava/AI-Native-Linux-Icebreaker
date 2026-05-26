#!/usr/bin/env python3
"""
DPO training: teach the model to prefer safe, minimal commands over dangerous ones.
Loads the SFT adapter as starting point.

Usage:
  python3 scripts/dpo_train.py
"""

import os
import torch

os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

from datasets import load_dataset
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import DPOConfig, DPOTrainer


def main():
    model_id = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    sft_adapter = "training/adapters/sft/final"
    output_dir = "training/adapters/dpo"

    if torch.backends.mps.is_available():
        device = "mps"
        print("DPO training device: Apple M4 Metal (MPS)")
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    # --- Tokenizer ---
    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # --- Load SFT-finetuned model as starting point ---
    print(f"Loading SFT adapter from: {sft_adapter}")
    base_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map={"": device},
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base_model, sft_adapter)
    model.config.use_cache = False
    model.enable_input_require_grads()

    # Reference model (frozen — base model without adapters)
    ref_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map={"": device},
        trust_remote_code=True,
    )

    # --- Dataset ---
    dpo_data = load_dataset("json", data_files="data/processed/dpo_pairs.jsonl", split="train")
    print(f"DPO pairs: {len(dpo_data)}")

    def format_prompt(example):
        """Apply chat template to prompt field."""
        messages = [{"role": "user", "content": example["prompt"]}]
        return {
            "prompt": tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            ),
            "chosen": example["chosen"] + tokenizer.eos_token,
            "rejected": example["rejected"] + tokenizer.eos_token,
        }

    dpo_data = dpo_data.map(format_prompt)
    split = dpo_data.train_test_split(test_size=0.1, seed=42)

    # --- DPO config ---
    dpo_args = DPOConfig(
        output_dir=output_dir,
        num_train_epochs=2,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=5e-5,
        beta=0.1,                  # KL penalty weight
        max_length=512,
        max_prompt_length=256,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        save_steps=200,
        eval_steps=200,
        eval_strategy="steps",
        logging_steps=20,
        save_total_limit=2,
        report_to="none",
        dataloader_num_workers=0,  # MPS requirement
        fp16=False,
        bf16=False,
        remove_unused_columns=False,
    )

    trainer = DPOTrainer(
        model=model,
        ref_model=ref_model,
        args=dpo_args,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        tokenizer=tokenizer,
    )

    print("\nStarting DPO training...")
    trainer.train()

    final_path = f"{output_dir}/final"
    trainer.save_model(final_path)
    tokenizer.save_pretrained(final_path)
    print(f"\nDPO complete. Adapter saved to: {final_path}")


if __name__ == "__main__":
    main()
