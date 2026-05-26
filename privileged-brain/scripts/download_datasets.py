#!/usr/bin/env python3
"""
Download bash/shell command datasets from HuggingFace.

Sources (all confirmed available):
  - sahil2801/CodeAlpaca-20k  — 20k code instructions, ~1,400 bash-related
  - HuggingFaceH4/CodeAlpaca_20K — 18k code instructions, additional bash content
  - theblackcat102/evol-codealpaca-v1 — 111k evolved instructions, largest source
"""

import json
import re
from pathlib import Path

from datasets import load_dataset

RAW_DIR = Path("data/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Keywords that strongly indicate a bash/shell command pair
BASH_KEYWORDS = [
    "#!/bin/bash", "#!/bin/sh",
    "sudo ", "apt-get", "apt ", "systemctl", "journalctl",
    "chmod ", "chown ", "grep ", "awk ", "sed ",
    "find /", "find .", "ls -", "ps aux", "ps -",
    "curl ", "wget ", "ssh ", "scp ",
    "tar ", "gzip", "gunzip",
    "iptables", "ufw ", "netstat", "ss -",
    "df -", "du -", "free -",
    "kill ", "killall", "pkill",
    "crontab", "nohup ", "screen ",
    "mkdir ", "rmdir ", "rm -",
    "cat /etc", "cat /var", "tail -", "head -",
]


def is_bash_pair(instruction: str, output: str) -> bool:
    """True if the output looks like a bash command/script."""
    combined = (instruction + " " + output).lower()
    output_lower = output.lower().strip()

    # Must match at least one keyword
    if not any(kw.lower() in combined for kw in BASH_KEYWORDS):
        return False

    # Output should not be primarily Python/JS/SQL
    bad_starts = ["def ", "class ", "import ", "from ", "const ", "var ", "let ", "select ", "create table"]
    if any(output_lower.startswith(s) for s in bad_starts):
        return False

    # Skip very long outputs (multi-hundred line programs — not what we want)
    if output.count("\n") > 30:
        return False

    return True


def clean_output(output: str) -> str:
    """Strip markdown code fences if present."""
    output = output.strip()
    # Remove ```bash ... ``` or ```sh ... ``` wrappers
    output = re.sub(r"^```(?:bash|sh|shell|zsh)?\n", "", output)
    output = re.sub(r"\n?```$", "", output)
    return output.strip()


def download_codealpha_20k():
    print("Downloading sahil2801/CodeAlpaca-20k...")
    ds = load_dataset("sahil2801/CodeAlpaca-20k", split="train")
    pairs = []
    for row in ds:
        inst = (row.get("instruction") or "").strip()
        out = clean_output(row.get("output") or "")
        if inst and out and is_bash_pair(inst, out):
            pairs.append({"nl": inst, "bash": out})

    out_path = RAW_DIR / "codealpha_20k.jsonl"
    with open(out_path, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  Saved {len(pairs)} bash pairs -> {out_path}")
    return len(pairs)


def download_hf_codealpha():
    print("Downloading HuggingFaceH4/CodeAlpaca_20K...")
    ds = load_dataset("HuggingFaceH4/CodeAlpaca_20K", split="train")
    pairs = []
    for row in ds:
        inst = (row.get("prompt") or "").strip()
        out = clean_output(row.get("completion") or "")
        if inst and out and is_bash_pair(inst, out):
            pairs.append({"nl": inst, "bash": out})

    out_path = RAW_DIR / "hf_codealpha.jsonl"
    with open(out_path, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  Saved {len(pairs)} bash pairs -> {out_path}")
    return len(pairs)


def download_evol_codealpaca():
    print("Downloading theblackcat102/evol-codealpaca-v1 (111k rows — may take ~1 min)...")
    ds = load_dataset("theblackcat102/evol-codealpaca-v1", split="train")
    pairs = []
    for row in ds:
        inst = (row.get("instruction") or "").strip()
        out = clean_output(row.get("output") or "")
        if inst and out and is_bash_pair(inst, out):
            pairs.append({"nl": inst, "bash": out})

    out_path = RAW_DIR / "evol_codealpaca.jsonl"
    with open(out_path, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"  Saved {len(pairs)} bash pairs -> {out_path}")
    return len(pairs)


if __name__ == "__main__":
    total = 0
    total += download_codealpha_20k()
    total += download_hf_codealpha()
    total += download_evol_codealpaca()
    print(f"\nTotal bash pairs downloaded: {total}")
    print(f"Raw data directory: {RAW_DIR.resolve()}")
