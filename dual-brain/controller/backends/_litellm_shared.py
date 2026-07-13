"""v6.8 N.2.a — shared LiteLLM transport for QB backends.

Wraps `litellm.completion` / `litellm.acompletion` behind a narrow
interface used by the three provider subclasses (gemini/anthropic/openai).
The subclasses keep their own `_call_provider`/`_stream_provider` methods
(preserving stack-trace debuggability + registry integrity + per-provider
schema massaging), but the actual SDK invocation goes through here.

INV-1 (Brain Isolation): `tools` is NEVER passed. The QB has no MCP tool
connections, ever. The auditor probe in the calling subclass verifies
this on every call.

Exception matrix (from research §13.4 + §15.9):
- RateLimitError: retry with exponential backoff (up to retry_count),
  then wrap as BrainProviderError.
- Timeout / APIError / APIConnectionError / ServiceUnavailableError:
  retry once, then wrap.
- ContextWindowExceededError: raise BrainTruncationError immediately.
- ContentPolicyViolationError: raise BrainProviderError with a
  "content_policy" marker so upstream fallback_backend picks it up.
- AuthenticationError / BadRequestError / InvalidRequestError /
  NotFoundError / PermissionDeniedError: raise BrainProviderError.
  No retry — these are terminal.
- Anything else: sanitize + wrap.

`from None` is used on every re-raise to strip the original exception's
`__context__` (which may carry Authorization headers) — D18.
"""

from __future__ import annotations

import time
from typing import Any, Generator

import litellm

from .base import BrainProviderError, BrainTruncationError, RequestEnvelope
from .sanitize import sanitize_exception


# ── Exception classification ─────────────────────────────────────────────
#
# We enumerate concrete classes rather than a `try/except Exception` catch
# so a future LiteLLM addition (new exception type) doesn't silently fall
# through the retry logic and get classified as "generic terminal error".

_RETRY_WITH_BACKOFF: tuple[type[Exception], ...] = (
    litellm.RateLimitError,
)
_RETRY_ONCE: tuple[type[Exception], ...] = (
    litellm.Timeout,
    litellm.APIError,
    litellm.APIConnectionError,
    litellm.ServiceUnavailableError,
)
_TERMINAL_PROVIDER_ERROR: tuple[type[Exception], ...] = (
    litellm.AuthenticationError,
    litellm.BadRequestError,
    litellm.InvalidRequestError,
    litellm.NotFoundError,
    litellm.PermissionDeniedError,
    litellm.ContentPolicyViolationError,
)


def _wrap_provider_error(exc: Exception, marker: str = "") -> BrainProviderError:
    """Sanitize + wrap. `from None` is applied by the caller."""
    reason = sanitize_exception(exc)
    if marker:
        reason = f"{marker}: {reason}"
    return BrainProviderError(reason)


