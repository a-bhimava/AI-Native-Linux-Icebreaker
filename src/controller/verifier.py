"""
verifier.py — Quarantined Brain (QB) intent verification for Tier 2+ operations.

This implements the "lightweight LLM classifier" described in §8 (Graduated
Determinism) of the whitepaper.  The QB receives:
  1. The original Intent Object (looked up from intent_store by ref_id — QB already
     generated this object, so it already "knows" it).
  2. The generated bash command (just the command string; QB has no execution capability).

QB returns a structured verdict.  The verifier never passes raw QB output text
to the Privileged Brain — only a structured correction that becomes a revised
Intent Object through intent_store.revise().

Security invariants preserved:
  INV-1: QB has ZERO MCP connections. The command is harmless text at QB.
  INV-2: QB feedback becomes a new validated Intent Object, not raw text to PB.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import intent_store

# Quarantined Brain inference endpoint (Phi-4-mini via llama.cpp or Ollama)
QB_API_URL = "http://127.0.0.1:8080/v1/chat/completions"
QB_MODEL   = "phi4-mini"
QB_TIMEOUT = 10  # seconds

_VERDICT_SCHEMA = {
    "matches": bool,
    "issue": str,            # one of: none | wrong_target | wrong_tool | wrong_scope | other
    "correction": dict,      # subset of Intent Object fields to override; empty when matches=true
}

_VALID_ISSUES = frozenset({"none", "wrong_target", "wrong_tool", "wrong_scope", "other"})

_VERIFIER_SYSTEM = (
    "You are a system safety verifier. You will be given:\n"
    "1. An intent object: the structured goal the user expressed.\n"
    "2. A bash command: what the execution engine generated.\n\n"
    "Your task: determine whether the bash command correctly implements the intent.\n"
    "Rules:\n"
    "- Respond ONLY with valid JSON matching this exact schema — no other text:\n"
    '  {"matches": true}\n'
    "  or\n"
    '  {"matches": false, "issue": "<wrong_target|wrong_tool|wrong_scope|other>",\n'
    '   "correction": {"<field>": "<corrected value>"}}\n'
    "- The correction object must contain only fields present in the intent object.\n"
    "- Do not suggest new tools or actions not already implied by the intent.\n"
    "- If uncertain, return matches=true — do not block speculatively."
)


class IssueType(str, Enum):
    NONE         = "none"
    WRONG_TARGET = "wrong_target"
    WRONG_TOOL   = "wrong_tool"
    WRONG_SCOPE  = "wrong_scope"
    OTHER        = "other"


@dataclass
class VerifierResult:
    matches: bool
    issue: IssueType = IssueType.NONE
    correction: dict = None  # type: ignore[assignment]
    revised_ref_id: Optional[str] = None  # set if a revised Intent Object was stored

    def __post_init__(self):
        if self.correction is None:
            self.correction = {}


def _call_qb(intent: dict, command: str) -> Optional[dict]:
    """Call the QB inference API and return the raw parsed JSON verdict, or None on error."""
    user_content = (
        f"Intent object:\n{json.dumps(intent, indent=2)}\n\n"
        f"Generated bash command:\n{command}"
    )
    payload = {
        "model": QB_MODEL,
        "messages": [
            {"role": "system", "content": _VERIFIER_SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 128,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        QB_API_URL, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=QB_TIMEOUT) as resp:
            result = json.loads(resp.read())
            raw_text = result["choices"][0]["message"]["content"].strip()
            # Strip accidental markdown fences
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r"\s*```$", "", raw_text)
            return json.loads(raw_text)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError):
        return None


def _validate_verdict(raw: dict, intent: dict) -> Optional[VerifierResult]:
    """Validate the QB verdict schema; return None if malformed."""
    if not isinstance(raw, dict):
        return None
    matches = raw.get("matches")
    if not isinstance(matches, bool):
        return None

    if matches:
        return VerifierResult(matches=True)

    issue_str = raw.get("issue", "other")
    if issue_str not in _VALID_ISSUES:
        issue_str = "other"
    issue = IssueType(issue_str)

    correction = raw.get("correction", {})
    if not isinstance(correction, dict):
        correction = {}

    # Only allow corrections for fields that exist in the original intent (INV-2)
    allowed_keys = set(intent.keys())
    correction = {k: v for k, v in correction.items() if k in allowed_keys}

    # Correction values must be strings or numbers — no nested objects (shell-meta check)
    _META = re.compile(r"[;&|`$<>\x00-\x1f]")
    clean_correction = {}
    for k, v in correction.items():
        if isinstance(v, str) and _META.search(v):
            continue  # drop values containing shell metacharacters
        if isinstance(v, (str, int, float, bool)):
            clean_correction[k] = v

    return VerifierResult(matches=False, issue=issue, correction=clean_correction)


def verify(ref_id: str, command: str) -> VerifierResult:
    """Verify that a generated bash command matches the intent referenced by ref_id.

    If the QB is unreachable, returns matches=True (fail-open — the risk classifier
    and COW dry-run remain as downstream guards).

    If the QB returns matches=False with a valid correction, stores a revised Intent
    Object in intent_store and populates result.revised_ref_id.

    Args:
        ref_id:  Opaque UUID from intent_store pointing to the original Intent Object.
        command: The bash command generated by the Privileged Brain (not user input).

    Returns:
        VerifierResult with matches, issue, correction, and revised_ref_id fields.
    """
    intent = intent_store.get(ref_id)
    if intent is None:
        # Unknown or expired reference — fail-open
        return VerifierResult(matches=True)

    raw = _call_qb(intent, command)
    if raw is None:
        # QB unreachable — fail-open; downstream guards remain active
        return VerifierResult(matches=True)

    result = _validate_verdict(raw, intent)
    if result is None:
        # Malformed verdict — fail-open
        return VerifierResult(matches=True)

    if not result.matches and result.correction:
        new_ref = intent_store.revise(ref_id, result.correction)
        result.revised_ref_id = new_ref

    return result
