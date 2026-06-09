"""``BrainBackend`` abstract base class — the safety floor for QB backends.

Mechanically enforces three invariants regardless of subclass behavior:

* **INV-1 / G3 (zero MCP attachment)** — three-layer defense:
  (1) sentinel-guarded ``mcpd_client`` constructor kwarg;
  (2) ``RequestEnvelope.tools_disabled=True`` injected by base, not
  overridable by subclass; (3) ``OutboundPayloadAuditor`` runtime scan
  with a call-count probe that catches subclasses that skip the
  auditor.
* **INV-2-pluggable (retry-on-malformed-schema)** — ``complete()``
  owns ``json.loads`` + ``jsonschema.Draft7Validator`` + bounded retry
  with sampling decay. ``complete()`` is non-overridable
  (``__init_subclass__`` enforcement).
* **P2-F19 (no API key leakage)** — subclass contract requires SDK
  errors to be wrapped via ``sanitize_exception`` + ``from None``.

See ``dual-brain/docs/phase2/phase2_roadmap.md:132`` for the M2.4
milestone definition and ``dual-brain/docs/phase2/phase2_roadmap.md:218-244``
for the cross-cutting design decisions this module realizes.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Final

import jsonschema

from .auditor import OutboundPayloadAuditor


_GUARD_SENTINEL: Final = object()
MAX_RETRY_HARD_CAP: Final[int] = 5

_SAMPLING_DECAY: Final = (
    {"temperature": 0.4, "top_p": 0.9},
    {"temperature": 0.1, "top_p": 0.7},
    {"temperature": 0.0, "top_p": 1.0},
    {"temperature": 0.0, "top_p": 1.0},
    {"temperature": 0.0, "top_p": 1.0},
)


class BrainError(Exception):
    """Base of the BrainBackend exception hierarchy."""


class BrainSecurityError(BrainError):
    """G3 / INV-1 violation. Never caught and retried — surfaced immediately."""


class BrainConfigError(BrainError):
    """Bad TOML, missing env var, retry cap exceeded, unknown backend name."""


class BrainProviderError(BrainError):
    """Transport/auth/5xx from the underlying provider. Never retried.

    The message MUST be the output of ``sanitize.sanitize_exception``.
    Concrete backends are responsible for wrapping SDK exceptions.
    """


class BrainSchemaError(BrainError):
    """Retry loop exhausted on a schema validation failure."""

    def __init__(
        self,
        last_error: str,
        attempts: int,
        last_payload_excerpt: str,
    ) -> None:
        super().__init__(last_error)
        self.last_error = last_error
        self.attempts = attempts
        self.last_payload_excerpt = last_payload_excerpt


class BrainTruncationError(BrainSchemaError):
    """Retry loop exhausted on ``json.JSONDecodeError`` specifically.

    Distinct from ``BrainSchemaError`` so the orchestrator can surface
    different remediation (raise max_tokens) for the P2-F14 case.
    """


@dataclass(frozen=True)
class BrainResponse:
    """Successful ``complete()`` return value.

    Field shape mirrors ``controller/audit.py`` ``REQUIRED_FIELDS``
    (lines 97-115) so the orchestrator can map this directly into an
    audit row.
    """

    content_json: dict
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    backend: str
    model: str
    attempts: int


@dataclass(frozen=True)
class RequestEnvelope:
    """Generalized request the base assembles per attempt.

    ``tools_disabled`` is always ``True``; the base class constructs the
    envelope, the subclass cannot. A subclass's ``_call_provider`` must
    translate the envelope into provider-specific payload and honor
    ``tools_disabled`` by setting the SDK's tools/functions/grounding
    field to its "off" value. The post-translation payload is then
    audited by ``OutboundPayloadAuditor``.
    """

    system: str
    user: str
    schema: dict | None
    sampling: dict
    tools_disabled: bool = True


class BrainBackend(ABC):
    """Quarantined Brain backend ABC.

    Subclasses implement ``_call_provider(envelope)`` only. The
    ``complete()`` method is non-overridable; schema validation, retry,
    bounded context, sampling decay, and the G3 auditor probe all live
    here.
    """

    __slots__ = ("_config", "_auditor", "_attempt_count")
    backend_name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "complete" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} cannot override BrainBackend.complete(); "
                "schema validation and retry are non-overridable "
                "(INV-2-pluggable safety floor)."
            )
        # D20: every subclass MUST declare __slots__ — otherwise the
        # subclass instance gets a __dict__ and the base's slot lockdown
        # is bypassed. Subclasses without SDK state can declare
        # __slots__ = ().
        if "__slots__" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare __slots__ "
                "(attribute-injection lockdown / D20). Use "
                "`__slots__ = ()` if no extra instance attrs are needed."
            )

    def __init__(self, config: Any, *, mcpd_client: Any = _GUARD_SENTINEL) -> None:
        if mcpd_client is not _GUARD_SENTINEL:
            raise BrainSecurityError(
                "INV-1 / Phase-2 G3 violation: "
                "BrainBackend must never receive an mcpd_client."
            )
        self._config = config
        self._auditor = OutboundPayloadAuditor()
        self._attempt_count = 0

    @abstractmethod
    def _call_provider(
        self, envelope: RequestEnvelope
    ) -> tuple[str, int, int]:
        """Provider-specific raw call. Returns ``(raw_text, tokens_in, tokens_out)``.

        Subclass contract:

        1. Translate ``envelope`` into provider-specific request payload.
           Honor ``envelope.tools_disabled`` by setting the SDK's
           tools/functions/grounding field to its disabled value.
        2. Call ``self._auditor.intercept(payload_dict)`` immediately
           before SDK transmission. The base class verifies this via a
           call-count probe — skipping the auditor raises
           ``BrainSecurityError`` after ``_call_provider`` returns.
        3. Wrap SDK errors in ``raise BrainProviderError(
           sanitize_exception(e)) from None``. ``from None`` is required
           to strip the original exception's ``__context__`` (which may
           carry the Authorization header).
        """

    def _format_retry_feedback(self, prior_error: str) -> str:
        """Concise correction note for the next attempt.

        Does NOT include the prior raw output. Token count is bounded
        regardless of retry depth (D15). Subclasses MAY override to
        emit a provider-natural correction phrasing (e.g. Anthropic
        critique-as-user-turn) but MUST keep the total bounded.
        """
        return (
            f"PREVIOUS ATTEMPT FAILED: {prior_error[:200]}. Output JSON only."
        )

    def _estimate_cost(
        self, tokens_in: int, tokens_out: int
    ) -> float | None:
        """Backend cost estimate in USD, or ``None`` if not applicable.

        Local backend stays ``None``. API backends override with their
        published rate table.
        """
        return None

    @staticmethod
    def _extract_json_strict(raw: str) -> dict:
        """Single strict slice: first ``{`` to its matching ``}``.

        No regex fallback. No second-best slice. Brittle extraction is
        an injection channel where attackers hide payloads in
        conversational padding (D19).
        """
        start = raw.find("{")
        if start < 0:
            raise json.JSONDecodeError("no opening brace", raw, 0)
        depth = 0
        end = -1
        for index in range(start, len(raw)):
            char = raw[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end < 0:
            raise json.JSONDecodeError("unterminated object", raw, start)
        parsed = json.loads(raw[start : end + 1])
        if not isinstance(parsed, dict):
            raise json.JSONDecodeError(
                "extracted JSON is not an object", raw, start
            )
        return parsed

    def complete(
        self,
        system: str,
        user: str,
        schema: dict | None,
        max_retries: int = 3,
    ) -> BrainResponse:
        if max_retries < 1:
            raise BrainConfigError(
                f"max_retries={max_retries} must be >= 1"
            )
        if max_retries > MAX_RETRY_HARD_CAP:
            raise BrainConfigError(
                f"max_retries={max_retries} exceeds hard cap "
                f"{MAX_RETRY_HARD_CAP}"
            )

        validator = (
            jsonschema.Draft7Validator(
                schema,
                format_checker=jsonschema.FormatChecker(formats=["uuid"]),
            )
            if schema is not None
            else None
        )

        attempt = 0
        prior_error = ""
        last_excerpt = ""
        effective_user = user
        last_truncation = False

        while attempt < max_retries:
            attempt += 1
            self._attempt_count = attempt
            sampling = _SAMPLING_DECAY[
                min(attempt - 1, len(_SAMPLING_DECAY) - 1)
            ]
            envelope = RequestEnvelope(
                system=system,
                user=effective_user,
                schema=schema,
                sampling=sampling,
                tools_disabled=True,
            )
            probe_before = self._auditor.call_count
            raw, tokens_in, tokens_out = self._call_provider(envelope)
            if self._auditor.call_count == probe_before:
                raise BrainSecurityError(
                    f"{type(self).__name__}._call_provider did not call "
                    "self._auditor.intercept(); G3 stage-2 contract "
                    "violated."
                )

            try:
                payload = self._extract_json_strict(raw)
                if validator is not None:
                    validator.validate(payload)
                return BrainResponse(
                    content_json=payload,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=self._estimate_cost(tokens_in, tokens_out),
                    backend=self.backend_name,
                    model=getattr(self._config, "model", "unknown"),
                    attempts=attempt,
                )
            except json.JSONDecodeError as exc:
                last_truncation = True
                prior_error = f"JSON decode failure: {exc.msg}"
                last_excerpt = raw[:200]
            except jsonschema.ValidationError as exc:
                last_truncation = False
                prior_error = (
                    f"Schema validation failure at "
                    f"{list(exc.absolute_path)}: {exc.message}"
                )
                last_excerpt = raw[:200]

            effective_user = (
                user + "\n\n" + self._format_retry_feedback(prior_error)
            )

        error_cls = (
            BrainTruncationError if last_truncation else BrainSchemaError
        )
        raise error_cls(prior_error, attempt, last_excerpt)
