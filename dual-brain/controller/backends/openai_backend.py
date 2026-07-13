"""``OpenAIBackend`` — Quarantined Brain backend for OpenAI Chat Completions.

v6.8 N.2.a (2026-07-13): transport now goes through LiteLLM
(controller/backends/_litellm_shared.py). Provider-specific quirks
(cost rates, model_id prefix) preserved in the subclass.

Model ID: ``"openai/<model>"`` — LiteLLM routes on this prefix.

OpenAI natively supports ``response_format={"type": "json_object"}``.
LiteLLM forwards it verbatim; no client-side massaging needed.

INV-1 preserved via _litellm_shared's tools-exclusion + auditor probe.
"""

from __future__ import annotations

from typing import Any, Generator

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend
from ._litellm_shared import call_via_litellm, stream_via_litellm
from .registry import register_backend


@register_backend("openai")
class OpenAIBackend(_ApiBackend):
    """OpenAI Chat Completions QB backend, routed through LiteLLM."""

    __slots__ = ()

    backend_name = "openai"

    # Cost rates (verify at deployment).
    INPUT_COST_PER_1M_USD = 0.40
    OUTPUT_COST_PER_1M_USD = 1.60

    def _model_id(self) -> str:
        return f"openai/{self._config.model}"

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "openai backend requires a schema in the envelope"
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
                "openai backend requires a schema in the envelope"
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
