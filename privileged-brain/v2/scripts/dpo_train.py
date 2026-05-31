#!/usr/bin/env python3
"""
DPO training: teach the model to prefer safe, minimal commands over dangerous ones.
Loads the SFT adapter as starting point.

Usage:
  python3 scripts/dpo_train.py                        # standard DPO
  python3 scripts/dpo_train.py --cot                  # CoT DPO (Run 7 → Run 7 DPO)
  python3 scripts/dpo_train.py --cot --sft-adapter training/adapters/run7_cot/final
"""

import argparse
import os
import torch

os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

from datasets import load_dataset
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import DPOConfig, DPOTrainer


SYSTEM_PROMPT_COT = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive natural language descriptions of system administration tasks and output "
    "a structured two-line response:\n\n"
    "REASONING: <one sentence — what this command does and why it is safe to run>\n"
    "COMMAND: <the bare bash command>\n\n"
    "Rules you must always follow:\n"
    "1. REASONING must be exactly one line — your internal safety check before executing.\n"
    "2. COMMAND must be exactly one line — the bare shell command, no markdown, no fences.\n"
    "3. Prefer minimal-scope, reversible commands.\n"
    "4. Never read or process external data (emails, documents, URLs).\n"
    "5. If a request is ambiguous or dangerous: COMMAND: REFUSE: <one-line reason>\n"
    "6. If a request is too vague to safely execute: COMMAND: CLARIFY: <one specific question>"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cot", action="store_true",
                        help="Use CoT DPO dataset and CoT system prompt")
    parser.add_argument("--sft-adapter", default=None, dest="sft_adapter",
                        help="Path to SFT adapter (default: run7_cot/final with --cot, sft/final otherwise)")
    args = parser.parse_args()

    model_id = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    if args.cot:
        sft_adapter = args.sft_adapter or "training/adapters/run7_cot/final"
        output_dir = "training/adapters/run7_cot_dpo_v2"
        data_file = "data/processed/dpo_pairs_cot.jsonl"
    else:
        sft_adapter = args.sft_adapter or "training/adapters/sft/final"
        output_dir = "training/adapters/dpo"
        data_file = "data/processed/dpo_pairs.jsonl"

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

    # --- Policy model: SFT adapter (trainable) ---
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

    # --- Reference model: SFT adapter merged and frozen ---
    # Must be the SFT model, not bare base — base model assigns near-zero probability
    # to REFUSE/CoT format, which inverts the DPO reward signal.
    print("Loading reference model (SFT merged, frozen)...")
    ref_base = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map={"": device},
        trust_remote_code=True,
    )
    ref_model = PeftModel.from_pretrained(ref_base, sft_adapter)
    ref_model = ref_model.merge_and_unload()
    ref_model.eval()
    for param in ref_model.parameters():
        param.requires_grad = False

    # --- Dataset ---
    dpo_data = load_dataset("json", data_files=data_file, split="train")
    print(f"DPO pairs: {len(dpo_data)} ({'CoT' if args.cot else 'standard'} format)")

    def format_prompt(example):
        if args.cot:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_COT},
                {"role": "user", "content": example["prompt"]},
            ]
        else:
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
        processing_class=tokenizer,
    )

    print("\nStarting DPO training...")
    trainer.train()

    final_path = f"{output_dir}/final"
    trainer.save_model(final_path)
    tokenizer.save_pretrained(final_path)
    print(f"\nDPO complete. Adapter saved to: {final_path}")


if __name__ == "__main__":
    main()
