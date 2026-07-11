"""QB-Verifier — configurable single or majority-vote verification.

Replaces the inline ``_qb_verify()`` in ``main.py`` with a pluggable
strategy. Default ``votes=1`` preserves current single-call behavior
(BP-2 feature-flag). When ``votes > 1``, ``MajorityVoter`` calls the
QB backend N times and requires a majority of ``verified: true``
responses to pass.

Fail-safe: any vote that raises ``BrainProviderError`` (or any
exception) counts as ``verified: false`` (BP-10).
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VerifierConfig:
    votes: int = 1
    require: int = 0
    parallel: bool = True
    timeout_seconds: int = 30
    # F-49 (2026-07-10): retry strategy when verifier rejects. The v6.61
    # UTM sweep showed legitimate writes rejected as 'verifier call failed';
    # the empirical answer for which strategy is right depends on workload.
    # Exposed as a runtime knob rather than a hard-coded string match.
    #   off                 — never retry, first vote is authoritative
    #   on_call_failed_only — retry only if reason contains "verifier call
    #                         failed" (F-44 behavior; default)
    #   on_any_rejection    — retry once on any verified=false
    #   skip_tier_01        — bypass verifier entirely for Tier 0/1
    retry_mode: str = "on_call_failed_only"


@dataclass(frozen=True)
class VerifierResult:
    verified: bool
    reason: str
    votes_cast: int = 1
    verified_count: int = 0
    individual_reasons: list[str] = field(default_factory=list)


_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "reason":   {"type": "string"},
        "concerns": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "wrong_element", "unintended_side_effect",
                    "timing_issue", "accessibility_violation",
                ],
            },
        },
        "predicted_state": {"type": "string"},
    },
    "required": ["verified", "reason"],
    "additionalProperties": False,
}


class VerifierStrategy(ABC):
    @abstractmethod
    def verify(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> VerifierResult:
        ...


class SingleVerifier(VerifierStrategy):
    """Current behavior: one call, pass/fail."""

    def verify(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> VerifierResult:
        user_msg = json.dumps(
            {"intent": intent, "tool_call": tool_call},
            separators=(",", ":"),
        )
        try:
            resp = qb.complete(
                system=verifier_system,
                user=user_msg,
                schema=_VERIFY_SCHEMA,
                max_retries=1,
            )
            result = resp.content_json
            verified = result.get("verified", False)
            reason = result.get("reason", "")
            return VerifierResult(
                verified=verified,
                reason=reason,
                votes_cast=1,
                verified_count=1 if verified else 0,
                individual_reasons=[reason],
            )
        except Exception as exc:
            # F-53 (2026-07-10): surface the actual exception in the reason
            # string so retry logic and downstream logs can distinguish
            # (a) network flake, (b) response-schema rejection, (c) empty
            # completion, (d) semantic issues. The prior blanket 'verifier
            # call failed' string was uninformative and led to false retry
            # decisions AND opaque user-facing errors ("verifier rejected:
            # verifier call failed").
            reason = f"verifier call failed: {type(exc).__name__}: {exc}"[:400]
            return VerifierResult(
                verified=False,
                reason=reason,
                votes_cast=1,
                verified_count=0,
                individual_reasons=[reason],
            )


class MajorityVoter(VerifierStrategy):
    """N calls, majority wins. Parallel via ThreadPoolExecutor."""

    def __init__(self, cfg: VerifierConfig) -> None:
        self._cfg = cfg

    def _single_vote(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> dict:
        user_msg = json.dumps(
            {"intent": intent, "tool_call": tool_call},
            separators=(",", ":"),
        )
        try:
            resp = qb.complete(
                system=verifier_system,
                user=user_msg,
                schema=_VERIFY_SCHEMA,
                max_retries=1,
            )
            return resp.content_json
        except Exception as exc:
            # F-53 v6.65: same as line 115 — surface the exception type + msg
            # so the retry-mode dispatch in main.py sees the real failure and
            # user-visible reason strings actually tell the operator why.
            reason = f"verifier call failed: {type(exc).__name__}: {exc}"[:400]
            return {"verified": False, "reason": reason}

    def verify(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> VerifierResult:
        n = self._cfg.votes

        if self._cfg.parallel and n > 1:
            results = self._run_parallel(intent, tool_call, qb, verifier_system)
        else:
            results = self._run_sequential(intent, tool_call, qb, verifier_system)

        verified_count = sum(1 for r in results if r.get("verified"))
        threshold = self._cfg.require if self._cfg.require > 0 else math.ceil(n / 2)
        reasons = [r.get("reason", "") for r in results]

        verified = verified_count >= threshold
        if verified:
            passing = [r for r in reasons if r]
            summary = passing[0] if passing else "majority verified"
        else:
            failing = [
                r.get("reason", "")
                for r in results
                if not r.get("verified")
            ]
            summary = failing[0] if failing else "majority rejected"

        return VerifierResult(
            verified=verified,
            reason=summary,
            votes_cast=n,
            verified_count=verified_count,
            individual_reasons=reasons,
        )

    def _run_parallel(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> list[dict]:
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=self._cfg.votes) as pool:
            futures = [
                pool.submit(self._single_vote, intent, tool_call, qb, verifier_system)
                for _ in range(self._cfg.votes)
            ]
            for future in as_completed(futures):
                try:
                    results.append(future.result(timeout=self._cfg.timeout_seconds))
                except Exception as exc:
                    # F-53 v6.65: surface the actual exception type + message
                    # in the vote's individual reason so users and audit logs
                    # can distinguish timeout vs API error vs schema failure.
                    reason = f"vote failed: {type(exc).__name__}: {exc}"[:400]
                    results.append({"verified": False, "reason": reason})
        return results

    def _run_sequential(
        self,
        intent: dict,
        tool_call: dict,
        qb: Any,
        verifier_system: str,
    ) -> list[dict]:
        return [
            self._single_vote(intent, tool_call, qb, verifier_system)
            for _ in range(self._cfg.votes)
        ]


def make_verifier(cfg: VerifierConfig) -> VerifierStrategy:
    """Factory: return the appropriate verifier for the given config."""
    if cfg.votes <= 1:
        return SingleVerifier()
    return MajorityVoter(cfg)
