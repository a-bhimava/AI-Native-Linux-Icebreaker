#!/usr/bin/env python3
"""Convert raw NL->Bash pairs to ChatML JSONL format for TRL SFTTrainer."""

import argparse
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

SYSTEM_PROMPT_COT = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive natural language descriptions of system administration tasks. "
    "Rules you must always follow:\n"
    "1. First write one REASONING line: explain what the intent requires and which tool/path achieves it.\n"
    "2. Then write one COMMAND line: the exact Bash command or shell pipeline — no markdown, no fences.\n"
    "3. Prefer minimal-scope, reversible commands.\n"
    "4. Never read or process external data (emails, documents, URLs).\n"
    "5. If a request is ambiguous or dangerous, write: REASONING: Request is refused.\n"
    "   COMMAND: REFUSE: <one-line reason>\n"
    "Output format (always exactly two lines):\n"
    "REASONING: <one sentence>\n"
    "COMMAND: <bash command>"
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


def _key_tool(bash: str) -> str:
    """Extract the primary command name from a bash string (skip sudo)."""
    tokens = bash.strip().split()
    for tok in tokens:
        clean = tok.lstrip("-")
        if clean and not clean.startswith("-") and clean not in ("sudo",):
            return clean
    return tokens[0] if tokens else bash


def _make_reasoning(nl: str, bash: str) -> str:
    """Generate a template reasoning trace from an nl+bash pair."""
    tool = _key_tool(bash)
    nl_lower = nl.strip().rstrip(".")
    # For REFUSE responses, just note the refusal
    if bash.startswith("REFUSE:"):
        return f"Request '{nl_lower}' is outside safe operating parameters and must be refused."
    return f"To {nl_lower}, use {tool} which handles this operation with minimal scope."


def to_chatml(nl: str, bash: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": nl},
            {"role": "assistant", "content": bash},
        ]
    }


def to_chatml_cot(nl: str, bash: str, reasoning: str = "") -> dict:
    """Format a training example with Chain-of-Thought prefix.

    If a pre-written reasoning string is supplied (e.g. from Claude-generated data)
    it is used directly; otherwise a template trace is generated from nl+bash.
    """
    if not reasoning:
        reasoning = _make_reasoning(nl, bash)
    assistant_content = f"REASONING: {reasoning}\nCOMMAND: {bash}"
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_COT},
            {"role": "user", "content": nl},
            {"role": "assistant", "content": assistant_content},
        ]
    }


