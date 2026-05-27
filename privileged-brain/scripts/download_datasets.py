#!/usr/bin/env python3
"""
Download verified NL->bash datasets from HuggingFace.

Sources (all confirmed NL->bash only, no general coding Q&A):
  - westenfelder/NL2SH-ALFA  -- 40,639 train + 600 manually-verified test (NAACL 2025)
  - neulab/tldr               -- 6,414 train pairs from tldr-pages (MIT)
  - mecha-org/linux-command-dataset -- 8,669 tested Linux commands (Apache 2.0)
"""

import json
from pathlib import Path

from datasets import load_dataset

RAW_DIR = Path("data/raw")
EVAL_DIR = Path("eval")
RAW_DIR.mkdir(parents=True, exist_ok=True)
EVAL_DIR.mkdir(parents=True, exist_ok=True)


def download_nl2sh_alfa():
    print("Downloading westenfelder/NL2SH-ALFA (train + test)...")
    # This dataset uses named configs ('train', 'test') not dataset splits
    ds_train = load_dataset("westenfelder/NL2SH-ALFA", "train", split="train")
    ds_test  = load_dataset("westenfelder/NL2SH-ALFA", "test",  split="train")

    train_pairs = []
    for row in ds_train:
        nl = (row.get("nl") or "").strip()
        bash = (row.get("bash") or "").strip()
        if nl and bash:
            train_pairs.append({"nl": nl, "bash": bash})

    train_path = RAW_DIR / "nl2sh_alfa_train.jsonl"
    with open(train_path, "w") as f:
        for p in train_pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  Train: {len(train_pairs)} pairs -> {train_path}")

    test_pairs = []
    for row in ds_test:
        nl = (row.get("nl") or "").strip()
        bash = (row.get("bash") or row.get("bash2") or "").strip()
        if nl and bash:
            test_pairs.append({"nl": nl, "bash": bash})

    test_path = EVAL_DIR / "nl2sh_alfa_test.jsonl"
    with open(test_path, "w") as f:
        for p in test_pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  Test (eval only, NOT for training): {len(test_pairs)} pairs -> {test_path}")

    return len(train_pairs)


def download_tldr():
    print("Downloading neulab/tldr (train split)...")
    try:
        ds = load_dataset("neulab/tldr", split="train", trust_remote_code=True)
    except Exception as e:
        print(f"  SKIP: neulab/tldr unavailable ({type(e).__name__}: {e})")
        print("  This dataset uses a legacy script; skipping — NL2SH-ALFA is sufficient.")
        return 0
    pairs = []
    for row in ds:
        nl = (row.get("nl") or "").strip()
        bash = (row.get("cmd") or "").strip()
        if nl and bash:
            pairs.append({"nl": nl, "bash": bash})

    out_path = RAW_DIR / "tldr_train.jsonl"
    with open(out_path, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  {len(pairs)} pairs -> {out_path}")
    return len(pairs)


def download_linux_commands():
    print("Downloading mecha-org/linux-command-dataset...")
    ds = load_dataset("mecha-org/linux-command-dataset", split="train")
    pairs = []
    for row in ds:
        nl = (row.get("input") or "").strip()
        bash = (row.get("output") or "").strip()
        if nl and bash:
            pairs.append({"nl": nl, "bash": bash})

    out_path = RAW_DIR / "linux_commands.jsonl"
    with open(out_path, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  {len(pairs)} pairs -> {out_path}")
    return len(pairs)


if __name__ == "__main__":
    total = 0
    total += download_nl2sh_alfa()
    total += download_tldr()
    total += download_linux_commands()
    print(f"\nTotal raw pairs downloaded: {total}")
    print(f"Eval set (held out): eval/nl2sh_alfa_test.jsonl")
    print(f"Raw data directory:  {RAW_DIR.resolve()}")
