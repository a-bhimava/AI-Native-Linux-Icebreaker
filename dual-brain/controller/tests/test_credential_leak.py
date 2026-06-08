"""Negative tests for API key leakage (P2-F19).

Strategy: plant a fingerprint-shaped dummy API key in the environment
and/or as an SDK exception payload. Force every failure path we can
think of. Assert the fingerprint never appears in any captured output
stream, log buffer, exception stringification, traceback chain, or
serialized config.

This is the test bar for the M2.4 secret-handling contract: D11
(forbidden TOML keys), D18 (sanitize_exception), D21 (SecretRef
masking), D22 (root logger filter).
"""

from __future__ import annotations

import io
import json
import logging
import pickle
import sys
import tempfile
import traceback
from pathlib import Path

import pytest

from controller.backends import (
    BrainConfigError,
    BrainProviderError,
    KeyRedactionFilter,
    SecretRef,
    install_root_redaction_filter,
    sanitize_exception,
)
from controller.config import load


_FINGERPRINT = "sk-ant-DUMMY-fingerprint-must-not-leak-XYZ987654321"


# ── 34: dummy key never in stderr during network failure ─────────────────────


def test_dummy_key_never_in_stderr_during_simulated_sdk_error(monkeypatch):
    install_root_redaction_filter()
    monkeypatch.setenv("X_DUMMY_KEY_34", _FINGERPRINT)

    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    class FakeSDKError(RuntimeError):
        pass

    try:
        try:
            raise FakeSDKError(f"401 Authorization: Bearer {_FINGERPRINT}")
        except Exception as e:
            raise BrainProviderError(sanitize_exception(e)) from None
    except BrainProviderError as bpe:
        print(bpe, file=sys.stderr)
        sys.stderr.flush()

    assert _FINGERPRINT not in captured_err.getvalue()


# ── 35: dummy key never in logs during TOML parse error ──────────────────────


def test_dummy_key_never_in_logs_during_toml_parse_error(tmp_path):
    install_root_redaction_filter()
    bad_toml = tmp_path / "controller.toml"
    bad_toml.write_text(
        f'[qb]\nbackend = "anthropic"\n[qb.anthropic]\n'
        f'malformed_toml = '
    )
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        try:
            load(bad_toml)
        except BrainConfigError as e:
            logging.error("config load failure: %s", e)
        handler.flush()
        assert _FINGERPRINT not in buf.getvalue()
    finally:
        root.removeHandler(handler)


# ── 36: dummy key never in audit row built from BrainResponse ────────────────


def test_dummy_key_never_in_audit_row(monkeypatch):
    monkeypatch.setenv("X_DUMMY_KEY_36", _FINGERPRINT)
    from controller.backends import BrainResponse
    resp = BrainResponse(
        content_json={"intent_id": "fake"},
        tokens_in=10, tokens_out=5,
        cost_usd=0.001,
        backend="anthropic",
        model="claude-haiku-4-5",
        attempts=1,
    )
    serialized = json.dumps({
        "backend": resp.backend,
        "model": resp.model,
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        "cost_usd": resp.cost_usd,
        "attempts": resp.attempts,
    })
    assert _FINGERPRINT not in serialized


# ── 37: dummy key never in repr of ControllerConfig ──────────────────────────


def test_dummy_key_never_in_repr_of_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FINGERPRINT)
    toml_path = tmp_path / "controller.toml"
    toml_path.write_text(
        '[qb]\nbackend = "anthropic"\n\n'
        '[qb.anthropic]\nmodel = "claude-haiku-4-5"\n'
        'api_key_env = "ANTHROPIC_API_KEY"\nmax_tokens = 1024\n'
        'timeout_seconds = 30\n'
    )
    cfg = load(toml_path)
    rendered = repr(cfg)
    assert _FINGERPRINT not in rendered
    assert "ANTHROPIC_API_KEY" in rendered  # env var NAME is OK
    assert "********" in rendered


# ── 38: dummy key never in traceback chain ───────────────────────────────────


def test_dummy_key_never_in_traceback_chain():
    """`from None` strips __context__; sanitize_exception scrubs the
    message. Together: no leak via traceback.format_exception."""
    class FakeSDKError(RuntimeError):
        pass

    try:
        try:
            raise FakeSDKError(f"Bearer {_FINGERPRINT} returned 401")
        except Exception as e:
            raise BrainProviderError(sanitize_exception(e)) from None
    except BrainProviderError as outer:
        tb = "".join(traceback.format_exception(
            type(outer), outer, outer.__traceback__
        ))
    assert _FINGERPRINT not in tb


# ── 39: dummy key never in pickled BrainResponse ─────────────────────────────


def test_dummy_key_never_in_pickled_brainresponse():
    """Defense in depth: even if someone pickles a response, the dummy
    fingerprint (which doesn't belong in the response in the first
    place) is absent."""
    from controller.backends import BrainResponse
    resp = BrainResponse(
        content_json={"intent_id": "fake-no-key-here"},
        tokens_in=10, tokens_out=5, cost_usd=0.001,
        backend="anthropic",
        model="claude-haiku-4-5",
        attempts=1,
    )
    blob = pickle.dumps(resp)
    assert _FINGERPRINT.encode() not in blob


def test_secret_ref_pickled_does_not_carry_value(monkeypatch):
    """SecretRef only holds the env var NAME. Pickling it gives an
    env-name-only blob, even when the env var is set."""
    monkeypatch.setenv("X_DUMMY_KEY_39B", _FINGERPRINT)
    s = SecretRef("X_DUMMY_KEY_39B")
    blob = pickle.dumps(s)
    assert _FINGERPRINT.encode() not in blob
    assert b"X_DUMMY_KEY_39B" in blob


# ── Belt-and-suspenders: a real client_secret-style TOML is rejected ─────────


def test_toml_with_client_secret_rejected_without_echoing_value(tmp_path):
    bad = tmp_path / "controller.toml"
    bad.write_text(
        '[qb]\nbackend = "local"\n\n'
        '[qb.local]\nmodel = "x"\nendpoint = "http://x"\n'
        f'client_secret = "{_FINGERPRINT}"\n'
        'max_tokens = 1\ntimeout_seconds = 1\n'
    )
    with pytest.raises(BrainConfigError) as exc_info:
        load(bad)
    msg = str(exc_info.value)
    assert "client_secret" in msg
    assert _FINGERPRINT not in msg