def _build_kwargs(
    *,
    model: str,
    system: str,
    user: str,
    sampling: dict,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
    response_format: dict | None,
    stream: bool,
) -> dict[str, Any]:
    """Assemble the kwargs LiteLLM's completion() accepts.

    Deliberately excludes `tools`, `tool_choice`, and `functions` —
    QB must never bind tools (INV-1). This is the single call site
    where those parameters are constructable; enforcing the exclusion
    here + subclass-side auditor probe gives defense-in-depth.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(sampling.get("temperature", 0.0)),
        "top_p": float(sampling.get("top_p", 1.0)),
        "max_tokens": int(max_tokens),
        "timeout": float(timeout_seconds),
        "api_key": api_key,
        "stream": stream,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if stream:
        # Include usage in the final streaming chunk (§15.8).
        kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def call_via_litellm(
    *,
    model: str,
    envelope: RequestEnvelope,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
    response_format: dict | None = None,
    retry_count: int = 2,
    auditor: Any = None,
) -> tuple[str, int, int]:
    """Non-streaming LiteLLM invocation.

    Returns (raw_text, prompt_tokens, completion_tokens).
    Raises BrainProviderError / BrainTruncationError.

    Never sends `tools=[...]` — the QB has no MCP connections (INV-1).
    If `auditor` is provided, its `intercept()` fires against the exact
    kwargs dict before we call `litellm.completion(**kwargs)` — that
    audit shape matches what LiteLLM will send byte-for-byte, so a
    future LiteLLM version that starts injecting tools/grounding
    is caught.
    """
    kwargs = _build_kwargs(
        model=model,
        system=envelope.system,
        user=envelope.user,
        sampling=envelope.sampling,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        response_format=response_format,
        stream=False,
    )
    if auditor is not None:
        # Redact api_key from the audit view — the auditor doesn't need it
        # and BP-8 says secrets don't land in logs/audit.
        audit_view = {k: v for k, v in kwargs.items() if k != "api_key"}
        auditor.intercept(audit_view)

    for attempt in range(retry_count + 1):
        try:
            response = litellm.completion(**kwargs)
            break
        except litellm.ContextWindowExceededError as exc:
            raise BrainTruncationError(
                sanitize_exception(exc), attempts=1, last_payload_excerpt="",
            ) from None
        except _RETRY_WITH_BACKOFF as exc:
            if attempt == retry_count:
                raise _wrap_provider_error(exc, "rate_limit_exhausted") from None
            time.sleep(0.5 * (2 ** attempt))
        except _RETRY_ONCE as exc:
            if attempt == retry_count:
                raise _wrap_provider_error(exc) from None
            time.sleep(0.25 * (2 ** attempt))
        except _TERMINAL_PROVIDER_ERROR as exc:
            marker = "content_policy" if isinstance(exc, litellm.ContentPolicyViolationError) else ""
            raise _wrap_provider_error(exc, marker) from None
        except Exception as exc:  # noqa: BLE001 — final safety net
            # Anything LiteLLM raises that isn't in the enumerated classes.
            # Wrapped + sanitized so we never leak Authorization headers.
            raise _wrap_provider_error(exc, "unclassified") from None

    # ── Parse response ────────────────────────────────────────────────
    try:
        raw_text = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise _wrap_provider_error(exc, "malformed_response") from None

    if not raw_text:
        raise BrainProviderError("empty response.choices[0].message.content")

    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    return raw_text, tokens_in, tokens_out


def stream_via_litellm(
    *,
    model: str,
    envelope: RequestEnvelope,
    max_tokens: int,
    timeout_seconds: float,
    api_key: str,
    response_format: dict | None = None,
    auditor: Any = None,
) -> Generator[tuple[str, int, int], None, None]:
    """Streaming LiteLLM invocation.

    Yields (text_chunk, prompt_tokens, completion_tokens) tuples. Interior
    chunks carry the text delta and (0, 0) token counts; the FINAL chunk
    carries an empty text string and the usage totals — matching the
    contract llama_local_backend + gemini_backend already implement.

    No streaming retry (matches existing backend behavior — a mid-stream
    error is surfaced to the caller for slow-path fallback).
    """
    kwargs = _build_kwargs(
        model=model,
        system=envelope.system,
        user=envelope.user,
        sampling=envelope.sampling,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        response_format=response_format,
        stream=True,
    )
    if auditor is not None:
        audit_view = {k: v for k, v in kwargs.items() if k != "api_key"}
        auditor.intercept(audit_view)

    try:
        stream = litellm.completion(**kwargs)
    except litellm.ContextWindowExceededError as exc:
        raise BrainTruncationError(
            sanitize_exception(exc), attempts=1, last_payload_excerpt="",
        ) from None
    except _RETRY_WITH_BACKOFF as exc:
        raise _wrap_provider_error(exc, "rate_limit") from None
    except _RETRY_ONCE + _TERMINAL_PROVIDER_ERROR as exc:
        raise _wrap_provider_error(exc) from None
    except Exception as exc:  # noqa: BLE001
        raise _wrap_provider_error(exc, "stream_setup_failed") from None

    prompt_tokens = 0
    completion_tokens = 0
    saw_content = False

    try:
        for chunk in stream:
            # Usage chunk: `choices` is empty, `usage` is populated.
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                continue
            # Content delta.
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) or ""
            if text:
                saw_content = True
                yield (text, 0, 0)
    except Exception as exc:  # noqa: BLE001
        raise _wrap_provider_error(exc, "mid_stream_error") from None

    if not saw_content:
        raise BrainProviderError("stream produced no content chunks")

    # Final tally chunk — matches gemini_backend.py:97-101 shape.
    yield ("", prompt_tokens, completion_tokens)
