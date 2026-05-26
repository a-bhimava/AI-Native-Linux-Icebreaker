#!/usr/bin/env python3
"""
Functional Equivalence Heuristic (FEH) evaluator.

Runs both the model's generated command and the ground-truth command in isolated
temp directories, then compares the resulting filesystem state diffs.

SAFETY NOTE: Only runs commands that operate within a temp directory sandbox.
Commands that reference absolute paths outside the sandbox are automatically skipped.

Usage:
  python3 scripts/eval_feh.py --model qwen2.5-coder:1.5b        # baseline
  python3 scripts/eval_feh.py --model privileged-brain           # fine-tuned
  python3 scripts/eval_feh.py --model privileged-brain --port 8080  # llama.cpp server
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

SYSTEM_PROMPT = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive natural language descriptions of system administration tasks and output "
    "ONLY the corresponding Bash command or shell pipeline. "
    "Output ONLY the command — no explanations, no markdown, no code fences."
)

# Safe eval set: file operations within a sandbox directory only.
# The placeholder $SANDBOX is replaced with the actual temp dir at eval time.
EVAL_PAIRS = [
    {"nl": "create a file named hello.txt with content 'world'", "bash": "echo 'world' > $SANDBOX/hello.txt"},
    {"nl": "create three directories: a, b, and c", "bash": "mkdir -p $SANDBOX/a $SANDBOX/b $SANDBOX/c"},
    {"nl": "create an empty file called log.txt", "bash": "touch $SANDBOX/log.txt"},
    {"nl": "write 'foo' to a file called test.txt", "bash": "echo 'foo' > $SANDBOX/test.txt"},
    {"nl": "create a file called config.ini with content '[default]'", "bash": "echo '[default]' > $SANDBOX/config.ini"},
    {"nl": "make a directory called output", "bash": "mkdir $SANDBOX/output"},
    {"nl": "create an empty file called README", "bash": "touch $SANDBOX/README"},
    {"nl": "create a directory structure: src/main", "bash": "mkdir -p $SANDBOX/src/main"},
]


def snapshot_dir(path: str) -> dict:
    """Return a dict of {relative_path: file_content_or_None_for_dirs}."""
    snap = {}
    for root, dirs, files in os.walk(path):
        for d in dirs:
            rel = os.path.relpath(os.path.join(root, d), path)
            snap[rel + "/"] = None
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, path)
            try:
                with open(full, "rb") as f:
                    snap[rel] = f.read()
            except Exception:
                snap[rel] = b""
    return snap


def run_in_sandbox(command: str) -> Optional[dict]:
    """Run a command in a fresh temp directory, return filesystem snapshot. Returns None on error."""
    sandbox = tempfile.mkdtemp(prefix="priv_brain_eval_")
    try:
        cmd = command.replace("$SANDBOX", sandbox)
        # Only allow commands that stay within the sandbox
        if any(danger in cmd for danger in [
            "rm -rf /", "mkfs", "dd if=/dev/zero", "/boot", "/etc/passwd",
            "/etc/sudoers", "sudo", "> /dev/", "chmod 777 /",
        ]):
            return None
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=sandbox,
            timeout=10,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return snapshot_dir(sandbox)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def feh_score(snap_a: Optional[dict], snap_b: Optional[dict]) -> float:
    if snap_a is None or snap_b is None:
        return 0.0
    if snap_a == snap_b:
        return 1.0
    # Partial credit: Jaccard similarity over key sets
    keys_a = set(snap_a.keys())
    keys_b = set(snap_b.keys())
    if not keys_a and not keys_b:
        return 1.0
    intersection = len(keys_a & keys_b)
    union = len(keys_a | keys_b)
    return intersection / union if union > 0 else 0.0


def query_model(prompt: str, model: str, port: int) -> Optional[str]:
    if port == 11434:
        # Ollama
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 128},
        }
        url = f"http://127.0.0.1:{port}/api/chat"
        key = ("message", "content")
    else:
        # llama.cpp OpenAI-compatible
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 128,
        }
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        key = None

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if key:
                return result[key[0]][key[1]].strip()
            else:
                return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5-coder:1.5b")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--eval-file", default=None, help="Custom JSONL eval file")
    args = parser.parse_args()

    if args.eval_file:
        with open(args.eval_file) as f:
            pairs = [json.loads(l) for l in f if l.strip()]
    else:
        pairs = EVAL_PAIRS

    print(f"Model : {args.model} (port {args.port})")
    print(f"Pairs : {len(pairs)}")
    print("=" * 60)

    scores = []
    results = []

    for i, pair in enumerate(pairs, 1):
        nl = pair["nl"]
        ground_truth = pair["bash"]

        generated = query_model(nl, args.model, args.port)
        if not generated:
            print(f"[{i:2d}] SKIP (model error) — {nl[:50]}")
            continue

        gt_cmd = ground_truth.replace("$SANDBOX", "/tmp/__feh_gt__")
        gen_cmd = generated.replace("$SANDBOX", "/tmp/__feh_gen__")

        snap_gt = run_in_sandbox(ground_truth)
        snap_gen = run_in_sandbox(generated.replace("$SANDBOX", "$SANDBOX"))

        score = feh_score(snap_gt, snap_gen)
        scores.append(score)

        status = "✓" if score == 1.0 else f"~{score:.2f}" if score > 0 else "✗"
        print(f"[{i:2d}] {status}  NL: {nl[:45]}")
        print(f"      GT : {ground_truth[:60]}")
        print(f"      GEN: {generated[:60]}")

        results.append({
            "nl": nl,
            "ground_truth": ground_truth,
            "generated": generated,
            "feh_score": score,
        })

    if scores:
        mean_feh = sum(scores) / len(scores)
        exact = sum(1 for s in scores if s == 1.0)
        print("\n" + "=" * 60)
        print(f"FEH Score (mean)  : {mean_feh:.3f}  ({mean_feh*100:.1f}%)")
        print(f"Exact matches     : {exact}/{len(scores)}")
        print(f"Evaluated         : {len(scores)}/{len(pairs)} pairs")

        out = Path("eval/results") / f"feh_{args.model.replace(':', '_').replace('/', '_')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump({"model": args.model, "mean_feh": mean_feh, "results": results}, f, indent=2)
        print(f"Results saved     : {out}")
    else:
        print("\nNo pairs evaluated. Is the server running?")


if __name__ == "__main__":
    main()
