#!/usr/bin/env python3
"""Convert raw NL→Bash pairs to ChatML JSONL format for TRL SFTTrainer."""

import json
import random
from pathlib import Path

RAW_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive natural language descriptions of system administration tasks and output "
    "ONLY the corresponding Bash command or shell pipeline. "
    "Rules you must always follow:\n"
    "1. Output ONLY the command — no explanations, no markdown, no code fences.\n"
    "2. Prefer minimal-scope, reversible commands.\n"
    "3. Never read or process external data (emails, documents, URLs).\n"
    "4. If a request is ambiguous or dangerous, output: REFUSE: <one-line reason>."
)


def to_chatml(nl: str, bash: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": nl},
            {"role": "assistant", "content": bash},
        ]
    }


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def is_valid(nl: str, bash: str) -> bool:
    """Filter out empty, suspiciously short, or clearly dangerous training examples."""
    if not nl or not bash:
        return False
    if len(nl) < 5 or len(bash) < 2:
        return False
    # Skip examples that train the model to destroy the system
    dangerous = ["rm -rf /", "mkfs", "dd if=/dev/zero", "> /dev/sda", "chmod -R 777 /"]
    for d in dangerous:
        if d in bash:
            return False
    return True


def main():
    all_pairs = []

    for fname in ["nl2bash.jsonl", "bash_commands.jsonl",
                  "codealpha_20k.jsonl", "hf_codealpha.jsonl", "evol_codealpaca.jsonl"]:
        rows = load_jsonl(RAW_DIR / fname)
        for row in rows:
            nl = (row.get("nl") or row.get("invocation") or row.get("input") or "").strip()
            bash = (row.get("bash") or row.get("cmd") or row.get("command") or row.get("output") or "").strip()
            if is_valid(nl, bash):
                all_pairs.append((nl, bash))

    # Load synthetic data if it exists
    synthetic_rows = load_jsonl(Path("data/synthetic/synthetic_pairs.jsonl"))
    for row in synthetic_rows:
        nl = row.get("nl", "").strip()
        bash = row.get("bash", "").strip()
        if is_valid(nl, bash):
            all_pairs.append((nl, bash))

    # Load advanced synthetic pairs (complex pipes, modern tools, edge cases)
    advanced_rows = load_jsonl(Path("data/synthetic/synthetic_advanced.jsonl"))
    for row in advanced_rows:
        nl = row.get("nl", "").strip()
        bash = row.get("bash", "").strip()
        if is_valid(nl, bash):
            all_pairs.append((nl, bash))

    print(f"Total valid pairs: {len(all_pairs)}")

    random.seed(42)
    random.shuffle(all_pairs)

    split_idx = int(len(all_pairs) * 0.9)
    train_pairs = all_pairs[:split_idx]
    valid_pairs = all_pairs[split_idx:]

    for split_name, pairs in [("train", train_pairs), ("valid", valid_pairs)]:
        out_path = PROCESSED_DIR / f"{split_name}.jsonl"
        with open(out_path, "w") as f:
            for nl, bash in pairs:
                f.write(json.dumps(to_chatml(nl, bash)) + "\n")
        print(f"  {split_name}: {len(pairs)} examples -> {out_path}")

    print(f"\nProcessed data directory: {PROCESSED_DIR.resolve()}")


if __name__ == "__main__":
    main()
