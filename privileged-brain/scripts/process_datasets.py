#!/usr/bin/env python3
"""Convert raw NL->Bash pairs to ChatML JSONL format for TRL SFTTrainer."""

import json
import re
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

# Prose indicators: if the bash field starts with any of these, it's not a command.
_PROSE_STARTS = (
    "here ", "sure", "this will", "to do", "you can", "the ", "i would",
    "1.", "2.", "```", "#!", "below", "in this", "first,", "note:",
    "this is", "to use", "you need", "to run", "the command", "use ",
    "run the", "this command", "you should", "to find", "to list",
)

_MAX_CMD_LEN = 500   # no command is genuinely longer than this
_MAX_NL_LEN  = 300   # reject paragraph-length NL descriptions


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
    """Return True only if the example is a genuine NL->bash pair, not prose."""
    if not nl or not bash:
        return False
    if len(nl) < 5 or len(bash) < 2:
        return False
    if len(bash) > _MAX_CMD_LEN:
        return False
    if len(nl) > _MAX_NL_LEN:
        return False
    if "```" in bash:
        return False
    if "\n\n" in bash:
        return False
    if bash.lower().startswith(_PROSE_STARTS):
        return False
    # Skip destructive commands that would train dangerous behaviour
    for danger in ["rm -rf /", "mkfs", "dd if=/dev/zero", "> /dev/sda", "chmod -R 777 /"]:
        if danger in bash:
            return False
    # Man-page OR syntax: -flag1|-flag2 or --flag1|--flag2 scraped from docs
    if re.search(r'-[\w-]+\|-[\w-]+', bash):
        return False
    # Trailing ellipsis — pseudo-code placeholder, not a real command
    if bash.rstrip().endswith(('...', '…')):
        return False
    # echo-wrapped command — prints instead of executes
    _ECHO_CMDS = (
        'find', 'ln', 'cp', 'mv', 'rm', 'chmod', 'chown', 'mkdir', 'touch',
        'curl', 'wget', 'git', 'sudo', 'systemctl', 'service', 'docker',
        'kubectl', 'python', 'pip', 'npm', 'yarn', 'apt', 'yum', 'brew',
    )
    if re.match(r'^echo\s+(' + '|'.join(_ECHO_CMDS) + r')\b', bash, re.IGNORECASE):
        return False
    return True


def _load_source(fname: str, nl_key: str, bash_key: str) -> list[tuple[str, str]]:
    pairs = []
    for row in load_jsonl(RAW_DIR / fname):
        nl = (row.get(nl_key) or "").strip()
        bash = (row.get(bash_key) or "").strip()
        if is_valid(nl, bash):
            pairs.append((nl, bash))
    return pairs


def main():
    counts = {}
    all_pairs = []

    # ── New verified datasets (downloaded by download_datasets.py) ────────────
    for fname, nl_key, bash_key in [
        ("nl2sh_alfa_train.jsonl", "nl",    "bash"),   # NL2SH-ALFA train
        ("tldr_train.jsonl",       "nl",    "bash"),   # TLDR (neulab)
        ("linux_commands.jsonl",   "nl",    "bash"),   # mecha-org/linux-command-dataset
    ]:
        pairs = _load_source(fname, nl_key, bash_key)
        counts[fname] = len(pairs)
        all_pairs.extend(pairs)

    # ── Legacy NL2Bash (10 examples, kept for continuity) ────────────────────
    pairs = _load_source("nl2bash.jsonl", "nl", "bash")
    counts["nl2bash.jsonl"] = len(pairs)
    all_pairs.extend(pairs)

    # ── Synthetic pairs (project-specific + REFUSE examples) ─────────────────
    for fpath, nl_key, bash_key in [
        (Path("data/synthetic/synthetic_pairs.jsonl"),   "nl", "bash"),
        (Path("data/synthetic/synthetic_advanced.jsonl"),"nl", "bash"),
    ]:
        rows = load_jsonl(fpath)
        pairs = []
        for row in rows:
            nl = row.get(nl_key, "").strip()
            bash = row.get(bash_key, "").strip()
            if is_valid(nl, bash):
                pairs.append((nl, bash))
        counts[fpath.name] = len(pairs)
        all_pairs.extend(pairs)

    print("Source breakdown:")
    for src, n in counts.items():
        print(f"  {src:<35} {n:>6} pairs")
    print(f"  {'TOTAL':<35} {len(all_pairs):>6} pairs")

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
