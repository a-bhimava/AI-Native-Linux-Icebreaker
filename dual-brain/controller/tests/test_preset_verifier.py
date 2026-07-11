"""Phase 6 Scope C — preset_verifier regression tests.

Cover the classification path from HTTP response → `VerifyStatus`
without actually hitting the network (mock `urllib.request.urlopen`).
The point is:

  * 200        → VERIFIED
  * 401 / 403  → AUTH_FAILED (not NOT_FOUND — distinct badge state)
  * 404        → NOT_FOUND
  * 429        → NETWORK_ERROR (rate-limit, don't punish the badge)
  * 5xx        → NETWORK_ERROR
  * DNS / TLS  → NETWORK_ERROR (URLError path)
  * no key     → NO_KEY without a network call

Provider-specific message extraction is tested for each of gemini,
anthropic, openai — the error-body shapes differ.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from controller.preset_verifier import (
    VerifyResult,
    VerifyStatus,
    verify_preset,
)


class _FakeResponse:
    """Stand-in for urllib.request.urlopen's return — supports the
    `with … as resp:` protocol + `.read()` + `.status`."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self.status = status
        self._data = json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._data


def _make_http_error(code: int, body: dict) -> urllib.error.HTTPError:
    """Build an HTTPError with a readable body for the error path."""
    return urllib.error.HTTPError(
        url="https://example.com/models/whatever",
        code=code, msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


# ── No-key fast path ─────────────────────────────────────────────────────


def test_no_key_returns_immediately() -> None:
    """Passing None must NOT hit the network — otherwise verifying 12
    presets when no key is configured wastes 12 * 8 s of timeouts."""
    with patch("urllib.request.urlopen") as urlopen:
        result = verify_preset("gemini", "gemini-2.5-flash", None)
    assert result.status is VerifyStatus.NO_KEY
    assert result.http_code is None
    urlopen.assert_not_called()


def test_empty_key_treated_as_no_key() -> None:
    result = verify_preset("gemini", "gemini-2.5-flash", "   ")
    assert result.status is VerifyStatus.NO_KEY


# ── 200 / verified ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "body_key"),
    [
        ("gemini", "displayName"),
        ("anthropic", "display_name"),
        ("openai", "id"),
    ],
)
def test_200_returns_verified(provider: str, body_key: str) -> None:
    body = {body_key: "The Nice Model"}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
        result = verify_preset(provider, "some-model", "test-key")
    assert result.status is VerifyStatus.VERIFIED
    assert result.http_code == 200
    assert "The Nice Model" in result.detail or "some-model" in result.detail


# ── 404 / not found ──────────────────────────────────────────────────────


def test_gemini_404_extracts_error_message() -> None:
    """Gemini shape: {'error': {'message': '...'}}"""
    error = _make_http_error(404, {"error": {"message": "Model not found: xyz"}})
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("gemini", "gemini-nonexistent", "test-key")
    assert result.status is VerifyStatus.NOT_FOUND
    assert result.http_code == 404
    assert "Model not found" in result.detail


def test_anthropic_404_extracts_error_message() -> None:
    """Anthropic shape: {'type': 'error', 'error': {'message': '...'}}"""
    error = _make_http_error(404, {
        "type": "error", "error": {"message": "model_not_found: xyz"}
    })
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("anthropic", "claude-nonexistent", "test-key")
    assert result.status is VerifyStatus.NOT_FOUND
    assert "model_not_found" in result.detail


def test_openai_404_extracts_error_message() -> None:
    """OpenAI shape is the same as Gemini's."""
    error = _make_http_error(404, {"error": {"message": "does not exist"}})
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("openai", "gpt-nonexistent", "test-key")
    assert result.status is VerifyStatus.NOT_FOUND


# ── Auth failures ────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failed_distinct_from_not_found(code: int) -> None:
    """401/403 must NOT be reported as NOT_FOUND — otherwise a rotated
    key would look like every preset died at once."""
    error = _make_http_error(code, {"error": {"message": "bad key"}})
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("openai", "gpt-5", "wrong-key")
    assert result.status is VerifyStatus.AUTH_FAILED
    assert result.http_code == code


# ── 429 / rate limit ─────────────────────────────────────────────────────


def test_rate_limit_treated_as_network_not_dead() -> None:
    """429 is transient; the badge must be grey (network), not red."""
    error = _make_http_error(429, {"error": {"message": "quota exceeded"}})
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("gemini", "gemini-2.5-flash", "test-key")
    assert result.status is VerifyStatus.NETWORK_ERROR


# ── 5xx / provider outage ────────────────────────────────────────────────


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_treated_as_network(code: int) -> None:
    error = _make_http_error(code, {})
    with patch("urllib.request.urlopen", side_effect=error):
        result = verify_preset("anthropic", "claude-sonnet-4-6", "test-key")
    assert result.status is VerifyStatus.NETWORK_ERROR
    assert result.http_code == code


# ── Transport errors ─────────────────────────────────────────────────────


def test_urlerror_is_network_error() -> None:
    """DNS / TLS / refused / timeout all live under URLError."""
    err = urllib.error.URLError("DNS failure")
    with patch("urllib.request.urlopen", side_effect=err):
        result = verify_preset("gemini", "gemini-2.5-flash", "test-key")
    assert result.status is VerifyStatus.NETWORK_ERROR
    assert "DNS failure" in result.detail


def test_unclassified_exception_is_network_error() -> None:
    """Belt-and-braces: an unexpected exception type in the transport
    layer must still map to a safe badge, not crash the sweep."""
    class WeirdError(Exception):
        pass
    with patch("urllib.request.urlopen", side_effect=WeirdError("odd")):
        result = verify_preset("gemini", "gemini-2.5-flash", "test-key")
    assert result.status is VerifyStatus.NETWORK_ERROR
    assert "WeirdError" in result.detail


# ── Unknown provider ─────────────────────────────────────────────────────


def test_unknown_provider_returns_unknown() -> None:
    """Adding a new provider requires plumbing — until then, unknown
    dispatch returns UNKNOWN rather than crashing."""
    result = verify_preset("mystery-corp", "some-model", "test-key")
    assert result.status is VerifyStatus.UNKNOWN


def test_local_provider_returns_unknown() -> None:
    """Local presets go through model_registry, not this verifier."""
    result = verify_preset("local", "qwen-x", "test-key")
    assert result.status is VerifyStatus.UNKNOWN
