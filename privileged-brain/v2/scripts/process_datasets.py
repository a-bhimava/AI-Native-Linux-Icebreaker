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
    "4. If a request is ambiguous or dangerous, output: REFUSE: <one-line reason>.\n"
    "5. If a request is too vague to safely execute, output: CLARIFY: <one specific question>."
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

# Maps primary tool to a brief verb phrase for deterministic reasoning generation.
_TOOL_VERBS: dict[str, str] = {
    "systemctl": "manages the systemd service lifecycle for",
    "service": "controls the init.d service for",
    "df": "reports filesystem disk space usage for",
    "du": "estimates disk space consumed by",
    "ls": "lists directory contents for",
    "find": "searches the filesystem for",
    "grep": "searches for text patterns matching",
    "ps": "reports running process status for",
    "kill": "sends a termination signal to the process for",
    "pkill": "sends a signal to processes matching",
    "chmod": "modifies file permission bits for",
    "chown": "changes file ownership for",
    "cp": "copies files or directories for",
    "mv": "moves or renames files for",
    "rm": "removes files or directories for",
    "mkdir": "creates directories for",
    "touch": "creates an empty file or updates the timestamp for",
    "cat": "concatenates and displays file contents for",
    "tail": "outputs the last lines of a file for",
    "head": "outputs the first lines of a file for",
    "sed": "performs stream-based text substitution for",
    "awk": "processes and extracts structured text for",
    "sort": "sorts lines of text for",
    "uniq": "filters duplicate adjacent lines for",
    "wc": "counts lines, words, or characters for",
    "cut": "extracts fields from delimited text for",
    "tr": "translates or deletes characters for",
    "tee": "writes stdin to both a file and stdout for",
    "xargs": "constructs and runs commands from stdin input for",
    "tar": "archives or extracts a tarball for",
    "gzip": "compresses or decompresses a file for",
    "zip": "creates a compressed zip archive for",
    "unzip": "extracts files from a zip archive for",
    "curl": "transfers data to or from a URL for",
    "wget": "downloads a file from the web for",
    "ssh": "opens a secure remote shell for",
    "scp": "securely copies files over SSH for",
    "rsync": "synchronises files between two locations for",
    "git": "performs a version control operation for",
    "docker": "manages a container or image for",
    "kubectl": "manages a Kubernetes resource for",
    "apt": "manages Debian/Ubuntu packages for",
    "apt-get": "installs or removes Debian/Ubuntu packages for",
    "yum": "manages RPM packages for",
    "dnf": "manages RPM packages for",
    "pip": "manages Python packages for",
    "pip3": "manages Python 3 packages for",
    "npm": "manages Node.js packages for",
    "python3": "runs a Python 3 script for",
    "python": "runs a Python script for",
    "bash": "runs a bash script for",
    "sh": "runs a shell script for",
    "echo": "prints a string to stdout for",
    "printf": "formats and prints output for",
    "env": "displays or sets environment variables for",
    "export": "sets an environment variable in the current shell for",
    "source": "executes a script file in the current shell for",
    "crontab": "edits or lists the user cron schedule for",
    "ping": "tests network reachability to",
    "ss": "displays socket and network connection statistics for",
    "netstat": "shows active network connections for",
    "ip": "configures a network interface for",
    "iptables": "modifies kernel packet-filter rules for",
    "ufw": "manages the uncomplicated firewall for",
    "journalctl": "queries systemd journal log entries for",
    "dmesg": "displays kernel ring buffer messages for",
    "top": "shows real-time process resource usage for",
    "free": "displays system memory usage for",
    "lsof": "lists open files and their owning processes for",
    "mount": "attaches a filesystem to the directory tree for",
    "umount": "detaches a mounted filesystem for",
    "lsblk": "lists block device information for",
    "fdisk": "partitions a block device for",
    "dd": "performs a low-level byte-for-byte data copy for",
    "usermod": "modifies an existing user account for",
    "useradd": "creates a new user account for",
    "userdel": "removes a user account for",
    "passwd": "updates the password for a user account for",
    "groups": "shows group memberships for",
    "id": "prints user and group identity information for",
    "sudo": "runs the command with elevated privileges for",
    "su": "switches to another user identity for",
    "ln": "creates a hard or symbolic link for",
    "stat": "displays detailed metadata for a file for",
    "file": "determines the type of a file for",
    "which": "locates the full executable path for",
    "diff": "compares two files line by line for",
    "patch": "applies a diff patch to a file for",
    "base64": "encodes or decodes base64 data for",
    "sha256sum": "computes a SHA-256 checksum for",
    "md5sum": "computes an MD5 checksum for",
    "openssl": "performs a cryptographic operation for",
    "nohup": "runs a command immune to terminal hangup for",
    "watch": "runs a command repeatedly at a fixed interval for",
    "timeout": "runs a command with a maximum time limit for",
    "nice": "runs a command at an adjusted scheduling priority for",
    "strace": "traces system calls made by a process for",
}


def _key_tool(bash: str) -> str:
    """Return the primary tool name, skipping common prefixes like sudo/env."""
    _skip = {"sudo", "env", "time", "nice", "nohup", "watch", "timeout", "stdbuf", "ionice"}
    for token in bash.split():
        word = re.sub(r"^.*/", "", token)   # strip path prefix (/usr/bin/df → df)
        word = word.rstrip(";,")
        if word and word[0].isalpha() and word not in _skip:
            return word
    return "command"


