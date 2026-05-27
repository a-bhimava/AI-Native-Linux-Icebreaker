#!/usr/bin/env python3
"""
Print random samples from training/validation/eval data for manual inspection.

Usage:
    python3 scripts/sample_data.py                  # 20 samples from train
    python3 scripts/sample_data.py --n 50           # 50 samples
    python3 scripts/sample_data.py --split valid    # from validation set
    python3 scripts/sample_data.py --split eval     # from NL2SH-ALFA test set
    python3 scripts/sample_data.py --split all      # sample across all three
    python3 scripts/sample_data.py --seed 0         # reproducible sample
"""

import argparse
import json
import re
import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent

FILES = {
    "train": BASE_DIR / "data/processed/train.jsonl",
    "valid": BASE_DIR / "data/processed/valid.jsonl",
    "eval":  BASE_DIR / "eval/nl2sh_alfa_test.jsonl",
}

DIVIDER = "─" * 72


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def extract_nl_bash(row: dict) -> tuple[str, str]:
    """Works for both ChatML format and raw {nl, bash} format."""
    if "messages" in row:
        msgs = row["messages"]
        nl = next((m["content"] for m in msgs if m["role"] == "user"), "")
        bash = next((m["content"] for m in msgs if m["role"] == "assistant"), "")
    else:
        nl = row.get("nl", "")
        bash = row.get("bash", "")
    return nl.strip(), bash.strip()


_ECHO_CMDS_RE = re.compile(
    r'^echo\s+(find|ln|cp|mv|rm|chmod|chown|mkdir|touch|curl|wget|git|sudo'
    r'|systemctl|service|docker|kubectl|python|pip|npm|yarn|apt|yum|brew)\b',
    re.IGNORECASE,
)


def flag(bash: str) -> str:
    """Return a warning tag if the output looks like prose or a malformed command."""
    prose_starts = (
        "here ", "sure", "this will", "to do", "you can", "the ", "i would",
        "1.", "2.", "```", "below", "in this", "first,", "note:",
        "this is", "to use", "you need", "to run", "the command", "use ",
        "run the", "this command", "you should", "to find", "to list",
    )
    if bash.lower().startswith(prose_starts):
        return "  ⚠  LOOKS LIKE PROSE"
    if "```" in bash or bash.startswith("`") or bash.endswith("`"):
        return "  ⚠  CONTAINS BACKTICKS"
    if len(bash) > 500:
        return f"  ⚠  TOO LONG ({len(bash)} chars)"
    if "\n\n" in bash:
        return "  ⚠  DOUBLE NEWLINE (paragraph)"
    if re.search(r'-[\w-]+\|-[\w-]+', bash):
        return "  ⚠  MAN-PAGE OR SYNTAX (|)"
    if bash.rstrip().endswith(('...', '…')):
        return "  ⚠  TRAILING ELLIPSIS (pseudo-code)"
    if _ECHO_CMDS_RE.match(bash):
        return "  ⚠  ECHO-WRAPPED CMD (no-op)"
    return ""


def print_sample(idx: int, total: int, split: str, nl: str, bash: str):
    warn = flag(bash)
    print(f"\n{DIVIDER}")
    print(f"  [{idx+1}/{total}]  split={split}{warn}")
    print(DIVIDER)
    print(f"  NL  : {nl}")
    print(f"  BASH: {bash}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n",     type=int, default=20, help="Number of samples to print")
    parser.add_argument("--split", default="train", choices=["train", "valid", "eval", "all"])
    parser.add_argument("--seed",  type=int, default=None, help="Random seed for reproducibility")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    if args.split == "all":
        splits_to_sample = ["train", "valid", "eval"]
    else:
        splits_to_sample = [args.split]

    # Load and tag each row with its split name
    tagged: list[tuple[str, dict]] = []
    for split in splits_to_sample:
        path = FILES[split]
        if not path.exists():
            print(f"WARNING: {path} not found — skipping", file=sys.stderr)
            continue
        rows = load_jsonl(path)
        tagged.extend((split, r) for r in rows)
        print(f"Loaded {len(rows):,} rows from {split} ({path.name})", file=sys.stderr)

    if not tagged:
        print("No data found. Run process_datasets.py first.", file=sys.stderr)
        sys.exit(1)

    n = min(args.n, len(tagged))
    sample = rng.sample(tagged, n)

    print(f"\n{'='*72}", file=sys.stderr)
    print(f"  Showing {n} random samples  (total pool: {len(tagged):,})", file=sys.stderr)
    print(f"{'='*72}", file=sys.stderr)

    warnings = 0
    for i, (split, row) in enumerate(sample):
        nl, bash = extract_nl_bash(row)
        print_sample(i, n, split, nl, bash)
        if flag(bash):
            warnings += 1

    print(f"\n{DIVIDER}")
    print(f"  Done. {warnings}/{n} samples flagged as suspicious.")
    print(DIVIDER)


if __name__ == "__main__":
    main()
