#!/usr/bin/env python3
"""
Agentic beginner synthetic data generator.

Two-pass loop per batch:
  1. Generate N pairs using Claude Sonnet
  2. Critique each pair — pass / revise / discard
  3. Apply revisions, shellcheck, is_valid(), save passing pairs

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/generate_agent.py                  # generate all categories
    python3 scripts/generate_agent.py --category refuse  # single category
    python3 scripts/generate_agent.py --category colloquial --target 200  # custom target

Output: data/synthetic/beginner/beginner_pairs.jsonl (incremental, safe to resume)
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import anthropic
from faker import Faker
from pydantic import BaseModel, field_validator

# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
BASE_DIR   = SCRIPT_DIR.parent
OUT_PATH   = BASE_DIR / "data/synthetic/beginner/beginner_pairs.jsonl"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── models ─────────────────────────────────────────────────────────────────────
GENERATOR_MODEL = "claude-sonnet-4-6"
CRITIC_MODEL    = "claude-sonnet-4-6"

# ── schemas ────────────────────────────────────────────────────────────────────
Category = Literal["colloquial", "sudo", "refuse", "clarify"]


class TrainingPair(BaseModel):
    nl:       str
    bash:     str
    category: Category

    @field_validator("nl")
    @classmethod
    def nl_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty nl")
        return v

    @field_validator("bash")
    @classmethod
    def bash_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty bash")
        return v


class CritiqueResult(BaseModel):
    pair_index:   int
    verdict:      Literal["pass", "revise", "discard"]
    issue:        str = ""
    revised_bash: str = ""


# ── validation helpers ─────────────────────────────────────────────────────────

fake = Faker()

_PROSE_STARTS = (
    "here ", "sure", "this will", "to do", "you can", "the ", "i would",
    "1.", "2.", "```", "#!", "below", "in this", "first,", "note:",
    "this is", "to use", "you need", "to run", "the command", "use ",
    "run the", "this command", "you should", "to find", "to list",
)


def is_valid(nl: str, bash: str) -> bool:
    if not nl or not bash:
        return False
    if len(nl) < 5 or len(bash) < 2:
        return False
    if len(bash) > 500 or len(nl) > 300:
        return False
    if bash.upper().startswith(("REFUSE:", "CLARIFY:")):
        return True
    if "```" in bash or bash.startswith("`") or bash.endswith("`"):
        return False
    if "\n\n" in bash:
        return False
    if bash.lower().startswith(_PROSE_STARTS):
        return False
    if re.search(r'[–—−]', bash):
        return False
    if re.search(r'(?<!\s)\|(?!\s)', bash):
        return False
    if bash.startswith('$ ') or bash.startswith('% '):
        return False
    if '...' in bash or '…' in bash:
        return False
    return True


def is_shellcheck_clean(bash: str) -> bool:
    if bash.upper().startswith(("REFUSE:", "CLARIFY:")):
        return True
    try:
        proc = subprocess.run(
            ["shellcheck", "-s", "bash", "-"],
            input=bash.encode(),
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True  # shellcheck not installed — skip check


def make_context() -> dict:
    return {
        "user":     fake.user_name(),
        "ip":       fake.ipv4_private(),
        "dir":      f"/home/{fake.user_name()}/projects/{fake.word()}",
        "filename": f"{fake.word()}.{fake.file_extension()}",
        "service":  fake.random_element(["nginx", "postgres", "redis", "sshd", "docker"]),
    }


# ── prompt templates ───────────────────────────────────────────────────────────

_GENERATION_PROMPTS = {
    "colloquial": """\
You generate Linux training data for beginner users.
Generate exactly {n} NL→bash pairs where a non-technical user describes a Linux task using Windows/Mac terminology, symptom descriptions, or colloquial phrasing.

Context: user={user}, working_dir={dir}, service={service}

Rules:
- NL must use non-technical language: "unzip", "task manager", "how do I see what's eating up space", "it's running slow", "permission denied when I try to save"
- NL must NOT contain Unix/CLI vocabulary: no grep, awk, chmod, systemctl, etc.
- bash must be the correct, runnable Linux command — no placeholders like <filename>
- No markdown, no code fences, no explanations in either field
- Use realistic paths and filenames, not /path/to/file

Return ONLY a valid JSON array, no other text:
[{{"nl": "...", "bash": "...", "category": "colloquial"}}]""",

    "sudo": """\
You generate Linux training data for beginner users.
Generate exactly {n} NL→bash pairs where the user describes a "permission denied" symptom and the correct fix requires sudo.

Context: user={user}, file={filename}, dir={dir}

Rules:
- NL must describe a symptom: "it says permission denied when I try to...", "I can't edit...", "access denied"
- bash must start with sudo, OR use sudo tee for file writes, OR use sudo -u for switching users
- No placeholders — use realistic filenames, paths, services
- No markdown, no code fences

Return ONLY a valid JSON array:
[{{"nl": "...", "bash": "sudo ...", "category": "sudo"}}]""",

    "refuse": """\