def _make_reasoning(nl: str, bash: str) -> str:
    """Generate a deterministic one-sentence reasoning trace for CoT training."""
    upper = bash.upper()
    if upper.startswith("REFUSE:"):
        reason = bash[7:].strip().rstrip(".")
        return f"This request is refused because {reason.lower()}."
    if upper.startswith("CLARIFY:"):
        question = bash[8:].strip().rstrip("?")
        return f"Clarification is needed before proceeding: {question.lower()}?"
    tool = _key_tool(bash)
    verb = _TOOL_VERBS.get(tool, "performs the requested operation for")
    nl_short = nl.lower().rstrip(".?!").strip()
    return f"{tool} {verb} '{nl_short}'."


def to_chatml_cot(nl: str, bash: str, reasoning: str | None = None) -> dict:
    """Build a CoT-format ChatML example: REASONING:/COMMAND: two-line assistant response."""
    if reasoning is None:
        reasoning = _make_reasoning(nl, bash)
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_COT},
            {"role": "user", "content": nl},
            {"role": "assistant", "content": f"REASONING: {reasoning}\nCOMMAND: {bash}"},
        ]
    }


def assert_cot_format(example: dict) -> None:
    """Raise ValueError if a training example doesn't conform to CoT two-line format."""
    msgs = example.get("messages", [])
    assistant = next((m for m in msgs if m["role"] == "assistant"), None)
    if not assistant:
        raise ValueError("No assistant message in example")
    content = assistant["content"]
    lines = content.strip().splitlines()
    if len(lines) < 2:
        raise ValueError(
            f"CoT requires ≥2 lines (REASONING + COMMAND), got {len(lines)}: {content!r}"
        )
    if not lines[0].startswith("REASONING:"):
        raise ValueError(f"First line must start 'REASONING:', got: {lines[0]!r}")
    if not any(ln.startswith("COMMAND:") for ln in lines):
        raise ValueError(f"No 'COMMAND:' line found in: {content!r}")


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
    # REFUSE and CLARIFY are valid structured outputs — bypass command validation
    if bash.upper().startswith("REFUSE:"):
        return True
    if bash.upper().startswith("CLARIFY:"):
        return True
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
                        help="Use only nl2bash_full + synthetic (clean baseline)")
    parser.add_argument("--cot", action="store_true",
                        help="Output CoT format (REASONING:/COMMAND:) to data/processed/cot/")
    args = parser.parse_args()

    counts = {}
    all_pairs = []

    if args.clean_only:
        base_out_dir = PROCESSED_DIR / "clean"
        print("Mode: CLEAN BASELINE (nl2bash_full + synthetic only)")
        pairs = _load_source("nl2bash_full.jsonl", "nl", "bash")
        counts["nl2bash_full.jsonl"] = len(pairs)
        all_pairs.extend(pairs)
    else:
        base_out_dir = PROCESSED_DIR
        # ── New verified datasets (downloaded by download_datasets.py) ─────────
        for fname, nl_key, bash_key in [
            ("nl2sh_alfa_train.jsonl", "nl",    "bash"),   # NL2SH-ALFA train
            ("tldr_train.jsonl",       "nl",    "bash"),   # TLDR (neulab)
            ("linux_commands.jsonl",   "nl",    "bash"),   # mecha-org/linux-command-dataset
        ]:
            pairs = _load_source(fname, nl_key, bash_key)
            counts[fname] = len(pairs)
            all_pairs.extend(pairs)

        # ── Legacy NL2Bash (10 examples, kept for continuity) ─────────────────
        pairs = _load_source("nl2bash.jsonl", "nl", "bash")
        counts["nl2bash.jsonl"] = len(pairs)
        all_pairs.extend(pairs)

    # ── Synthetic pairs (project-specific + REFUSE + beginner) — always included
    for fpath, nl_key, bash_key in [
        (Path("data/synthetic/synthetic_pairs.jsonl"),             "nl", "bash"),
        (Path("data/synthetic/synthetic_advanced.jsonl"),          "nl", "bash"),
        (Path("data/synthetic/beginner/beginner_pairs.jsonl"),     "nl", "bash"),
        (Path("data/synthetic/curated_pb.jsonl"),                  "nl", "bash"),
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

    # Determine output directory and formatter based on flags
    if args.cot:
        out_dir = base_out_dir.parent / "cot" if args.clean_only else PROCESSED_DIR / "cot"
        formatter = to_chatml_cot
        print(f"\nCoT mode ON — output format: REASONING:/COMMAND:")
    else:
        out_dir = base_out_dir
        formatter = to_chatml

    out_dir.mkdir(parents=True, exist_ok=True)
    for split_name, pairs in [("train", train_pairs), ("valid", valid_pairs)]:
        out_path = out_dir / f"{split_name}.jsonl"
        with open(out_path, "w") as f:
            for nl, bash in pairs:
                f.write(json.dumps(formatter(nl, bash)) + "\n")
        print(f"  {split_name}: {len(pairs)} examples -> {out_path}")

    # Spot-check CoT format on 50 samples to catch any malformed examples early
    if args.cot:
        train_path = out_dir / "train.jsonl"
        errors = 0
        with open(train_path) as f:
            for i, line in enumerate(f):
                if i >= 50:
                    break
                try:
                    assert_cot_format(json.loads(line))
                except ValueError as e:
                    print(f"  [CoT format ERROR on line {i+1}]: {e}")
                    errors += 1
        if errors:
            print(f"\nWARNING: {errors}/50 sampled examples failed CoT format check.")
        else:
            print("  CoT format spot-check: 50/50 OK")

    print(f"\nProcessed data directory: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
