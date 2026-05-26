#!/usr/bin/env python3
"""
Test the inference endpoint (Ollama or llama.cpp server).
Sends NL2SH queries and prints the model's output.

Usage:
  # Test Ollama (default, port 11434)
  python3 scripts/test_inference.py

  # Test llama.cpp speculative server (port 8080)
  python3 scripts/test_inference.py --port 8080

  # Test a specific model in Ollama
  python3 scripts/test_inference.py --model privileged-brain
"""

import argparse
import json
import urllib.request
from typing import Optional

TEST_QUERIES = [
    "list all running processes sorted by memory usage",
    "show disk usage for each directory in /var",
    "restart the nginx service",
    "block all inbound traffic on port 3306 except from 192.168.1.0/24",
    "find all files larger than 100MB modified in the last 7 days",
    "show the last 50 lines of the system journal",
    "create a daily cron job to run /opt/backup.sh at 2am",
    "show all open TCP connections",
    "upgrade all installed packages",
    "delete all files in /tmp older than 3 days",
]

SYSTEM_PROMPT = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive natural language descriptions of system administration tasks and output "
    "ONLY the corresponding Bash command or shell pipeline. "
    "Output ONLY the command — no explanations, no markdown, no code fences."
)


def query_ollama(prompt: str, model: str, base_url: str) -> Optional[str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("message", {}).get("content", "").strip()
    except Exception as e:
        return f"ERROR: {e}"


def query_openai_compat(prompt: str, model: str, base_url: str) -> Optional[str]:
    """For llama.cpp server which exposes OpenAI-compatible /v1/chat/completions."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"ERROR: {e}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11434, help="Server port (11434=Ollama, 8080=llama.cpp)")
    parser.add_argument("--model", default="privileged-brain", help="Model name")
    parser.add_argument("--queries", nargs="*", help="Custom queries to test")
    args = parser.parse_args()

    base_url = f"http://127.0.0.1:{args.port}"
    queries = args.queries or TEST_QUERIES

    print(f"Testing model  : {args.model}")
    print(f"Server         : {base_url}")
    print(f"Queries        : {len(queries)}")
    print("=" * 60)

    use_ollama = args.port == 11434

    for i, query in enumerate(queries, 1):
        print(f"\n[{i}/{len(queries)}] {query}")
        print("  ->", end=" ", flush=True)

        if use_ollama:
            output = query_ollama(query, args.model, base_url)
        else:
            output = query_openai_compat(query, args.model, base_url)

        print(output)

    print("\n" + "=" * 60)
    print("Test complete.")


if __name__ == "__main__":
    main()
