"""Phase 6 Scope C — provider-agnostic preset live verification.

Verifies that a preset ID resolves at the provider's metadata endpoint
(`models.retrieve` semantics — non-billable, doesn't count against the
generation quota). Design decisions grounded in the 2026-07-10 Scope C
research (see the completion plan):

  * Direct REST calls, not SDK. The `google-generativeai` /
    `google-genai` SDK dependency chain conflicts with our locked
    grpc pin; using `urllib` gives us provider-agnostic error mapping
    and version stability at the cost of ~20 LOC per provider.

  * Distinct error states — the caller (Models page) shows a red
    badge for `NotFound` (model doesn't exist at the provider), a
    yellow badge for `AuthFailed` (bad API key — user config, not
    dead model), grey for `NetworkError` (transient, don't punish),
    green for `Verified`. Never conflate — a network flake would
    otherwise poison the badge state until manual refresh.

  * Non-billable: all three providers document metadata endpoints as
    read-only informational. A 1-token `generate` would cost input
    tokens AND hit the generation rate limit. `models.retrieve` is
    the strictly-correct answer.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum


log = logging.getLogger(__name__)


# ── Result taxonomy ──────────────────────────────────────────────────────


class VerifyStatus(str, Enum):
    """Distinct badge states — see module docstring on why these are
    kept apart."""
    VERIFIED       = "verified"       # green — provider confirms the model
    NOT_FOUND      = "not_found"      # red — provider returned 404
    AUTH_FAILED    = "auth_failed"    # yellow — 401 / 403 / bad key
    NETWORK_ERROR  = "network_error"  # grey — DNS / TLS / timeout / 5xx
    UNKNOWN        = "unknown"        # unclassified — should be rare
    NO_KEY         = "no_key"         # grey — API key not configured


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of one provider metadata call."""
    provider: str
    preset_id: str
    status: VerifyStatus
    detail: str                    # short human-readable reason (tooltip)
    http_code: int | None = None   # populated when the response had one


# ── HTTP helper ──────────────────────────────────────────────────────────


_DEFAULT_TIMEOUT_SECONDS = 8.0
"""Bounded per-call. Tight enough to keep a full 12-preset sweep under
100 s in the worst case, loose enough to survive a slow TLS handshake
on the guest."""


