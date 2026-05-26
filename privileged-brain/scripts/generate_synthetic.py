#!/usr/bin/env python3
"""
Generate synthetic NL→Bash pairs using Claude as teacher model.
Saves incrementally — safe to interrupt and resume.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 scripts/generate_synthetic.py --target 5000
"""

import argparse
import json
import os
import time
from pathlib import Path

import anthropic

SYNTHETIC_DIR = Path("data/synthetic")
SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = SYNTHETIC_DIR / "synthetic_pairs.jsonl"

CATEGORIES = [
    ("disk_management", "disk space, partitions, filesystem operations, du, df, mount"),
    ("service_control", "systemd services, start, stop, restart, enable, disable, status"),
    ("network_config", "network interfaces, firewall, iptables, ufw, DNS, routing, ss, netstat"),
    ("process_management", "processes, ps, kill, nice, top, htop, signals, background jobs"),
    ("package_management", "apt, dpkg, install, remove, upgrade, hold, pin, search"),
    ("log_analysis", "journalctl, syslog, grep logs, tail, follow logs, filter by date"),
    ("user_management", "adduser, passwd, sudo, groups, permissions, chown, chmod"),
    ("file_operations", "find, locate, copy, move, archive, tar, gzip, permissions"),
    ("system_monitoring", "uptime, load average, memory, CPU, iostat, vmstat, sar"),
    ("cron_scheduling", "crontab, at, scheduled tasks, recurring jobs"),
    ("ssh_security", "ssh config, keys, authorized_keys, fail2ban, port, banner"),
    ("container_basics", "docker ps, logs, exec, start, stop, inspect, volume"),
    ("adversarial_refusals", "requests that should be REFUSED: deleting system files, modifying /boot, disabling security"),
]

BATCH_PROMPT_TEMPLATE = """You are generating training data for an AI system execution model.

Category: {category}
Focus: {description}

Generate exactly {n} natural language → bash command pairs for this category.
Each pair must be realistic sysadmin task someone would actually do.

Rules:
- Commands must be safe, correct, and production-appropriate
- Prefer minimal-scope commands
- For "adversarial_refusals": the bash field must be "REFUSE: <reason>"
- No markdown, no explanations in the bash field — only the raw command

Respond with a JSON array only — no other text:
[
  {{"nl": "show disk usage for /var directory", "bash": "du -sh /var"}},
  {{"nl": "...", "bash": "..."}}
]"""


def count_existing() -> int:
    if not OUTPUT_FILE.exists():
        return 0
    with open(OUTPUT_FILE) as f:
        return sum(1 for line in f if line.strip())


def generate_batch(client: anthropic.Anthropic, category: str, description: str, n: int = 20) -> list[dict]:
    prompt = BATCH_PROMPT_TEMPLATE.format(category=category, description=description, n=n)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip any accidental markdown fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        pairs = json.loads(text)
        return [p for p in pairs if "nl" in p and "bash" in p and p["nl"] and p["bash"]]
    except Exception as e:
        print(f"    Batch failed ({category}): {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=5000, help="Total pairs to generate")
    parser.add_argument("--batch-size", type=int, default=20, help="Pairs per API call")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("       export ANTHROPIC_API_KEY=sk-ant-...")
        raise SystemExit(1)

    client = anthropic.Anthropic(api_key=api_key)
    existing = count_existing()
    print(f"Already generated: {existing} pairs")
    print(f"Target:            {args.target} pairs")

    if existing >= args.target:
        print("Target already reached. Done.")
        return

    remaining = args.target - existing

    with open(OUTPUT_FILE, "a") as out_f:
        generated = 0
        category_idx = existing // (args.target // len(CATEGORIES) + 1) % len(CATEGORIES)

        while generated < remaining:
            cat_name, cat_desc = CATEGORIES[category_idx % len(CATEGORIES)]
            category_idx += 1

            print(f"  [{existing + generated}/{args.target}] Generating {args.batch_size} pairs: {cat_name}...")
            pairs = generate_batch(client, cat_name, cat_desc, args.batch_size)

            for pair in pairs:
                out_f.write(json.dumps(pair) + "\n")
                generated += 1
                if generated >= remaining:
                    break

            out_f.flush()
            time.sleep(0.5)  # Gentle rate limiting

    total = count_existing()
    print(f"\nDone. Total synthetic pairs: {total}")
    print(f"Output: {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
