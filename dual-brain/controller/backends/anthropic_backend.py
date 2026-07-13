"""``AnthropicBackend`` — Quarantined Brain backend for Anthropic Messages.

v6.8 N.2.a (2026-07-13): transport now goes through LiteLLM
(controller/backends/_litellm_shared.py). Provider-specific quirks
(cost rates, model_id prefix) preserved in the subclass.

Model ID: ``"anthropic/<model>"`` — LiteLLM routes on this prefix.

Anthropic doesn't natively support ``response_format={"type": "json_object"}``
the same way as OpenAI, but LiteLLM emulates it via prompt shaping. The
schema-validation floor in ``BrainBackend.complete()`` remains the safety
net (INV-2-pluggable). Requires Claude Haiku 4.5 / Sonnet 4.5+ / Opus 4.5+.

INV-1 preserved via _litellm_shared's tools-exclusion + auditor probe.
"""

from __future__ import annotations

from typing import Any, Generator

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend
from ._litellm_shared import call_via_litellm, stream_via_litellm
from .registry import register_backend


@register_backend("anthropic")
class AnthropicBackend(_ApiBackend):
    """Anthropic Messages API QB backend, routed through LiteLLM."""

    __slots__ = ()

    backend_name = "anthropic"

    # Cost rates (verify at deployment).
    INPUT_COST_PER_1M_USD = 1.00
    OUTPUT_COST_PER_1M_USD = 5.00

    def _model_id(self) -> str:
        return f"anthropic/{self._config.model}"

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "anthropic backend requires a schema in the envelope"
            )
        return call_via_litellm(
            model=self._model_id(),
            envelope=envelope,
            max_tokens=self._config.max_tokens,
            timeout_seconds=self._config.timeout_seconds,
            api_key=self._api_key_ref.reveal(),
            response_format={"type": "json_object"},
            auditor=self._auditor,
        )

    def _stream_provider(
        self, envelope: RequestEnvelope
    ) -> Generator[tuple[str, int, int], None, None]:
        if envelope.schema is None:
            raise BrainProviderError(
                "anthropic backend requires a schema in the envelope"
            )
        yield from stream_via_litellm(
            model=self._model_id(),
            envelope=envelope,
            max_tokens=self._config.max_tokens,
            timeout_seconds=self._config.timeout_seconds,
            api_key=self._api_key_ref.reveal(),
            response_format={"type": "json_object"},
            auditor=self._auditor,
        )
