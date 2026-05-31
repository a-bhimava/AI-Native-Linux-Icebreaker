#!/usr/bin/env python3
"""
SFT fine-tuning of Qwen 2.5 Coder 1.5B on NL2SH data.
Runs on Apple M4 via PyTorch MPS backend, or CUDA on GCP L4.

Usage:
  python3 scripts/sft_train.py
  python3 scripts/sft_train.py --cot              # CoT format (REASONING:/COMMAND:)
  python3 scripts/sft_train.py --epochs 5 --lr 1e-4
"""

import argparse
import json
import os
import sys
import torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--output-dir", default="training/adapters/sft")
    parser.add_argument("--low-memory", action="store_true",
                        help="Enable memory-saving mode: batch=1, grad_checkpointing, seq=256")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from latest checkpoint in output-dir")
    parser.add_argument("--cot", action="store_true",
                        help="Train on CoT-format data from data/processed/cot/")
    args = parser.parse_args()

    # Low-memory overrides — apply before any torch allocations
    if args.low_memory:
        args.batch_size = 1
        args.grad_accum = 16       # keeps effective batch size = 16
        args.max_seq_len = 256     # NL2SH pairs are short; no info lost
        # Cap MPS at ~60% of unified memory to leave headroom for OS
        os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.6"
        print("LOW MEMORY MODE: batch=1, grad_checkpointing=True, seq_len=256, MPS cap=60%")
    else:
        # Disable watermark limit in normal mode — let MPS manage freely
        os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"

    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    # --- Device ---
    if torch.backends.mps.is_available():
        device = "mps"
        print("Training device: Apple M4 Metal (MPS)")
    elif torch.cuda.is_available():
        device = "cuda"
        print("Training device: CUDA")
    else:
        device = "cpu"
        print("Training device: CPU (slow)")

    # --- Tokenizer ---
    print(f"Loading tokenizer: {args.model_id}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_id, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # --- Model ---
    print(f"Loading model: {args.model_id}")
    model = AutoModelForCausalLM.from_pretrained(
        args.model_id,
        torch_dtype=torch.bfloat16,
        device_map={"": device},
        trust_remote_code=True,
    )
    model.config.use_cache = False
    model.enable_input_require_grads()

    if args.low_memory:
        model.gradient_checkpointing_enable()
        print("  Gradient checkpointing enabled (trades ~20% speed for ~40% less activation RAM)")

    # --- LoRA ---
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

    # --- Dataset ---
    data_dir = "data/processed/cot" if args.cot else "data/processed/clean"
    print(f"Loading datasets from: {data_dir}/")
    train_ds = load_dataset("json", data_files=f"{data_dir}/train.jsonl", split="train")
    valid_ds = load_dataset("json", data_files=f"{data_dir}/valid.jsonl", split="train")
    print(f"  Train: {len(train_ds)} examples")
    print(f"  Valid: {len(valid_ds)} examples")
    print(f"  Format: {'CoT (REASONING:/COMMAND:)' if args.cot else 'standard (bare command)'}")

    if args.cot:
        # Verify format on 50 samples before spending GPU hours on malformed data
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
        from process_datasets import assert_cot_format
        errors = 0
        with open(f"{data_dir}/train.jsonl") as f:
            for i, line in enumerate(f):
                if i >= 50:
                    break
                try:
                    assert_cot_format(json.loads(line))
                except ValueError as e:
                    print(f"  [CoT format ERROR sample {i+1}]: {e}")
                    errors += 1
        if errors:
            print(f"\nABORTING: {errors}/50 samples failed CoT format check.")
            print("Run: python3 scripts/process_datasets.py --cot  to regenerate.")
            raise SystemExit(1)
        print("  CoT format pre-check: 50/50 OK — starting training")

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

    # --- Training config ---
    training_args = SFTConfig(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        warmup_steps=264,
        lr_scheduler_type="cosine",
        max_length=args.max_seq_len,
        dataset_text_field="text",
        save_steps=500,
        eval_steps=500,
        eval_strategy="steps",
        logging_steps=50,
        save_total_limit=3,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",
        dataloader_num_workers=0,     # Required for MPS — no multiprocessing
        fp16=False,
        bf16=False,                   # Model is already bfloat16; trainer flag not needed on MPS
        optim="adamw_torch",
        weight_decay=0.01,
        packing=False,                # Disable packing for stability
    )

    # --- Trainer ---
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        processing_class=tokenizer,
    )

    print(f"\nStarting SFT training ({args.epochs} epochs)...")
    print(f"Format               : {'CoT (REASONING:/COMMAND:)' if args.cot else 'standard'}")
    print(f"Effective batch size : {args.batch_size * args.grad_accum}")
    print(f"LoRA rank            : {args.lora_rank}")
    print(f"Max seq length       : {args.max_seq_len}")
    print(f"Low-memory mode      : {args.low_memory}")
    print(f"Output               : {args.output_dir}/")
    print(f"\nExpected loss curve  : ~2.0 start → ~0.8 after epoch 1 → ~0.4 after epoch 3")
    print(f"If loss stuck >1.5 after 500 steps, run with --low-memory and lower --lr 1e-4\n")

    resume_from = args.output_dir if args.resume else None
    trainer.train(resume_from_checkpoint=resume_from)

    final_path = f"{args.output_dir}/final"
    trainer.save_model(final_path)
    tokenizer.save_pretrained(final_path)
    print(f"\nSFT complete. Adapter saved to: {final_path}")


if __name__ == "__main__":
    main()
