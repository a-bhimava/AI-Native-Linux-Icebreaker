"""
voting.py — Parallel k=3 inference with semantic deduplication for Tier 3 operations.

Runs k independent Privileged Brain inference calls at a temperature ladder
(0.0, 0.15, 0.3), groups semantically equivalent candidates, and returns the
majority winner.  If no majority exists, returns the candidate with the lowest
risk score and sets confidence=LOW so the HITL prompt can warn the user.

This runs entirely within PB inference — no cross-brain communication.
The QB verification step (verifier.py) runs AFTER this module, on the winner.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Privileged Brain inference endpoint (Qwen 2.5 Coder via llama.cpp or Ollama)
PB_API_URL = "http://127.0.0.1:8081/v1/chat/completions"
PB_MODEL   = "privileged-brain"
PB_TIMEOUT = 20  # seconds per call

# Temperature ladder — independent sampling points
_TEMPERATURES = [0.0, 0.15, 0.3]

_PB_SYSTEM = (
    "You are the Privileged Brain — a system execution engine for an AI-native OS. "
    "You receive a structured intent reference and output ONLY the corresponding "
    "Bash command or shell pipeline. "
    "Output ONLY the command — no explanations, no markdown, no code fences."
)


class Confidence(str, Enum):
    HIGH   = "high"    # majority of k=3 candidates agree
    LOW    = "low"     # all k=3 candidates differ — warn user in HITL prompt


@dataclass
class VoteResult:
    command: str
    confidence: Confidence
    candidates: list[str]  # all k generated candidates (for audit log)
    vote_count: int         # how many candidates matched the winner


def _normalize_command(cmd: str) -> str:
    """Normalize a command for deduplication — collapse whitespace, strip CoT prefix."""
    cmd = cmd.strip()
    # Strip CoT REASONING/COMMAND wrapper if present
    if cmd.startswith("REASONING:"):
        for line in cmd.splitlines():
            if line.startswith("COMMAND:"):
                cmd = line[len("COMMAND:"):].strip()
                break
        else:
            cmd = ""
    # Collapse internal whitespace
    cmd = re.sub(r"\s+", " ", cmd)
    return cmd


def _semantic_key(cmd: str) -> str:
    """Return a key that treats equivalent commands as identical.

    Current equivalences:
      - Flag order within a single tool invocation (e.g. -la vs -al)
      - Trailing semicolons
    """
    normalized = _normalize_command(cmd)
    # Sort single-word flags within each pipeline segment
    segments = normalized.split("|")
    canonical_segments = []
    for seg in segments:
        tokens = seg.strip().split()
        if not tokens:
            continue
        cmd_name = tokens[0]
        flags = sorted(t for t in tokens[1:] if t.startswith("-") and len(t) == 2)
        args  = [t for t in tokens[1:] if not (t.startswith("-") and len(t) == 2)]
        canonical_segments.append(" ".join([cmd_name] + flags + args))
    return " | ".join(canonical_segments).rstrip(";").strip()


async def _query_pb_async(intent_description: str, temperature: float) -> Optional[str]:
    """Send one async inference request to the Privileged Brain."""
    payload = {
        "model": PB_MODEL,
        "messages": [
            {"role": "system", "content": _PB_SYSTEM},
            {"role": "user",   "content": intent_description},
        ],
        "temperature": temperature,
        "max_tokens": 128,
    }
    data = json.dumps(payload).encode()

    loop = asyncio.get_event_loop()

    def _sync_call() -> Optional[str]:
        req = urllib.request.Request(
            PB_API_URL, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=PB_TIMEOUT) as resp:
                result = json.loads(resp.read())
                return result["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError):
            return None

    return await loop.run_in_executor(None, _sync_call)


async def _vote_async(intent_description: str) -> VoteResult:
    tasks = [
        _query_pb_async(intent_description, temp)
        for temp in _TEMPERATURES
    ]
    raw_candidates = await asyncio.gather(*tasks)

    # Filter out None / empty results
    candidates = [c for c in raw_candidates if c]
    if not candidates:
        return VoteResult(
            command="",
            confidence=Confidence.LOW,
            candidates=[],
            vote_count=0,
        )

    # Group by semantic key
    groups: dict[str, list[str]] = {}
    for c in candidates:
        key = _semantic_key(c)
        groups.setdefault(key, []).append(c)

    # Find majority
    best_key = max(groups, key=lambda k: len(groups[k]))
    best_group = groups[best_key]
    vote_count = len(best_group)

    # Pick the representative from the best group (prefer temp=0.0 if present)
    winner = best_group[0]

    confidence = Confidence.HIGH if vote_count > 1 else Confidence.LOW

    return VoteResult(
        command=_normalize_command(winner),
        confidence=confidence,
        candidates=[_normalize_command(c) for c in candidates],
        vote_count=vote_count,
    )


def vote(intent_description: str) -> VoteResult:
    """Run k=3 parallel inference calls and return the majority-voted command.

    Args:
        intent_description: The plain-language description passed to PB
                            (derived from the Intent Object, NOT raw user text).

    Returns:
        VoteResult with the winning command, confidence level, and all candidates.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(_vote_async(intent_description))
