"""``LlamaCppLocalBackend`` — Quarantined Brain via local llama-server.

Talks HTTP to a llama-server instance (started separately via
``scripts/start_qb_local.sh``). The grammar (``qb_intent.gbnf``) is
attached to every request so the model is grammar-constrained at
token-sampling time — invalid output is *physically impossible*, not
just rejected post-hoc.

This backend doesn't know or care which GGUF llama-server is running.
The model selection happens upstream:

* User edits ``model_id`` in ``controller.toml``
* ``scripts/start_qb_local.sh`` reads model_id, queries the
  ``model_registry`` for the on-disk file path + sha256 verification,
  launches llama-server with the resolved file
* This backend just talks HTTP to the running server

That separation is what makes the model plug-and-play (change model_id,
restart llama-server, no Controller change).

Transport is configurable. Today: HTTP loopback. ``transport="unix"``
is reserved for Phase 6 distro packaging and raises
``BrainConfigError`` with a clear message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import (
    BrainBackend,
    BrainConfigError,
    BrainProviderError,
    RequestEnvelope,
)
from .registry import register_backend
from .sanitize import sanitize_exception


@register_backend("local")
class LlamaCppLocalBackend(BrainBackend):
    """Local llama-server backend.

    Inherits ``BrainBackend.complete()``'s retry loop, sentinel guard,
    envelope construction, and outbound auditor probe.
    """

    __slots__ = ("_endpoint", "_grammar", "_session", "_timeout")

    backend_name = "local"

    def __init__(self, config: Any) -> None:
        super().__init__(config)

        if config.transport == "unix":
            raise BrainConfigError(
                "transport='unix' for local backend is reserved for Phase 6 "
                "distro packaging. Use transport='http' against a loopback "
                "llama-server (default) for now."
            )
        if config.transport != "http":
            raise BrainConfigError(
                f"unknown transport {config.transport!r}; "
                "supported: 'http' (today), 'unix' (Phase 6)"
            )

        if config.endpoint is None:
            raise BrainConfigError(
                "local backend requires `endpoint` in config "
                "(e.g. http://127.0.0.1:8081)"
            )

        # grammar_path is optional: QB needs GBNF for constrained decoding;
        # PB relies on post-hoc tool-call validation instead.
        if config.grammar_path is not None:
            grammar_path = Path(config.grammar_path).expanduser()
            if not grammar_path.is_file():
                raise BrainConfigError(
                    f"grammar file not found: {grammar_path}"
                )
            self._grammar: str | None = grammar_path.read_text(encoding="utf-8")
        else:
            self._grammar = None

        self._endpoint = str(config.endpoint).rstrip("/")
        self._timeout = config.timeout_seconds

        # Lazy import — requests is in requirements.txt for M2.5.
        import requests

        self._session = requests.Session()

        # Health probe at construction: fail fast if llama-server isn't up.
        # Wrapped in BrainConfigError, not BrainProviderError — the local
        # backend has no API key to leak, but more importantly this is a
        # setup-time problem and should be addressed by re-running
        # start_qb_local.sh, not by retrying.
        try:
            response = self._session.get(
                f"{self._endpoint}/health",
                timeout=5,
            )
            response.raise_for_status()
        except Exception as exc:
            raise BrainConfigError(
                f"llama-server health probe failed at {self._endpoint}/health: "
                f"{type(exc).__name__}: {exc}. "
                "Did you start it via scripts/start_qb_local.sh?"
            ) from None

    def _estimate_cost(
        self, tokens_in: int, tokens_out: int
    ) -> float | None:
        # Local inference: no monetary cost. Hardware/electricity is out
        # of scope for the audit row.
        return None

    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        """POST to llama-server /v1/chat/completions with the QB grammar attached."""
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": envelope.system},
                {"role": "user", "content": envelope.user},
            ],
            "temperature": envelope.sampling["temperature"],
            "top_p": envelope.sampling["top_p"],
            "max_tokens": self._config.max_tokens,
            "cache_prompt": True,
            "stream": False,
        }
        # GBNF passed inline when present — llama-server applies it at
        # sampling time.  PB omits grammar and relies on post-hoc validation.
        if self._grammar is not None:
            payload["grammar"] = self._grammar

        # D17 stage 2 — auditor scans the outbound payload for forbidden
        # keys (tools / grounding / etc.). The local backend never adds
        # any of those; auditor passes by default.
        self._auditor.intercept(payload)

        try:
            response = self._session.post(
                f"{self._endpoint}/v1/chat/completions",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            # D18 — sanitize the exception text. Local has no API key
            # to leak today, but the sanitize pipeline is uniform across
            # all backends, so any future-added auth still gets scrubbed.
            raise BrainProviderError(sanitize_exception(exc)) from None

        body = response.json()
        if "choices" not in body or not body["choices"]:
            raise BrainProviderError(
                "llama-server response has no choices"
            )
        content = body["choices"][0].get("message", {}).get("content", "")
        if not content:
            raise BrainProviderError(
                "llama-server response.choices[0].message.content is empty"
            )

        usage = body.get("usage", {})
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        return content, tokens_in, tokens_out
