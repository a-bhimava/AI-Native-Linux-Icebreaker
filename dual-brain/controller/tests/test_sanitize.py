"""Tests for ``backends.sanitize`` — exception scrubber + SecretRef + root filter.

Covers D18 (sanitize_exception), D21 (SecretRef), D22 (KeyRedactionFilter).
"""

from __future__ import annotations

import io
import json
import logging
import traceback

import pytest

from controller.backends import (
    BrainConfigError,
    KeyRedactionFilter,
    SecretRef,
    install_root_redaction_filter,
    sanitize_exception,
)


# ── 24-27: pattern coverage ──────────────────────────────────────────────────


def test_redacts_anthropic_key_in_message():
    exc = RuntimeError("auth failed using sk-ant-abcdefghijklmnopqrstuvwxyz")  # pragma: allowlist secret
    out = sanitize_exception(exc)
    assert "sk-ant-" not in out
    assert "[REDACTED]" in out


def test_redacts_google_api_key_in_message():
    exc = RuntimeError("invalid AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567")  # pragma: allowlist secret
    out = sanitize_exception(exc)
    assert "AIza" not in out


def test_redacts_bearer_token_case_insensitive():
    exc = RuntimeError("Authorization: bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ")  # pragma: allowlist secret
    out = sanitize_exception(exc)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in out


def test_redacts_jwt_shaped_substrings():
    exc = RuntimeError("token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.signaturepart")  # pragma: allowlist secret
    out = sanitize_exception(exc)
    assert "eyJ" not in out


# ── 28: useful context survives ──────────────────────────────────────────────


def test_sanitize_preserves_useful_context():
    exc = RuntimeError("401 Unauthorized from api.anthropic.com using sk-ant-abcdefghijklmnopqrstuvwxyz")  # pragma: allowlist secret
    out = sanitize_exception(exc)
    assert "401" in out
    assert "Unauthorized" in out
    assert "api.anthropic.com" in out


# ── 29: from None semantics — chained traceback does not leak ────────────────


def test_from_none_strips_chained_traceback():
    """Simulate what BrainBackend subclasses do at the SDK boundary."""
    secret = "sk-ant-leaks-if-traceback-not-stripped-XYZ-1234567890"  # pragma: allowlist secret

    class FakeSDKError(RuntimeError):
        pass

    def call_sdk():
        raise FakeSDKError(f"Bearer {secret} returned 401")

    class BrainProviderError(Exception): pass

    try:
        try:
            call_sdk()
        except Exception as e:
            raise BrainProviderError(sanitize_exception(e)) from None
    except BrainProviderError as outer:
        tb = "".join(traceback.format_exception(
            type(outer), outer, outer.__traceback__
        ))
        assert secret not in tb, f"secret leaked in traceback: {tb!r}"


# ── 29a-d: SecretRef ──────────────────────────────────────────────────────────


def test_secret_ref_repr_masks_value(monkeypatch):
    monkeypatch.setenv("FAKE_KEY_29A", "sk-ant-fake-fingerprint-AAA1234567890")  # pragma: allowlist secret
    s = SecretRef("FAKE_KEY_29A")
    out = repr(s)
    assert "sk-ant" not in out
    assert "********" in out
    assert "FAKE_KEY_29A" in out


def test_secret_ref_str_equals_repr(monkeypatch):
    monkeypatch.setenv("FAKE_KEY_29B", "sk-ant-different-AAA1234567890XYZ")  # pragma: allowlist secret
    s = SecretRef("FAKE_KEY_29B")
    out = f"the secret is {s}"
    assert "sk-ant" not in out
    assert "********" in out


def test_secret_ref_reveal_raises_on_missing_env(monkeypatch):
    monkeypatch.delenv("NONEXISTENT_KEY_29C", raising=False)
    s = SecretRef("NONEXISTENT_KEY_29C")
    with pytest.raises(BrainConfigError, match="NONEXISTENT_KEY_29C"):
        s.reveal()


def test_secret_ref_reveal_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("FAKE_KEY_29C2", "sk-ant-revealable-value")  # pragma: allowlist secret
    s = SecretRef("FAKE_KEY_29C2")
    assert s.reveal() == "sk-ant-revealable-value"  # pragma: allowlist secret


def test_secret_ref_not_serializable_to_json(monkeypatch):
    monkeypatch.setenv("FAKE_KEY_29D", "sk-ant-test-do-not-serialize-XYZ")  # pragma: allowlist secret
    s = SecretRef("FAKE_KEY_29D")
    with pytest.raises(TypeError):
        json.dumps(s)


def test_secret_ref_rejects_empty_env_name():
    with pytest.raises(ValueError):
        SecretRef("")


def test_secret_ref_equality():
    assert SecretRef("X") == SecretRef("X")
    assert SecretRef("X") != SecretRef("Y")
    assert SecretRef("X") != "X"


# ── 29e-g: KeyRedactionFilter ────────────────────────────────────────────────


def test_root_logger_filter_redacts_logging_error_calls():
    install_root_redaction_filter()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prior = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        logging.error("token=%s", "sk-ant-DUMMY-ABC1234567890123456789012345")  # pragma: allowlist secret
        handler.flush()
        out = buf.getvalue()
        assert "sk-ant" not in out
        assert "[REDACTED]" in out
    finally:
        root.removeHandler(handler)
        root.setLevel(prior)


def test_root_logger_filter_idempotent_install():
    install_root_redaction_filter()
    install_root_redaction_filter()
    install_root_redaction_filter()
    root = logging.getLogger()
    count = sum(1 for f in root.filters if isinstance(f, KeyRedactionFilter))
    assert count == 1


def test_root_logger_filter_handles_non_string_args():
    f = KeyRedactionFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="count=%d", args=(5,), exc_info=None,
    )
    assert f.filter(record) is True
    # No crash; args still numeric:
    assert record.args == (5,)


def test_root_logger_filter_redacts_string_in_args_tuple():
    f = KeyRedactionFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="leak=%s", args=("sk-ant-FAKE-1234567890ABCDEFGHIJK",),  # pragma: allowlist secret
        exc_info=None,
    )
    f.filter(record)
    assert "sk-ant" not in record.args[0]


def test_root_logger_filter_redacts_dict_args_values():
    install_root_redaction_filter()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prior = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        logging.info("leak=%(k)s", {"k": "sk-ant-FAKE-1234567890ABCDEFGHIJK"})  # pragma: allowlist secret
        handler.flush()
        out = buf.getvalue()
        assert "sk-ant" not in out
    finally:
        root.removeHandler(handler)
        root.setLevel(prior)
