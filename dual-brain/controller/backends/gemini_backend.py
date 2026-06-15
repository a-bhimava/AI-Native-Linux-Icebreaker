"""``GeminiBackend`` — Quarantined Brain backend using Google Gemini API.

Uses Gemini's native structured-output mode via
``generation_config.response_mime_type = "application/json"`` +
``response_schema``. The provider's OpenAPI-subset grammar constrains
JSON output server-side. No ``tools``, no ``grounding`` — the
``OutboundPayloadAuditor`` (D17 stage 2) passes by default.

The local ``jsonschema.Draft7Validator`` in ``BrainBackend.complete()``
remains the safety floor per INV-2-pluggable. It receives the ORIGINAL
intent schema; Gemini receives a transformed subset with provider-
incompatible keywords stripped (``maxLength``, ``format`` other than
the few Gemini supports, etc.).

Pinned SDK: ``google-generativeai>=0.8`` (``dual-brain/requirements.txt``).
"""

from __future__ import annotations

from typing import Any, Generator

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend, transform_schema_for_provider
from .registry import register_backend


@register_backend("gemini")
class GeminiBackend(_ApiBackend):
    """Google Gemini API backend with native ``response_schema`` structured output."""

    __slots__ = ("_genai",)

    backend_name = "gemini"

    # Placeholder pricing — verify at deployment. Gemini 2.0 Flash has a
    # generous free tier (~1500 req/day) before paid rates apply.
    INPUT_COST_PER_1M_USD = 0.075
    OUTPUT_COST_PER_1M_USD = 0.30

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        import google.generativeai as genai  # lazy — D7

        genai.configure(api_key=self._api_key_ref.reveal())
        self._genai = genai

    def _stream_provider(
        self, envelope: RequestEnvelope
    ) -> Generator[tuple[str, int, int], None, None]:
        """Stream via Gemini ``generate_content(stream=True)``."""
        if envelope.schema is None:
            raise BrainProviderError(
                "gemini backend requires a schema in the envelope"
            )

        wire_schema = transform_schema_for_provider(
            envelope.schema,
            convert_oneof_to_anyof=True,
            strip_format=True,
        )

        model = self._genai.GenerativeModel(
            self._config.model,
            system_instruction=envelope.system,
        )

        generation_config = {
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "max_output_tokens": self._config.max_tokens,
            "response_mime_type": "application/json",
            "response_schema": wire_schema,
        }

        audit_payload: dict[str, Any] = {
            "model": self._config.model,
            "system_instruction": envelope.system,
            "contents": [
                {"role": "user", "parts": [{"text": envelope.user}]}
            ],
            "generation_config": generation_config,
        }
        self._auditor.intercept(audit_payload)

        try:
            response = model.generate_content(
                envelope.user,
                generation_config=generation_config,
                request_options={"timeout": self._config.timeout_seconds},
                stream=True,
            )
            for chunk in response:
                text = getattr(chunk, "text", "") or ""
                if text:
                    yield (text, 0, 0)
            usage = response.usage_metadata
            yield (
                "",
                usage.prompt_token_count,
                usage.candidates_token_count,
            )
        except Exception as exc:
            from .sanitize import sanitize_exception
            raise BrainProviderError(sanitize_exception(exc)) from None

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "gemini backend requires a schema in the envelope"
            )

        wire_schema = transform_schema_for_provider(
            envelope.schema,
            convert_oneof_to_anyof=True,
            strip_format=True,  # Gemini's format list is narrower
        )

        model = self._genai.GenerativeModel(
            self._config.model,
            system_instruction=envelope.system,
        )

        generation_config = {
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "max_output_tokens": self._config.max_tokens,
            "response_mime_type": "application/json",
            "response_schema": wire_schema,
        }

        # Build the audit payload that MIRRORS the outbound request
        # shape. The SDK ultimately serializes the same content; the
        # auditor checks for tools/grounding/etc. in any of these fields.
        audit_payload: dict[str, Any] = {
            "model": self._config.model,
            "system_instruction": envelope.system,
            "contents": [
                {"role": "user", "parts": [{"text": envelope.user}]}
            ],
            "generation_config": generation_config,
        }
        self._auditor.intercept(audit_payload)

        response = self._wrap_sdk_call(
            model.generate_content,
            envelope.user,
            generation_config=generation_config,
            request_options={"timeout": self._config.timeout_seconds},
        )

        if not response.text:
            raise BrainProviderError("gemini response.text is empty")

        return (
            response.text,
            response.usage_metadata.prompt_token_count,
            response.usage_metadata.candidates_token_count,
        )
