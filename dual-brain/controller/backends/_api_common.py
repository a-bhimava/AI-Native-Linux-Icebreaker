"""Shared scaffolding for API-driven QB backends (Anthropic, Gemini).

Two primitives:

* ``_ApiBackend(BrainBackend)`` — mixin that handles API-key resolution
  via ``SecretRef`` (D21), per-backend cost-rate constants for the audit
  log, and ``_wrap_sdk_call`` which routes every SDK exception through
  ``sanitize_exception`` + ``from None`` (D18).
* ``transform_schema_for_provider(schema, ...)`` — strip JSON Schema
  keywords that Anthropic / Gemini reject from their native structured
  output grammars. The local ``jsonschema.Draft7Validator`` in
  ``base.complete()`` (``base.py:259-266``) still receives the ORIGINAL
  schema, so any constraint stripped here is still enforced — at our
  validator boundary instead of the provider's grammar.

The provider's structured-output mode is an OPTIMIZATION for first-try
success. The validator + retry loop in ``BrainBackend.complete()`` is
the INV-2-pluggable safety floor; see ``phase2_roadmap.md:221``.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from .base import BrainBackend, BrainConfigError, BrainProviderError
from .sanitize import sanitize_exception


# Keywords stripped unconditionally from the wire schema for both
# Anthropic and Gemini native structured-output modes. The local
# validator continues to enforce them.
_PROVIDER_STRIP_KEYS: Final = frozenset(
    {
        # numeric constraints
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        # string constraints
        "minLength",
        "maxLength",
        # array constraints
        "uniqueItems",
        # object constraints
        "minProperties",
        "maxProperties",
    }
)

# ``minItems``/``maxItems`` allowed only when value is 0 or 1
# (Anthropic). Strip both unconditionally — local validator covers them.
_PROVIDER_STRIP_ARRAY_BOUNDS: Final = frozenset({"minItems", "maxItems"})


def transform_schema_for_provider(
    schema: Any,
    *,
    convert_oneof_to_anyof: bool = True,
    strip_format: bool = False,
) -> Any:
    """Return a copy of ``schema`` with provider-rejected keywords stripped.

    Parameters
    ----------
    schema
        The input JSON Schema (dict). Lists are traversed; non-dict /
        non-list values are returned unchanged.
    convert_oneof_to_anyof
        Anthropic does not document ``oneOf`` in its supported list;
        both providers support ``anyOf``. For our intent schema's
        ``oneOf [string, number, boolean, null]`` value type union,
        ``anyOf`` is semantically equivalent since the branches are
        disjoint by ``type``. Default ``True``.
    strip_format
        Gemini's ``response_schema`` supports a narrower ``format`` list
        than Anthropic (no ``uuid``). Pass ``True`` for Gemini, ``False``
        for Anthropic (Anthropic supports ``uuid``, ``date-time``, etc.).

    Notes
    -----
    The local ``jsonschema.Draft7Validator`` in ``BrainBackend.complete()``
    receives the ORIGINAL schema — every constraint stripped here is
    still enforced at the validator boundary. This is exactly what
    INV-2-pluggable specifies (validator + retry IS the safety floor).
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in _PROVIDER_STRIP_KEYS:
                continue
            if key in _PROVIDER_STRIP_ARRAY_BOUNDS:
                continue
            if key == "format" and strip_format:
                continue
            if key == "oneOf" and convert_oneof_to_anyof:
                out["anyOf"] = [
                    transform_schema_for_provider(
                        item,
                        convert_oneof_to_anyof=convert_oneof_to_anyof,
                        strip_format=strip_format,
                    )
                    for item in value
                ]
                continue
            out[key] = transform_schema_for_provider(
                value,
                convert_oneof_to_anyof=convert_oneof_to_anyof,
                strip_format=strip_format,
            )
        return out
    if isinstance(schema, list):
        return [
            transform_schema_for_provider(
                item,
                convert_oneof_to_anyof=convert_oneof_to_anyof,
                strip_format=strip_format,
            )
            for item in schema
        ]
    return schema


class _ApiBackend(BrainBackend):
    """Mixin for API-driven backends.

    Subclasses must set ``backend_name``, ``INPUT_COST_PER_1M_USD``,
    ``OUTPUT_COST_PER_1M_USD``, and implement ``_call_provider``.
    """

    __slots__ = ("_client", "_api_key_ref")

    # Cost-rate constants. Subclasses override.
    INPUT_COST_PER_1M_USD: float = 0.0
    OUTPUT_COST_PER_1M_USD: float = 0.0

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        api_key = getattr(config, "api_key", None)
        if api_key is None:
            raise BrainConfigError(
                f"{self.backend_name} backend requires api_key in "
                f"BackendConfig; set api_key_env in TOML and ensure the "
                f"env var is exported"
            )
        # SecretRef — value is NEVER stored as an attribute.
        self._api_key_ref = api_key
        self._client: Any = None  # subclass lazy-initializes

    def _estimate_cost(
        self, tokens_in: int, tokens_out: int
    ) -> float | None:
        return (
            (tokens_in / 1_000_000.0) * self.INPUT_COST_PER_1M_USD
            + (tokens_out / 1_000_000.0) * self.OUTPUT_COST_PER_1M_USD
        )

    def _wrap_sdk_call(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """Call ``fn(*args, **kwargs)``; sanitize and re-raise on error.

        ``from None`` strips the original exception's ``__context__``
        (which may carry the Authorization header) — D18.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise BrainProviderError(sanitize_exception(exc)) from None
