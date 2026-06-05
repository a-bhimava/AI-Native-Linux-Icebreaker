#!/usr/bin/env python3
"""
FEH eval for CoT adapters — runs inference inline (no server needed).

Usage:
  python3 scripts/eval_feh_cot.py --adapter training/adapters/run7_cot/final
  python3 scripts/eval_feh_cot.py --adapter training/adapters/run7_cot/final --no-adapter  # baseline
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(Path(__file__).parent))
from process_datasets import SYSTEM_PROMPT_COT

EVAL_PAIRS = [
    {"nl": "create a file named hello.txt with content 'world' here",             "bash": "echo 'world' > $SANDBOX/hello.txt"},
    {"nl": "create three separate directories here: logs, cache, and tmp",        "bash": "mkdir $SANDBOX/logs $SANDBOX/cache $SANDBOX/tmp"},
    {"nl": "create an empty file called log.txt in the current directory",        "bash": "touch $SANDBOX/log.txt"},
    {"nl": "write 'foo' to a file called test.txt here",                          "bash": "echo 'foo' > $SANDBOX/test.txt"},
    {"nl": "write the text '[settings]' to a file called prefs.txt here",         "bash": "echo '[settings]' > $SANDBOX/prefs.txt"},
    {"nl": "make a directory called output in the current directory",             "bash": "mkdir $SANDBOX/output"},
    {"nl": "create an empty file called CHANGES here",                            "bash": "touch $SANDBOX/CHANGES"},
    {"nl": "create a directory structure src/main here",                          "bash": "mkdir -p $SANDBOX/src/main"},
    {"nl": "list all files in the current directory",                             "bash": "ls $SANDBOX"},
    {"nl": "count lines in hello.txt in the current directory",                   "bash": "wc -l $SANDBOX/hello.txt"},
    {"nl": "copy hello.txt to backup.txt in the current directory",               "bash": "cp $SANDBOX/hello.txt $SANDBOX/backup.txt"},
    {"nl": "rename test.txt to renamed.txt in the current directory",             "bash": "mv $SANDBOX/test.txt $SANDBOX/renamed.txt"},
]


def snapshot_dir(path: str) -> dict:
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


def run_in_sandbox(command: str, pre_commands: list[str] | None = None) -> Optional[dict]:
    sandbox = tempfile.mkdtemp(prefix="priv_brain_feh_")
    try:
        # Run any setup commands first (e.g. creating hello.txt before cp test)
        if pre_commands:
            for pre in pre_commands:
                pre_cmd = pre.replace("$SANDBOX", sandbox)
                subprocess.run(pre_cmd, shell=True, cwd=sandbox, timeout=5, capture_output=True)

        cmd = command.replace("$SANDBOX", sandbox)
        if any(danger in cmd for danger in [
            "rm -rf /", "mkfs", "dd if=/dev/zero", "/boot", "/etc/passwd",
            "/etc/sudoers", "sudo", "> /dev/", "chmod 777 /",
        ]):
            return None
        result = subprocess.run(cmd, shell=True, cwd=sandbox, timeout=10, capture_output=True)
        if result.returncode != 0:
            return None
        return snapshot_dir(sandbox)
    except Exception:
        return None
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def feh_score(snap_a: Optional[dict], snap_b: Optional[dict]) -> float:
    if snap_a is None or snap_b is None:
        return 0.0
    if snap_a == snap_b:
        return 1.0
    keys_a = set(snap_a.keys())
    keys_b = set(snap_b.keys())
    if not keys_a and not keys_b:
        return 1.0
    intersection = len(keys_a & keys_b)
    union = len(keys_a | keys_b)
    return intersection / union if union > 0 else 0.0


def extract_command(response: str) -> str:
    for line in response.splitlines():
        if line.startswith("COMMAND:"):
            return line[len("COMMAND:"):].strip()
    return response.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", default="training/adapters/run7_cot/final")
    parser.add_argument("--no-adapter", action="store_true", help="Test base model (no adapter)")
    args = parser.parse_args()

    model_id = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    base_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map={"": device},
        trust_remote_code=True,
    )

    if args.no_adapter:
        print("Using base model (no adapter)")
        model = base_model
    else:
        print(f"Loading adapter: {args.adapter}")
        model = PeftModel.from_pretrained(base_model, args.adapter)
    model.eval()

    # Pre-commands for pairs that require setup files (keys must match EVAL_PAIRS nl strings exactly)
    PRE_CMDS = {
        "copy hello.txt to backup.txt in the current directory":  ["echo 'world' > $SANDBOX/hello.txt"],
        "count lines in hello.txt in the current directory":      ["echo 'world' > $SANDBOX/hello.txt"],
        "rename test.txt to renamed.txt in the current directory": ["echo 'foo' > $SANDBOX/test.txt"],
    }

    scores = []
    results = []

    print(f"\n{'='*60}")
    print(f"FEH Eval — Run 7 CoT SFT adapter")
    print(f"Pairs : {len(EVAL_PAIRS)}")
    print(f"{'='*60}\n")

    for i, pair in enumerate(EVAL_PAIRS, 1):
        nl = pair["nl"]
        ground_truth = pair["bash"]

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_COT},
            {"role": "user", "content": nl},
        ]
        prompt_text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        input_ids = tokenizer.encode(prompt_text, return_tensors="pt").to(device)

        with torch.no_grad():
            output_ids = model.generate(
                input_ids,
                max_new_tokens=160,
                do_sample=False,
                temperature=1.0,
                pad_token_id=tokenizer.eos_token_id,
            )
        new_tokens = output_ids[0][input_ids.shape[1]:]
        raw_response = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        generated_cmd = extract_command(raw_response)

        pre = PRE_CMDS.get(nl)
        snap_gt = run_in_sandbox(ground_truth, pre)

        if generated_cmd.upper().startswith("REFUSE:") or generated_cmd.upper().startswith("CLARIFY:"):
            snap_gen = None
        else:
            snap_gen = run_in_sandbox(generated_cmd, pre)

        score = feh_score(snap_gt, snap_gen)
        scores.append(score)

        status = "✓" if score == 1.0 else f"~{score:.2f}" if score > 0 else "✗"
        print(f"[{i:2d}] {status}  NL: {nl[:45]}")
        print(f"      GT : {ground_truth[:70]}")
        print(f"      GEN: {generated_cmd[:70]}")
        if score < 1.0:
            print(f"      RAW: {raw_response.replace(chr(10), ' | ')[:90]}")

        results.append({"nl": nl, "ground_truth": ground_truth, "generated": generated_cmd,
                         "raw": raw_response, "feh_score": score})

    mean_feh = sum(scores) / len(scores)
    exact = sum(1 for s in scores if s == 1.0)
    print(f"\n{'='*60}")
    print(f"FEH Score (mean)  : {mean_feh:.3f}  ({mean_feh*100:.1f}%)")
    print(f"Exact matches     : {exact}/{len(scores)}")
    print(f"Target            : ≥85%")
    print(f"G1 Result         : {'PASS ✓' if mean_feh >= 0.85 else 'FAIL ✗'}")
    print(f"{'='*60}\n")

    out_dir = Path("eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "base" if args.no_adapter else args.adapter.replace("/", "_").replace(".", "")
    out_path = out_dir / f"feh_cot_{tag}.json"
    with open(out_path, "w") as f:
        json.dump({"adapter": args.adapter, "mean_feh": mean_feh,
                   "exact_matches": exact, "total": len(scores), "results": results}, f, indent=2)
    print(f"Results saved to: {out_path}")


if __name__ == "__main__":
    main()
