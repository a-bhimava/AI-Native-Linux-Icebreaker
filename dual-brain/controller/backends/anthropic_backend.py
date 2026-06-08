"""``AnthropicBackend`` — Quarantined Brain backend using Anthropic Messages API.

Uses Anthropic's native structured-output mode (``output_config.format``)
with ``type = "json_schema"`` so the provider constrains JSON output
server-side via grammar-driven decoding. No ``tools``, no ``tool_choice``,
no message prefill — so the ``OutboundPayloadAuditor`` (D17 stage 2)
passes by default and never sees a forbidden key.

The local ``jsonschema.Draft7Validator`` in ``BrainBackend.complete()``
remains the safety floor per INV-2-pluggable. It receives the ORIGINAL
intent schema (with ``maxLength``, full ``format``, etc.); Anthropic
receives a transformed subset stripped of constraints it doesn't honor.

Reference: Anthropic structured output docs (output_config.format,
type=json_schema). Requires Claude Haiku 4.5 / Sonnet 4.5+ / Opus 4.5+.
"""

from __future__ import annotations

from typing import Any

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend, transform_schema_for_provider
from .registry import register_backend


@register_backend("anthropic")
class AnthropicBackend(_ApiBackend):
    """Anthropic Messages API backend with native JSON-schema structured output."""

    __slots__ = ()

    backend_name = "anthropic"

    # Placeholder pricing — verify against current Anthropic published
    # rates at deployment time. The audit row's cost field is an
    # estimate, not a billing record.
    INPUT_COST_PER_1M_USD = 1.00
    OUTPUT_COST_PER_1M_USD = 5.00

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        import anthropic  # lazy — D7

        self._client = anthropic.Anthropic(
            api_key=self._api_key_ref.reveal(),
            timeout=float(config.timeout_seconds),
        )

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "anthropic backend requires a schema in the envelope"
            )

        wire_schema = transform_schema_for_provider(
            envelope.schema,
            convert_oneof_to_anyof=True,
            strip_format=False,  # Anthropic supports uuid, date-time, etc.
        )

        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "system": envelope.system,
            "messages": [{"role": "user", "content": envelope.user}],
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": wire_schema,
                }
            },
        }

        # D17 stage 2 — no `tools`, no `tool_choice`, no `tool_use` keys;
        # auditor passes by default.
        self._auditor.intercept(kwargs)

        response = self._wrap_sdk_call(
            self._client.messages.create, **kwargs
        )

        if not response.content:
            raise BrainProviderError(
                "anthropic response has no content blocks"
            )
        first = response.content[0]
        first_type = getattr(first, "type", None)
        if first_type != "text":
            raise BrainProviderError(
                f"anthropic response[0] is {first_type!r}, expected 'text'"
            )

        return (
            first.text,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
