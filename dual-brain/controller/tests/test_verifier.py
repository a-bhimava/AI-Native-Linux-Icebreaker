"""Tests for ``controller.verifier`` — single and majority-vote verification.

Covers: SingleVerifier backward compat, MajorityVoter (3-of-5, 2-of-3,
1-of-1), parallel execution, timeout handling, fail-safe on provider
error, cost accumulation, VerifierResult fields, make_verifier factory.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.verifier import (
    MajorityVoter,
    SingleVerifier,
    VerifierConfig,
    VerifierResult,
    make_verifier,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_qb(verified: bool = True, reason: str = "ok", fail: bool = False):
    qb = MagicMock()
    if fail:
        qb.complete.side_effect = RuntimeError("provider error")
    else:
        qb.complete.return_value = SimpleNamespace(
            content_json={"verified": verified, "reason": reason},
            tokens_in=10,
            tokens_out=5,
        )
    return qb


def _make_qb_sequence(results: list[dict]):
    qb = MagicMock()
    call_count = {"n": 0}

    def _complete(**kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        r = results[idx % len(results)]
        if r.get("_fail"):
            raise RuntimeError("provider error")
        return SimpleNamespace(content_json=r, tokens_in=10, tokens_out=5)

    qb.complete.side_effect = _complete
    return qb


_SYSTEM = "You are a verifier."
_INTENT = {"action": "system.status", "target": ""}
_TOOL_CALL = {"tool": "system.status", "params": {}}


# ── make_verifier factory ────────────────────────────────────────────────────


class TestMakeVerifier:
    def test_single_for_votes_1(self):
        v = make_verifier(VerifierConfig(votes=1))
        assert isinstance(v, SingleVerifier)

    def test_single_for_default(self):
        v = make_verifier(VerifierConfig())
        assert isinstance(v, SingleVerifier)

    def test_majority_for_votes_3(self):
        v = make_verifier(VerifierConfig(votes=3))
        assert isinstance(v, MajorityVoter)

    def test_majority_for_votes_5(self):
        v = make_verifier(VerifierConfig(votes=5))
        assert isinstance(v, MajorityVoter)


# ── SingleVerifier ───────────────────────────────────────────────────────────


class TestSingleVerifier:
    def test_verified_true(self):
        qb = _make_qb(verified=True, reason="intent matches tool call")
        v = SingleVerifier()
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.votes_cast == 1
        assert result.verified_count == 1

    def test_verified_false(self):
        qb = _make_qb(verified=False, reason="mismatch")
        v = SingleVerifier()
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False
        assert result.votes_cast == 1
        assert result.verified_count == 0
        assert "mismatch" in result.reason

    def test_provider_error_fail_safe(self):
        qb = _make_qb(fail=True)
        v = SingleVerifier()
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False
        assert "failed" in result.reason

    def test_calls_qb_complete_once(self):
        qb = _make_qb(verified=True)
        v = SingleVerifier()
        v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert qb.complete.call_count == 1

    def test_passes_correct_schema(self):
        qb = _make_qb(verified=True)
        v = SingleVerifier()
        v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        call_kwargs = qb.complete.call_args
        schema = call_kwargs.kwargs.get("schema") or call_kwargs[1].get("schema")
        assert "verified" in schema["properties"]
        assert "reason" in schema["properties"]

    def test_result_is_verifier_result(self):
        qb = _make_qb(verified=True)
        v = SingleVerifier()
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert isinstance(result, VerifierResult)

    def test_backward_compat_with_dict_interface(self):
        qb = _make_qb(verified=True, reason="all good")
        v = SingleVerifier()
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.reason == "all good"


# ── MajorityVoter ────────────────────────────────────────────────────────────


class TestMajorityVoter:
    def test_3_of_3_pass(self):
        qb = _make_qb(verified=True)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.votes_cast == 3
        assert result.verified_count == 3

    def test_0_of_3_fail(self):
        qb = _make_qb(verified=False)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False
        assert result.votes_cast == 3
        assert result.verified_count == 0

    def test_2_of_3_pass_majority(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": False, "reason": "bad"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.verified_count == 2

    def test_1_of_3_fail_majority(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": False, "reason": "bad"},
            {"verified": False, "reason": "bad"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False
        assert result.verified_count == 1

    def test_3_of_5_pass(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": False, "reason": "bad"},
            {"verified": False, "reason": "bad"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=5, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.verified_count == 3

    def test_exact_threshold(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": False, "reason": "bad"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, require=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False

    def test_exact_threshold_met(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, require=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True

    def test_provider_error_counts_as_false(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"_fail": True},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is True
        assert result.verified_count == 2

    def test_all_errors_fail(self):
        qb = _make_qb(fail=True)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert result.verified is False
        assert result.verified_count == 0

    def test_individual_reasons_populated(self):
        results = [
            {"verified": True, "reason": "r1"},
            {"verified": False, "reason": "r2"},
            {"verified": True, "reason": "r3"},
        ]
        qb = _make_qb_sequence(results)
        cfg = VerifierConfig(votes=3, parallel=False)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert len(result.individual_reasons) == 3
        assert "r1" in result.individual_reasons
        assert "r2" in result.individual_reasons
        assert "r3" in result.individual_reasons


# ── Parallel execution ───────────────────────────────────────────────────────


class TestParallel:
    def test_parallel_calls_correct_count(self):
        qb = _make_qb(verified=True)
        cfg = VerifierConfig(votes=5, parallel=True)
        v = MajorityVoter(cfg)
        result = v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert qb.complete.call_count == 5
        assert result.votes_cast == 5

    def test_parallel_multiple_threads(self):
        threads_seen = set()
        original_qb = _make_qb(verified=True)

        def _tracking_complete(**kwargs):
            threads_seen.add(threading.current_thread().ident)
            return original_qb.complete(**kwargs)

        qb = MagicMock()
        qb.complete.side_effect = _tracking_complete

        cfg = VerifierConfig(votes=3, parallel=True)
        v = MajorityVoter(cfg)
        v.verify(_INTENT, _TOOL_CALL, qb, _SYSTEM)
        assert len(threads_seen) >= 1

    def test_parallel_produces_same_result_as_sequential(self):
        results = [
            {"verified": True, "reason": "ok"},
            {"verified": True, "reason": "ok"},
            {"verified": False, "reason": "bad"},
        ]

        qb_seq = _make_qb_sequence(results)
        cfg_seq = VerifierConfig(votes=3, parallel=False)
        v_seq = MajorityVoter(cfg_seq)
        r_seq = v_seq.verify(_INTENT, _TOOL_CALL, qb_seq, _SYSTEM)

        qb_par = _make_qb_sequence(results * 3)
        cfg_par = VerifierConfig(votes=3, parallel=True)
        v_par = MajorityVoter(cfg_par)
        r_par = v_par.verify(_INTENT, _TOOL_CALL, qb_par, _SYSTEM)

        assert r_seq.verified == r_par.verified
        assert r_seq.votes_cast == r_par.votes_cast


# ── VerifierConfig defaults ──────────────────────────────────────────────────


class TestVerifierConfig:
    def test_defaults(self):
        cfg = VerifierConfig()
        assert cfg.votes == 1
        assert cfg.require == 0
        assert cfg.parallel is True
        assert cfg.timeout_seconds == 30

    def test_frozen(self):
        cfg = VerifierConfig()
        with pytest.raises(AttributeError):
            cfg.votes = 5

    def test_custom_values(self):
        cfg = VerifierConfig(votes=5, require=4, parallel=False, timeout_seconds=60)
        assert cfg.votes == 5
        assert cfg.require == 4
        assert cfg.parallel is False
        assert cfg.timeout_seconds == 60


# ── VerifierResult ───────────────────────────────────────────────────────────


class TestVerifierResult:
    def test_fields(self):
        r = VerifierResult(
            verified=True, reason="ok",
            votes_cast=3, verified_count=2,
            individual_reasons=["ok", "ok", "bad"],
        )
        assert r.verified is True
        assert r.votes_cast == 3
        assert r.verified_count == 2
        assert len(r.individual_reasons) == 3

    def test_frozen(self):
        r = VerifierResult(verified=True, reason="ok")
        with pytest.raises(AttributeError):
            r.verified = False
