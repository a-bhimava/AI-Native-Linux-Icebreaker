"""M7.0.2e (v6.16) — unit tests for Controller._qb_repair.

Regression lock for the QB-consult rescue path called by PbRetryLoop.
Mocks the QB backend so tests are hermetic (no network, no llama-server).

Also serves as an INV-1 guard: the user_msg passed to qb.complete must
NEVER include raw user text — only schema-validated intent fields +
PB's own output + verifier reason (already sanitized upstream).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Import the SCHEMA constant so tests can assert it's the one being passed
# to qb.complete (not some inline dict that could drift).
from controller.main import _PB_REPAIR_SCHEMA


def _make_controller():
    """Build a stub Controller-like object exposing just the fields
    Controller._qb_repair reads: _qb, _prompts, _system_logger. Rather
    than construct a full Controller (which needs backends, mcpd,
    audit, etc.), duplicate the tiny _qb_repair body here — the test
    is asserting the ADAPTER shape, not the full Controller wiring.

    That's a legitimate pattern: _qb_repair is a pure method with
    three collaborators; the test isolates it by mocking those three.
    """
    from controller.main import Controller
    # Ct-scan best practice: don't invoke __init__ (needs sockets). Use
    # object.__new__ + manually set the collaborators the method reads.
    ctrl = object.__new__(Controller)
    ctrl._qb = MagicMock()
    ctrl._prompts = MagicMock()
    ctrl._prompts.get.return_value = "SYSTEM PROMPT (test stub)"
    ctrl._system_logger = None  # _log_exception is None-safe
    return ctrl


def _intent(**overrides) -> dict:
    base = {
        "intent_id": "test-intent-uuid",
        "action": "fs.write",
        "target": "/tmp/foo",
        "reason": "user requested",
        "risk_level": "low",
        "pb_hint": "",
    }
    base.update(overrides)
    return base


def _tool_call(**params) -> dict:
    return {"tool": "fs.write", "params": params or {"path": "/tmp/foo"}}


# ── Happy path ──────────────────────────────────────────────────────────────

def test_qb_repair_returns_hint_from_response():
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(
        content_json={"pb_hint": "Use params.content=<bytes>, not <str>."},
    )
    hint = ctrl._qb_repair(
        _intent(),
        _tool_call(),
        "content type mismatch",
        prior_attempts_count=1,
    )
    assert hint == "Use params.content=<bytes>, not <str>."
    ctrl._qb.complete.assert_called_once()


# ── 200-char cap ────────────────────────────────────────────────────────────

def test_qb_repair_caps_hint_at_200_chars():
    ctrl = _make_controller()
    long_hint = "A" * 500
    ctrl._qb.complete.return_value = SimpleNamespace(
        content_json={"pb_hint": long_hint},
    )
    hint = ctrl._qb_repair(_intent(), _tool_call(), "reason", 1)
    assert len(hint) == 200
    assert hint == "A" * 200


def test_qb_repair_handles_non_string_hint():
    """QB returns pb_hint=null or an int somehow → return "" not crash."""
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": None})
    assert ctrl._qb_repair(_intent(), _tool_call(), "r", 1) == ""

    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": 42})
    assert ctrl._qb_repair(_intent(), _tool_call(), "r", 1) == ""


def test_qb_repair_missing_hint_key_returns_empty():
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={})
    assert ctrl._qb_repair(_intent(), _tool_call(), "r", 1) == ""


# ── Exception handling ─────────────────────────────────────────────────────

def test_qb_repair_exception_returns_stub():
    ctrl = _make_controller()
    ctrl._qb.complete.side_effect = RuntimeError("Gemini timed out")
    hint = ctrl._qb_repair(_intent(), _tool_call(), "r", 1)
    assert hint == "[qb-repair-failed: RuntimeError]"


def test_qb_repair_exception_type_visible_in_stub():
    """Different exception classes surface distinct stubs — helps
    operator triage in the audit trail (BP-13)."""
    ctrl = _make_controller()
    ctrl._qb.complete.side_effect = ValueError("bad schema")
    assert ctrl._qb_repair(_intent(), _tool_call(), "r", 1) == "[qb-repair-failed: ValueError]"

    ctrl._qb.complete.side_effect = TimeoutError("slow")
    assert ctrl._qb_repair(_intent(), _tool_call(), "r", 1) == "[qb-repair-failed: TimeoutError]"


# ── qb.complete call-shape assertions ──────────────────────────────────────

def test_qb_repair_uses_pb_repair_schema():
    """Assert the exact SCHEMA constant is threaded — not some inline dict."""
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": "ok"})
    ctrl._qb_repair(_intent(), _tool_call(), "r", 1)
    call_kwargs = ctrl._qb.complete.call_args.kwargs
    assert call_kwargs["schema"] is _PB_REPAIR_SCHEMA
    assert call_kwargs["max_retries"] == 1


def test_qb_repair_pulls_system_prompt_from_prompts_loader():
    ctrl = _make_controller()
    ctrl._prompts.get.return_value = "CUSTOM COACH PROMPT"
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": "ok"})
    ctrl._qb_repair(_intent(), _tool_call(), "r", 1)
    ctrl._prompts.get.assert_called_once_with("qb_pb_repair")
    call_kwargs = ctrl._qb.complete.call_args.kwargs
    assert call_kwargs["system"] == "CUSTOM COACH PROMPT"


# ── INV-1 guard: no raw user text ──────────────────────────────────────────

def test_qb_repair_user_msg_excludes_raw_text_fields():
    """Even if the intent dict contains extra keys like `query` (from an
    OC-edition intent leak), the user_msg must include only the 5
    canonical intent fields + PB output + verifier reason. Guards
    against INV-1 breakage if the intent schema ever grows a raw-text
    field."""
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": "ok"})

    intent_with_leak = _intent(
        # These fields WOULD be raw text if the intent schema ever
        # allowed them. Assert they get filtered out.
        query="please delete my emails from bob@example.com",
        raw_user_text="delete emails",
        chat_history=["turn 1 raw text", "turn 2 raw text"],
        user_input="something private",
    )
    ctrl._qb_repair(intent_with_leak, _tool_call(), "reason", 1)

    user_msg_str = ctrl._qb.complete.call_args.kwargs["user"]
    user_msg = json.loads(user_msg_str)

    forbidden = {"query", "raw_user_text", "chat_history", "user_input"}
    leaked = forbidden & set(user_msg.keys())
    assert not leaked, f"Raw-text keys leaked into user_msg: {leaked}"


def test_qb_repair_user_msg_includes_all_expected_fields():
    """The user_msg must include exactly the 7 documented fields."""
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": "ok"})

    intent = _intent()
    tool_call = _tool_call(path="/tmp/bar")
    ctrl._qb_repair(intent, tool_call, "verifier said no", 2)

    user_msg = json.loads(ctrl._qb.complete.call_args.kwargs["user"])
    assert user_msg["action"] == "fs.write"
    assert user_msg["target"] == "/tmp/foo"
    assert user_msg["reason"] == "user requested"
    assert user_msg["risk_level"] == "low"
    assert user_msg["failed_tool_call"] == tool_call
    assert user_msg["verifier_reason"] == "verifier said no"
    assert user_msg["attempt_number"] == 2


def test_qb_repair_handles_missing_intent_fields():
    """Minimal intent (only action) → doesn't crash; missing fields → empty strings."""
    ctrl = _make_controller()
    ctrl._qb.complete.return_value = SimpleNamespace(content_json={"pb_hint": "ok"})

    minimal_intent = {"action": "fs.write"}
    ctrl._qb_repair(minimal_intent, None, "", 0)

    user_msg = json.loads(ctrl._qb.complete.call_args.kwargs["user"])
    assert user_msg["action"] == "fs.write"
    assert user_msg["target"] == ""
    assert user_msg["reason"] == ""
    assert user_msg["risk_level"] == ""
    assert user_msg["failed_tool_call"] is None
    assert user_msg["verifier_reason"] == ""
    assert user_msg["attempt_number"] == 0


