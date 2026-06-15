"""``OpenAIBackend`` — Quarantined Brain backend using OpenAI Chat Completions API.

Uses OpenAI's native Structured Outputs mode (``response_format`` with
``type = "json_schema"``, ``strict = True``) so the provider constrains
JSON output server-side via grammar-driven decoding. No ``tools``, no
``functions`` — so the ``OutboundPayloadAuditor`` (D17 stage 2) passes
by default.

The local ``jsonschema.Draft7Validator`` in ``BrainBackend.complete()``
remains the safety floor per INV-2-pluggable. It receives the ORIGINAL
intent schema; OpenAI receives a transformed copy with
``additionalProperties: false`` injected on every object (required by
OpenAI Structured Outputs).

OpenAI Structured Outputs natively support ``oneOf`` — no ``anyOf``
conversion needed (unlike Anthropic).
"""

from __future__ import annotations

from typing import Any, Generator

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend, transform_schema_for_provider
from .registry import register_backend


def _inject_additional_properties_false(schema: Any) -> Any:
    """Recursively inject ``additionalProperties: false`` on every object.

    OpenAI Structured Outputs require this on all object schemas. Without
    it the provider may reject the schema or produce non-conforming output.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            out[key] = _inject_additional_properties_false(value)
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_inject_additional_properties_false(item) for item in schema]
    return schema


@register_backend("openai")
class OpenAIBackend(_ApiBackend):
    """OpenAI Chat Completions API backend with native Structured Outputs."""

    __slots__ = ()

    backend_name = "openai"

    # GPT-4.1 mini pricing — verify against current OpenAI published
    # rates at deployment time. The audit row's cost field is an
    # estimate, not a billing record.
    INPUT_COST_PER_1M_USD = 0.40
    OUTPUT_COST_PER_1M_USD = 1.60

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        import openai  # lazy — D7

        self._client = openai.OpenAI(
            api_key=self._api_key_ref.reveal(),
            timeout=float(config.timeout_seconds),
        )

    def _stream_provider(
        self, envelope: RequestEnvelope
    ) -> Generator[tuple[str, int, int], None, None]:
        """Stream via OpenAI ``chat.completions.create(stream=True)``."""
        if envelope.schema is None:
            raise BrainProviderError(
                "openai backend requires a schema in the envelope"
            )

        wire_schema = _inject_additional_properties_false(
            transform_schema_for_provider(
                envelope.schema,
                convert_oneof_to_anyof=False,
                strip_format=False,
            )
        )

        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {"role": "system", "content": envelope.system},
                {"role": "user", "content": envelope.user},
            ],
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "intent",
                    "strict": True,
                    "schema": wire_schema,
                },
            },
            "stream": True,
        }

        self._auditor.intercept(kwargs)

        try:
            stream = self._client.chat.completions.create(**kwargs)
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield (chunk.choices[0].delta.content, 0, 0)
            if hasattr(chunk, "usage") and chunk.usage is not None:
                yield (
                    "",
                    chunk.usage.prompt_tokens,
                    chunk.usage.completion_tokens,
                )
            else:
                yield ("", 0, 0)
        except Exception as exc:
            from .sanitize import sanitize_exception
            raise BrainProviderError(sanitize_exception(exc)) from None

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "openai backend requires a schema in the envelope"
            )

        wire_schema = _inject_additional_properties_false(
            transform_schema_for_provider(
                envelope.schema,
                convert_oneof_to_anyof=False,
                strip_format=False,
            )
        )

        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {"role": "system", "content": envelope.system},
                {"role": "user", "content": envelope.user},
            ],
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "intent",
                    "strict": True,
                    "schema": wire_schema,
                },
            },
        }

        self._auditor.intercept(kwargs)

        response = self._wrap_sdk_call(
            self._client.chat.completions.create, **kwargs
        )

        choice = response.choices[0]
        if not choice.message.content:
            raise BrainProviderError(
                "openai response has no content"
            )

        return (
            choice.message.content,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )
