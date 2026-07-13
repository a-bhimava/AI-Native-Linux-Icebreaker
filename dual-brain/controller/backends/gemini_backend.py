"""``GeminiBackend`` — Quarantined Brain backend for Google Gemini.

v6.8 N.2.a (2026-07-13): transport now goes through LiteLLM
(controller/backends/_litellm_shared.py) rather than the
``google-generativeai`` SDK directly. This unifies the streaming +
retry + exception surface across gemini/anthropic/openai without
losing Gemini-specific quirks (schema massaging, cost rates, provider
prefix).

Provider-specific quirks preserved:
- Response_format uses ``{"type": "json_object"}`` for JSON-only
  output. Gemini's native ``response_schema`` mode is stronger but
  requires custom SDK usage — for v6.8 we drop to json_object mode
  which LiteLLM supports uniformly. Schema-validation floor stays in
  ``BrainBackend.complete()`` regardless (INV-2-pluggable).
- Model_id is prefixed ``"gemini/<model>"`` — LiteLLM dispatches on
  this prefix to route to the Gemini provider.
- The ``schema`` field on the envelope stays required for parity with
  the pre-LiteLLM contract; downstream validation still checks it.

INV-1 preserved: ``_litellm_shared`` never sends ``tools`` /
``tool_choice`` / ``functions``; the auditor still probes on every call.
"""

from __future__ import annotations

from typing import Any, Generator

from .base import BrainProviderError, RequestEnvelope
from ._api_common import _ApiBackend
from ._litellm_shared import call_via_litellm, stream_via_litellm
from .registry import register_backend


@register_backend("gemini")
class GeminiBackend(_ApiBackend):
    """Google Gemini QB backend, routed through LiteLLM."""

    __slots__ = ()

    backend_name = "gemini"

    # Cost rates (verify at deployment). Estimates only; audit rows carry
    # cost as an ESTIMATE, not a billing record.
    INPUT_COST_PER_1M_USD = 0.075
    OUTPUT_COST_PER_1M_USD = 0.30

    def _model_id(self) -> str:
        """Return the LiteLLM-prefixed model id for this call."""
        return f"gemini/{self._config.model}"

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        if envelope.schema is None:
            raise BrainProviderError(
                "gemini backend requires a schema in the envelope"
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
                "gemini backend requires a schema in the envelope"
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
