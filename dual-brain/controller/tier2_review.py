"""Tier-2 escalate-only review (M5.2).

When a rule-based classifier assigns Tier MEDIUM, an optional second-pass
reviewer can escalate to Tier HIGH (triggering the HITL gate). The reviewer
may NEVER downgrade — escalate-only is a runtime invariant (BP-5).

Two strategies ship:
  - ``llm``  — calls the QB backend with structured fields (no raw user text,
    per INV-1). On malformed output or exception, fails safe (no escalation).
  - ``rule`` — deterministic pattern-match rules from config.

The ``get_reviewer()`` factory returns the requested strategy or ``None`` if
Tier-2 review is disabled.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .risk_classifier import ClassificationResult, Tier

log = logging.getLogger(__name__)

_TIER2_SCHEMA = {
    "type": "object",
    "properties": {
        "escalate": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 300},
    },
    "required": ["escalate", "reason"],
    "additionalProperties": False,
}

_REVIEW_SYSTEM_PROMPT = (
    "You are a risk reviewer for an AI-operated system. Given a structured "
    "intent description (action, target, tier, risk_level, reversible, params), "
    "decide whether to ESCALATE this operation to require human approval.\n\n"
    "Escalate if:\n"
    "- The operation could cause data loss or service disruption\n"
    "- The target is a system-critical path or service\n"
    "- The params suggest a broad or risky scope\n\n"
    "Do NOT escalate routine reads, user-home writes, or well-scoped operations.\n\n"
    'Output JSON only: {"escalate": true/false, "reason": "<1 sentence>"}'
)


@dataclass(frozen=True)
class Tier2Decision:
    escalate: bool
    reason: str


class Tier2Reviewer(ABC):
    @abstractmethod
    def review(self, intent: dict, cls: ClassificationResult) -> Tier2Decision:
        ...


class LlmTier2Reviewer(Tier2Reviewer):
    """Calls the QB backend with structured intent fields only (INV-1)."""

    def __init__(self, qb_backend: Any, *, max_retries: int = 2) -> None:
        self._qb = qb_backend
        self._max_retries = max_retries

    def review(self, intent: dict, cls: ClassificationResult) -> Tier2Decision:
        user_msg = json.dumps({
            "action": intent.get("action"),
            "target": intent.get("target"),
            "tier": int(cls.tier),
            "risk_level": intent.get("risk_level"),
            "reversible": cls.reversible,
            "params": intent.get("params", {}),
        }, separators=(",", ":"))

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._qb.complete(
                    system=_REVIEW_SYSTEM_PROMPT,
                    user=user_msg,
                    schema=_TIER2_SCHEMA,
                    max_retries=1,
                )
                data = resp.content_json
                if isinstance(data.get("escalate"), bool) and isinstance(data.get("reason"), str):
                    return Tier2Decision(
                        escalate=data["escalate"],
                        reason=data["reason"][:300],
                    )
                log.warning("Tier-2 reviewer returned malformed output (attempt %d): %r", attempt, data)
            except Exception:
                log.warning("Tier-2 reviewer exception (attempt %d)", attempt, exc_info=True)

        return Tier2Decision(escalate=False, reason="reviewer_error")


class RuleTier2Reviewer(Tier2Reviewer):
    """Deterministic escalation rules — no LLM call."""

    def __init__(self, *, escalate_actions: frozenset[str] | None = None) -> None:
        self._escalate_actions = escalate_actions or frozenset()

    def review(self, intent: dict, cls: ClassificationResult) -> Tier2Decision:
        action = intent.get("action", "")
        if action in self._escalate_actions:
            return Tier2Decision(
                escalate=True,
                reason=f"Rule: {action} is configured for escalation",
            )
        return Tier2Decision(escalate=False, reason="no rule matched")


def get_reviewer(
    strategy: str,
    *,
    qb_backend: Any = None,
    max_retries: int = 2,
    escalate_actions: frozenset[str] | None = None,
) -> Optional[Tier2Reviewer]:
    if strategy == "llm":
        if qb_backend is None:
            raise ValueError("LLM Tier-2 reviewer requires a qb_backend")
        return LlmTier2Reviewer(qb_backend, max_retries=max_retries)
    elif strategy == "rule":
        return RuleTier2Reviewer(escalate_actions=escalate_actions)
    elif strategy == "none":
        return None
    else:
        raise ValueError(f"Unknown Tier-2 review strategy: {strategy!r}")