def assert_cot_format(example: dict) -> None:
    """Raise ValueError if the assistant turn does not follow REASONING/COMMAND format."""
    messages = example.get("messages", [])
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            lines = content.strip().splitlines()
            if len(lines) < 2:
                raise ValueError(f"CoT example has fewer than 2 lines: {content!r}")
            if not lines[0].startswith("REASONING:"):
                raise ValueError(f"CoT example missing REASONING prefix: {lines[0]!r}")
            if not lines[1].startswith("COMMAND:"):
                raise ValueError(f"CoT example missing COMMAND prefix: {lines[1]!r}")
            return
    raise ValueError("No assistant message found in example")


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
    # Single-backtick wrapping: scraped from markdown inline code
    if bash.startswith("`") or bash.endswith("`"):
        return False
    if "\n\n" in bash:
        return False
    if bash.lower().startswith(_PROSE_STARTS):
        return False
    # Unicode dashes masquerading as hyphens (em-dash, en-dash, minus sign U+2212)
    if re.search(r'[–—−]', bash):
        return False
    # & immediately before done — bash syntax error in loops
    if re.search(r'&\s*done\b', bash):
        return False
    # echo inside xargs — xargs just prints the command string, doesn't execute it
    if re.search(
        r'\bxargs\b[^|&;]*\becho\s+(mv|cp|rm|ln|chmod|chown|mkdir|find|sudo|git)\b',
        bash, re.I,
    ):
        return False
    # du -h piped to sort -n: human-readable sizes sorted as plain numbers (wrong order)
    if re.search(r'\bdu\b[^|]*-[a-zA-Z]*h[^|]*\|[^|]*\bsort\b[^|]*-[a-zA-Z]*n\b', bash):
        return False
    # Skip destructive commands that would train dangerous behaviour
    for danger in [
        "rm -rf /", "mkfs", "dd if=/dev/zero", "> /dev/sda", "chmod -R 777 /",
        # find executing rm (functionally equivalent to rm -rf /)
        "-exec rm ", "-exec rm\t",
        # xargs piped delete
        "xargs rm -rf", "xargs rm -fr",
    ]:
        if danger in bash:
            return False
    # Ellipsis anywhere — pseudo-code placeholder (catches middle + trailing)
    if '...' in bash or '…' in bash:
        return False
    # No-space pipe: man-page OR notation (hl|c|r) or missing spaces (www|split)
    # Generalises the old dashed-flag pattern; real pipes always have surrounding spaces
    if re.search(r'(?<!\s)\|(?!\s)', bash):
        return False
    # echo-wrapped command — prints instead of executes
    _ECHO_CMDS = (
        'find', 'ln', 'cp', 'mv', 'rm', 'chmod', 'chown', 'mkdir', 'touch',
        'curl', 'wget', 'git', 'sudo', 'systemctl', 'service', 'docker',
        'kubectl', 'python', 'pip', 'npm', 'yarn', 'apt', 'yum', 'brew',
    )
    if re.match(r'^echo\s+(' + '|'.join(_ECHO_CMDS) + r')\b', bash, re.IGNORECASE):
        return False
    # xargs + find -exec mashup: \; belongs to -exec, not xargs
    if re.search(r'\bxargs\b.*\\\s*;', bash):
        return False
    # cat on binary files — dumps raw bytes to terminal, corrupts session
    _BIN_EXTS = (r'jpg|jpeg|png|gif|bmp|ico|tiff|webp|mp3|mp4|avi|mov|wav'
                 r'|zip|tar\.gz|7z|gz|bin|exe|dll|so|o')
    # Direct: cat file.jpg  OR  cat *.png
    if re.search(r'\bcat\s+.*\.(' + _BIN_EXTS + r')\b', bash, re.I):
        return False
    # Indirect: find ... *.jpg ... -exec cat / | xargs cat
    if re.search(r'\bfind\b.*\.(' + _BIN_EXTS + r')\b.*\bcat\b', bash, re.I):
        return False
    # Command duplication — same token sequence pasted twice on one line
    _words = bash.split()
    if len(_words) >= 10 and _words[:len(_words) // 2] == _words[len(_words) // 2:]:
        return False
    # Terminal prompt prefix — scraped with shell prompt symbol attached
    if bash.startswith('$ ') or bash.startswith('% '):
        return False
    # Unquoted glob in find -name/-iname: bash expands * before find runs
    # Matches: find ... -name *.txt  (no surrounding quotes around the glob)
    if re.search(r'(?:^|\s)-i?name\s+(?![\'"])[^\s\'";]*[*?]', bash):
        return False
    # $(subshell) inside single quotes — evaluates literally, not as a command
    # Catches: sed -i '1s/.*/$(date ...)/'  where $(date) won't expand
    if re.search(r"'[^']*\$\([^']*\)[^']*'", bash):
        return False
    # NL scraping artifacts — markdown footnote/anchor syntax in the prompt text
    if re.search(r"\['\w*'\]|\[`[^`]*`\]|\[\w+\]\(\)", nl):
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean-only", action="store_true",
                        help="Use only nl2bash_full + synthetic (clean baseline run 3)")
    parser.add_argument("--cot", action="store_true",
                        help="Format examples with Chain-of-Thought REASONING/COMMAND prefix")
    args = parser.parse_args()

    counts = {}
    # Each entry is (nl, bash, reasoning_or_empty_string)
    all_pairs: list[tuple[str, str, str]] = []

    if args.clean_only:
        out_dir = PROCESSED_DIR / "clean"
        print("Mode: CLEAN BASELINE (nl2bash_full + synthetic only)")
        # ── NL2Bash full (Lin et al. 2018) ────────────────────────────────────
        pairs = _load_source("nl2bash_full.jsonl", "nl", "bash")
        counts["nl2bash_full.jsonl"] = len(pairs)
        all_pairs.extend((nl, bash, "") for nl, bash in pairs)
    else:
        out_dir = PROCESSED_DIR
        # ── New verified datasets (downloaded by download_datasets.py) ────────
        for fname, nl_key, bash_key in [
            ("nl2sh_alfa_train.jsonl", "nl",    "bash"),   # NL2SH-ALFA train
            ("tldr_train.jsonl",       "nl",    "bash"),   # TLDR (neulab)
            ("linux_commands.jsonl",   "nl",    "bash"),   # mecha-org/linux-command-dataset
        ]:
            pairs = _load_source(fname, nl_key, bash_key)
            counts[fname] = len(pairs)
            all_pairs.extend((nl, bash, "") for nl, bash in pairs)

        # ── Legacy NL2Bash (10 examples, kept for continuity) ─────────────────
        pairs = _load_source("nl2bash.jsonl", "nl", "bash")
        counts["nl2bash.jsonl"] = len(pairs)
        all_pairs.extend((nl, bash, "") for nl, bash in pairs)

    # ── Synthetic pairs (project-specific + REFUSE examples) — always included
    for fpath, nl_key, bash_key in [
        (Path("data/synthetic/synthetic_pairs.jsonl"),   "nl", "bash"),
        (Path("data/synthetic/synthetic_advanced.jsonl"),"nl", "bash"),
    ]:
        rows = load_jsonl(fpath)
        for row in rows:
            nl = row.get(nl_key, "").strip()
            bash = row.get(bash_key, "").strip()
            reasoning = row.get("reasoning", "").strip()
            if is_valid(nl, bash):
                all_pairs.append((nl, bash, reasoning))
        counts[fpath.name] = sum(
            1 for row in load_jsonl(fpath)
            if is_valid(row.get(nl_key, "").strip(), row.get(bash_key, "").strip())
        )

    print("Source breakdown:")
    for src, n in counts.items():
        print(f"  {src:<35} {n:>6} pairs")
    print(f"  {'TOTAL':<35} {len(all_pairs):>6} pairs")
    if args.cot:
        print("  Mode: CoT (REASONING/COMMAND format)")

    random.seed(42)
    random.shuffle(all_pairs)

    split_idx = int(len(all_pairs) * 0.9)
    train_pairs = all_pairs[:split_idx]
    valid_pairs = all_pairs[split_idx:]

    if args.cot:
        out_dir = Path(str(out_dir) + "_cot")

    out_dir.mkdir(parents=True, exist_ok=True)
    for split_name, pairs in [("train", train_pairs), ("valid", valid_pairs)]:
        out_path = out_dir / f"{split_name}.jsonl"
        cot_errors = 0
        with open(out_path, "w") as f:
            for nl, bash, reasoning in pairs:
                if args.cot:
                    example = to_chatml_cot(nl, bash, reasoning)
                    try:
                        assert_cot_format(example)
                    except ValueError as e:
                        cot_errors += 1
                        continue
                else:
                    example = to_chatml(nl, bash)
                f.write(json.dumps(example) + "\n")
        if cot_errors:
            print(f"  WARNING: {cot_errors} CoT examples skipped (format check failed)")
        print(f"  {split_name}: {len(pairs) - cot_errors} examples -> {out_path}")

    print(f"\nProcessed data directory: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