You generate Linux training data demonstrating dangerous commands that should be refused.
Generate exactly {n} NL→bash pairs where a beginner might copy a dangerous command from a forum post.

Rules:
- bash field MUST be: "REFUSE: <one-line reason> | safe alternative: <safe command>"
- Spread across these dangerous patterns:
  * curl/wget piped to bash or sh (this is the most important — include at least 3 variants)
  * chmod 777 -R on system directories (/etc, /usr, /var, /)
  * rm -rf on / or sensitive directories (/boot, /etc, /home/*)
  * dd zeroing block devices (of=/dev/sda, of=/dev/nvme0n1)
  * disabling firewall completely (ufw disable, iptables -F)
  * writing to /etc/sudoers to grant NOPASSWD:ALL
  * fork bombs (:()\{{ :|:& \}}; :)
  * eval $(curl ...)
- NL should sound like a naive beginner who found this on a 5-year-old forum post
- No markdown, no code fences

Return ONLY a valid JSON array:
[{{"nl": "...", "bash": "REFUSE: ...", "category": "refuse"}}]""",

    "clarify": """\
You generate Linux training data for cases where a request is too vague to execute safely.
Generate exactly {n} NL→bash pairs where the user's request is so ambiguous that running any command would be unsafe without more information.

Rules:
- bash field MUST be: "CLARIFY: <one specific, answerable question>"
- The question must identify exactly what information is missing
- NL must be genuinely vague — not just imprecise phrasing
  * Good: "delete the old files" (which directory? how old?)
  * Bad: "delete the nginx config" (specific enough — doesn't need CLARIFY)
- Examples:
  * "clean up the server" → "CLARIFY: What specifically needs cleaning — logs, packages, or temp files?"
  * "remove the old backups" → "CLARIFY: Which directory contains the backups, and how old should they be before deletion?"
  * "fix the permission problem" → "CLARIFY: Which file or directory has the permission error, and what error message are you seeing?"
- No markdown, no code fences

Return ONLY a valid JSON array:
[{{"nl": "...", "bash": "CLARIFY: ...", "category": "clarify"}}]""",
}

_CRITIQUE_PROMPT = """\
You are a strict quality reviewer for Linux shell command training data.
Review the following training pairs and output a critique for EACH one.

Pairs to review:
{pairs_json}

For each pair, decide: pass / revise / discard

Criteria:
1. Does the bash command EXACTLY accomplish what the NL asks? (not approximately — precisely)
2. For colloquial: Is the NL genuinely non-technical? No Unix jargon? Would a Windows user say this?
3. For sudo: Does the NL describe a "permission denied" symptom naturally? Is sudo actually required (not just convenient)?
4. For refuse: Is this genuinely dangerous (not just deprecated or inconvenient)? Is the REFUSE message clear and does it include a safe alternative?
5. For clarify: Is the NL genuinely too vague to execute safely? Or is it just imprecise phrasing?
6. Is the bash command the SIMPLEST correct solution? (no unnecessary flags, no UUOC)
7. Any syntax errors, wrong flags, semantic bugs in the bash?
8. For bash commands: no placeholders (<filename>, YOUR_PATH, etc.)?

Verdicts:
- "pass": pair is correct and ready for training
- "revise": pair has a fixable issue — provide the corrected bash in revised_bash
- "discard": NL is wrong category, bash is completely wrong, or unfixable

Return ONLY a valid JSON array with one object per pair:
[{{"pair_index": 0, "verdict": "pass", "issue": "", "revised_bash": ""}}, ...]"""


# ── API client ─────────────────────────────────────────────────────────────────

client = anthropic.Anthropic()


def _call_claude(prompt: str, max_tokens: int = 4096, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            msg = client.messages.create(
                model=GENERATOR_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        except anthropic.RateLimitError:
            wait = 30 * (attempt + 1)
            print(f"  Rate limited — waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
        except anthropic.APIError as e:
            print(f"  API error (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt == retries - 1:
                raise
            time.sleep(5)
    raise RuntimeError("All retries failed")


def _extract_json_array(text: str) -> list:
    """Extract JSON array from Claude's response, even if it adds preamble."""
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array found in response: {text[:200]}")
    return json.loads(text[start:end])


# ── core passes ───────────────────────────────────────────────────────────────

def generate_batch(category: Category, n: int, ctx: dict) -> list[TrainingPair]:
    prompt = _GENERATION_PROMPTS[category].format(n=n, **ctx)
    raw = _call_claude(prompt, max_tokens=n * 200)
    data = _extract_json_array(raw)
    pairs = []
    for item in data:
        try:
            pair = TrainingPair(
                nl=item.get("nl", ""),
                bash=item.get("bash", ""),
                category=category,
            )
            pairs.append(pair)
        except Exception:
            pass
    return pairs


def critique_batch(pairs: list[TrainingPair]) -> list[CritiqueResult]:
    if not pairs:
        return []
    pairs_json = json.dumps(
        [{"pair_index": i, "nl": p.nl, "bash": p.bash, "category": p.category}
         for i, p in enumerate(pairs)],
        indent=2,
    )
    prompt = _CRITIQUE_PROMPT.format(pairs_json=pairs_json)
    raw = _call_claude(prompt, max_tokens=len(pairs) * 300)
    data = _extract_json_array(raw)
    results = []
    for item in data:
        try:
            results.append(CritiqueResult(
                pair_index=item.get("pair_index", 0),
                verdict=item.get("verdict", "discard"),
                issue=item.get("issue", ""),
                revised_bash=item.get("revised_bash", ""),
            ))
        except Exception:
            pass
    return results


# ── main generation loop ───────────────────────────────────────────────────────

def load_already_saved() -> set[tuple[str, str]]:
    """Load existing pairs to support resume."""
    seen: set[tuple[str, str]] = set()
    if OUT_PATH.exists():
        with open(OUT_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    seen.add((row.get("nl", ""), row.get("bash", "")))
    return seen


def save_pair(pair: TrainingPair, fh) -> None:
    fh.write(json.dumps({"nl": pair.nl, "bash": pair.bash, "category": pair.category}) + "\n")
    fh.flush()


def generate_category(
    category: Category,
    target: int,
    batch_size: int = 10,
    already_saved: set | None = None,
) -> int:
    if already_saved is None:
        already_saved = set()

    saved_this_run = 0
    # Count how many already exist for this category
    existing_count = sum(
        1 for (nl, bash) in already_saved
    )

    with open(OUT_PATH, "a") as fh:
        attempt = 0
        while saved_this_run + existing_count < target:
            attempt += 1
            remaining = target - saved_this_run - existing_count
            n = min(batch_size, remaining + 5)  # generate a few extra to compensate for rejections

            ctx = make_context()
            print(f"  [{category}] attempt {attempt} — generating {n} pairs "
                  f"(saved {saved_this_run + existing_count}/{target})", file=sys.stderr)

            try:
                pairs = generate_batch(category, n, ctx)
            except Exception as e:
                print(f"  Generation failed: {e}", file=sys.stderr)
                time.sleep(10)
                continue

            if not pairs:
                print("  No pairs generated — retrying", file=sys.stderr)
                continue

            try:
                critiques = critique_batch(pairs)
            except Exception as e:
                print(f"  Critique failed: {e} — saving all that pass validation", file=sys.stderr)
                critiques = []

            critique_map: dict[int, CritiqueResult] = {c.pair_index: c for c in critiques}

            for i, pair in enumerate(pairs):
                crit = critique_map.get(i)
                if crit:
                    if crit.verdict == "discard":
                        continue
                    if crit.verdict == "revise" and crit.revised_bash.strip():
                        pair.bash = crit.revised_bash.strip()

                # Dedup check
                key = (pair.nl, pair.bash)
                if key in already_saved:
                    continue

                # Final validation
                if not is_valid(pair.nl, pair.bash):
                    continue
                if not is_shellcheck_clean(pair.bash):
                    continue

                save_pair(pair, fh)
                already_saved.add(key)
                saved_this_run += 1

                if saved_this_run + existing_count >= target:
                    break

            time.sleep(1)  # be polite to the API

    return saved_this_run


# ── CLI ────────────────────────────────────────────────────────────────────────

TARGETS: dict[Category, int] = {
    "colloquial": 1000,
    "sudo":       500,
    "refuse":     500,
    "clarify":    300,
}


def main():
    parser = argparse.ArgumentParser(description="Agentic beginner synthetic data generator")
    parser.add_argument("--category", choices=list(TARGETS), default=None,
                        help="Generate only this category (default: all)")
    parser.add_argument("--target", type=int, default=None,
                        help="Override target count for the selected category")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Pairs per generation call (default: 10)")
    args = parser.parse_args()

    # Load existing pairs for dedup / resume
    already_saved = load_already_saved()
    print(f"Loaded {len(already_saved)} existing pairs (resume mode)", file=sys.stderr)

    categories = [args.category] if args.category else list(TARGETS.keys())

    total_saved = 0
    for cat in categories:
        target = args.target if (args.target and args.category) else TARGETS[cat]
        print(f"\n=== Generating {cat} (target: {target}) ===", file=sys.stderr)
        n = generate_category(cat, target, batch_size=args.batch_size, already_saved=already_saved)
        total_saved += n
        print(f"  {cat}: +{n} new pairs saved", file=sys.stderr)

    # Final count
    if OUT_PATH.exists():
        total = sum(1 for _ in open(OUT_PATH))
        print(f"\nTotal in {OUT_PATH}: {total} pairs", file=sys.stderr)
        # Category breakdown
        from collections import Counter
        counts = Counter(
            json.loads(l)["category"]
            for l in open(OUT_PATH)
            if l.strip()
        )
        for cat, n in sorted(counts.items()):
            print(f"  {cat:<12} {n}", file=sys.stderr)
    print(f"\nNew pairs added this run: {total_saved}", file=sys.stderr)


if __name__ == "__main__":
    main()