def _get_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict | None, str | None]:
    """Perform a bounded GET. Returns `(status_code, body_json_or_none,
    error_string_or_none)`. Never raises — the caller maps to
    `VerifyStatus`."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return code, json.loads(raw), None
            except json.JSONDecodeError:
                return code, None, "malformed response body"
    except urllib.error.HTTPError as exc:
        # Non-2xx — still a valid HTTP status. Try to pull the JSON
        # error body so the tooltip can carry the provider's reason.
        code = exc.code
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            body = None
        return code, body, None
    except urllib.error.URLError as exc:
        # DNS, TLS, timeout, refused, etc.
        return 0, None, f"{type(exc).__name__}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        # F-53 pattern — surface the type so a systemic failure is
        # diagnosable in journalctl.
        return 0, None, f"{type(exc).__name__}: {exc}"


# ── Provider-specific verify ─────────────────────────────────────────────


def _verify_gemini(preset_id: str, api_key: str) -> VerifyResult:
    # Gemini REST API models.get — https://ai.google.dev/api/models
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{preset_id}?key={api_key}"
    )
    code, body, err = _get_json(url, headers={})
    return _classify("gemini", preset_id, code, body, err)


def _verify_anthropic(preset_id: str, api_key: str) -> VerifyResult:
    # Anthropic REST — https://docs.anthropic.com/en/api/models-list
    url = f"https://api.anthropic.com/v1/models/{preset_id}"
    code, body, err = _get_json(url, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    return _classify("anthropic", preset_id, code, body, err)


def _verify_openai(preset_id: str, api_key: str) -> VerifyResult:
    # OpenAI REST — https://platform.openai.com/docs/api-reference/models/retrieve
    url = f"https://api.openai.com/v1/models/{preset_id}"
    code, body, err = _get_json(url, headers={
        "Authorization": f"Bearer {api_key}",
    })
    return _classify("openai", preset_id, code, body, err)


def _classify(
    provider: str,
    preset_id: str,
    code: int | None,
    body: dict | None,
    err: str | None,
) -> VerifyResult:
    """Map (code, body, err) → VerifyResult. Kept as one path so all
    three providers share the same taxonomy — inconsistent classifica-
    tion between providers would confuse users into thinking the badge
    means different things depending on backend."""
    if err is not None:
        # Transport-level failure (DNS, TLS, timeout, connection refused).
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.NETWORK_ERROR,
            detail=err[:200],
            http_code=None,
        )
    if code is None or code == 0:
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.UNKNOWN,
            detail="no status returned",
            http_code=None,
        )
    if 200 <= code < 300:
        # Provider confirmed the model exists. body may be minimal
        # metadata; we don't rely on any field being present.
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.VERIFIED,
            detail=_extract_display(body) or "OK",
            http_code=code,
        )
    if code in (401, 403):
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.AUTH_FAILED,
            detail=_extract_error_message(body) or f"HTTP {code}",
            http_code=code,
        )
    if code == 404:
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.NOT_FOUND,
            detail=_extract_error_message(body) or "model not found",
            http_code=code,
        )
    if code == 429:
        # Rate limit — treat as network-ish so the user isn't
        # false-alarmed. Retry later.
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.NETWORK_ERROR,
            detail=_extract_error_message(body) or "rate limited (429)",
            http_code=code,
        )
    if code >= 500:
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.NETWORK_ERROR,
            detail=f"provider {code}",
            http_code=code,
        )
    # 4xx that isn't 401/403/404/429 — unusual (400 Bad Request often
    # means the model ID is malformed, e.g. contains a slash).
    return VerifyResult(
        provider=provider, preset_id=preset_id,
        status=VerifyStatus.UNKNOWN,
        detail=(_extract_error_message(body) or f"HTTP {code}"),
        http_code=code,
    )


def _extract_error_message(body: dict | None) -> str | None:
    """Best-effort pull of the provider's own error message from the
    response body. Each provider uses a slightly different shape; try
    all three so tooltips carry the real reason."""
    if not isinstance(body, dict):
        return None
    # Gemini: {"error": {"message": "...", "status": "..."}}
    if isinstance(body.get("error"), dict):
        msg = body["error"].get("message")
        if isinstance(msg, str):
            return msg[:200]
    # Anthropic: {"type": "error", "error": {"message": "..."}}
    if body.get("type") == "error" and isinstance(body.get("error"), dict):
        msg = body["error"].get("message")
        if isinstance(msg, str):
            return msg[:200]
    # OpenAI: {"error": {"message": "..."}} — same shape as Gemini
    return None


def _extract_display(body: dict | None) -> str | None:
    """Pull a human-friendly display name if the provider surfaced one.
    Used as the tooltip when verification succeeds."""
    if not isinstance(body, dict):
        return None
    for key in ("display_name", "displayName", "name", "id"):
        val = body.get(key)
        if isinstance(val, str):
            return val[:200]
    return None


# ── Public entry point ───────────────────────────────────────────────────


def verify_preset(
    provider: str,
    preset_id: str,
    api_key: str | None,
) -> VerifyResult:
    """Dispatch to the right provider verifier.

    Callers should pull the API key from the env var named by the
    catalogue's model_registry config (never from the TOML directly —
    the key is a secret, BP-8). Passing `None` returns the NO_KEY
    result without making a network call so the badge can render
    "configure a key" without waiting on a timeout.
    """
    if api_key is None or not api_key.strip():
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.NO_KEY,
            detail="no API key configured",
            http_code=None,
        )
    if provider == "gemini":
        result = _verify_gemini(preset_id, api_key)
    elif provider == "anthropic":
        result = _verify_anthropic(preset_id, api_key)
    elif provider == "openai":
        result = _verify_openai(preset_id, api_key)
    else:
        result = None
    if result is not None:
        # Phase 6 Scope D/E debug logging. Lazy import — preset_verifier
        # is called from GUI worker threads and we don't want an import
        # error here to break the sweep.
        try:
            from . import debug_log
            if debug_log.is_enabled():
                debug_log.record(
                    "preset_verify", "preset_verifier",
                    {
                        "provider": provider,
                        "preset_id": preset_id,
                        "status": result.status.value,
                        "http_code": result.http_code,
                        "detail_head": result.detail[:120],
                    },
                )
        except Exception:  # noqa: BLE001
            pass
        return result
    if provider == "local":
        # `local` presets resolve via `model_registry` (file presence +
        # checksum), not an HTTP call. The Models page shortcuts to a
        # `VERIFIED` badge when the local file is present; this fallback
        # exists so the API is uniform.
        return VerifyResult(
            provider=provider, preset_id=preset_id,
            status=VerifyStatus.UNKNOWN,
            detail="local presets are verified by model_registry, not this module",
            http_code=None,
        )
    return VerifyResult(
        provider=provider, preset_id=preset_id,
        status=VerifyStatus.UNKNOWN,
        detail=f"unknown provider {provider!r}",
        http_code=None,
    )