# ── Prompt file shipping ───────────────────────────────────────────────────

def test_qb_repair_prompt_file_exists():
    """PKG-1 discipline: qb_pb_repair.txt must ship via package-data.
    Guards against a future setup.cfg / pyproject.toml change silently
    dropping the prompts directory from the venv."""
    # Locate the prompts directory relative to this test file.
    # controller/tests/test_qb_repair.py → controller/prompts/qb_pb_repair.txt
    test_file = Path(__file__).resolve()
    prompt_file = test_file.parent.parent / "prompts" / "qb_pb_repair.txt"
    assert prompt_file.exists(), (
        f"qb_pb_repair.txt missing at {prompt_file}. "
        "Ensure controller/prompts/ is in [tool.setuptools.package-data]."
    )
    content = prompt_file.read_text(encoding="utf-8")
    assert "pb_hint" in content, "prompt must instruct the model to emit pb_hint"
    assert len(content) > 100, "prompt too short — likely truncated"


def test_qb_repair_prompt_never_references_raw_text_verbatim():
    """Meta-check on the prompt content itself: never instruct QB to
    read raw user text. Belt-and-braces against a future prompt edit
    that accidentally leaks INV-1."""
    test_file = Path(__file__).resolve()
    prompt_file = test_file.parent.parent / "prompts" / "qb_pb_repair.txt"
    content = prompt_file.read_text(encoding="utf-8").lower()
    # Words that would indicate INV-1 leakage in the prompt.
    forbidden_phrases = ["user's message", "user message", "user input",
                         "raw text", "user chat"]
    for phrase in forbidden_phrases:
        # Allow "user chat text" if it's in the "Never reference" instruction.
        # Look for the phrase in a positive context (would tell the model
        # to READ it) — this heuristic just checks the phrase isn't there.
        # If it IS present, the prompt must contextualize it as a "don't do"
        # instruction ("Never reference user chat text").
        if phrase in content:
            # Find the surrounding context; must include a negation.
            idx = content.find(phrase)
            window = content[max(0, idx - 30):idx + len(phrase) + 5]
            assert any(neg in window for neg in ["never", "don't", "do not", "not"]), (
                f"Prompt mentions {phrase!r} without a negation context: "
                f"...{window}..."
            )
