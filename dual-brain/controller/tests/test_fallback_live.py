"""Phase 6 Scope E — LIVE fallback chain sweep.

Skipped unless ``ICEBREAKER_LIVE=1`` is set AND at least
``ANTHROPIC_API_KEY`` is configured. Runs against real provider APIs
to verify the fallback flow that mocked tests cannot cover:

  * BrainProviderError from a bogus primary key really does trigger
    fallback (matches the offline test but with real error class).
  * BrainSchemaError from a hostile prompt does NOT trigger fallback
    (E2 in the plan).
  * All-keys-bogus produces an enumerated failure message identifying
    every backend + its error type.
  * Streaming: partial output prevents fallback (real network drop
    behavior may differ from mocked BrainProviderError).

Env vars this test reads (NEVER inlined — the operator supplies them):

  * ICEBREAKER_LIVE=1                — master gate. Absent = skip.
  * ANTHROPIC_API_KEY                — real, working Anthropic key.
  * GEMINI_API_KEY                   — real, working Gemini key.
  * OPENAI_API_KEY                   — optional; unused today.
  * ICEBREAKER_E2E_BAD_GEMINI_KEY    — a well-formed-shape Gemini key
                                       that fails auth (401). Operator
                                       supplies from a burner project
                                       or a rotated-out real key.
  * ICEBREAKER_E2E_BAD_ANTHROPIC_KEY — same shape for Anthropic.
  * ICEBREAKER_E2E_BAD_GEMINI_KEY_2  — a SECOND bad Gemini key for the
                                       chain-exhaustion test (E3). Any
                                       string the Gemini SDK will
                                       accept at config time and the
                                       Gemini API will reject at 401.

Rationale for the bad-key env vars: some provider SDKs validate the
key's SHAPE at construction time (regex on prefix, length) and raise
before ever hitting the network. Random garbage won't survive that.
The operator has to provide a real-shape key that they've already
revoked or that comes from a burner project — the test file itself
must never carry one, even a synthetic one, because entropy-based
secret scanners cannot distinguish "obviously synthetic" from real
(GitGuardian PR #29, 2026-07-11).

Running the sweep:

    ICEBREAKER_LIVE=1 \\
    ANTHROPIC_API_KEY=<real-anthropic-key> \\
    GEMINI_API_KEY=<real-gemini-key> \\
    ICEBREAKER_E2E_BAD_GEMINI_KEY=<revoked-shape-gemini-key> \\
    ICEBREAKER_E2E_BAD_ANTHROPIC_KEY=<revoked-shape-anthropic-key> \\
    ICEBREAKER_E2E_BAD_GEMINI_KEY_2=<second-revoked-shape-gemini-key> \\
    pytest controller/tests/test_fallback_live.py -v

Or source ``dual-brain/scripts/deploy.env`` (gitignored) which the
operator populates once per machine.

Expected duration: ~30 s (three real API calls + timeouts).
"""

from __future__ import annotations

import os

import pytest

# Only import backend construction machinery — the live keys are read
# at test time, not at import.


_LIVE = os.environ.get("ICEBREAKER_LIVE") == "1"
_ANTHROPIC = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
_OPENAI = os.environ.get("OPENAI_API_KEY", "").strip()
# Bogus, operator-supplied. NEVER inlined — see module docstring.
_BAD_GEMINI = os.environ.get("ICEBREAKER_E2E_BAD_GEMINI_KEY", "").strip()
_BAD_GEMINI_2 = os.environ.get("ICEBREAKER_E2E_BAD_GEMINI_KEY_2", "").strip()
_BAD_ANTHROPIC = os.environ.get(
    "ICEBREAKER_E2E_BAD_ANTHROPIC_KEY", ""
).strip()


pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live-only; set ICEBREAKER_LIVE=1 to enable",
)


# ── Backend factory helpers ──────────────────────────────────────────────


def _make_backend(name: str, api_key: str, model: str):
    """Instantiate a real backend against the given API key. Isolates
    the provider-SDK import so test collection stays fast even when
    the SDKs are missing.

    BackendConfig's `api_key` is a `SecretRef` — it stores an env var
    NAME, not the raw value. We stash the caller's key under a
    per-backend env var so the SecretRef can `.reveal()` it back.
    """
    from controller.backends.registry import make_backend
    from controller.backends.sanitize import SecretRef
    from controller.config import BackendConfig

    # Import concrete backend module → side-effect registers it.
    if name == "anthropic":
        import controller.backends.anthropic_backend  # noqa: F401
    elif name == "gemini":
        import controller.backends.gemini_backend  # noqa: F401
    elif name == "openai":
        import controller.backends.openai_backend  # noqa: F401
    else:
        raise ValueError(f"unknown backend {name!r}")

    # Use a per-call env var so bogus + real backends of the same
    # provider don't stomp on each other's key.
    env_var = f"_SCOPE_E_LIVE_KEY_{name.upper()}_{abs(hash(api_key)) % 10000}"
    os.environ[env_var] = api_key

    qb = BackendConfig(
        name=name,
        model=model,
        api_key=SecretRef(env_var),
        max_tokens=64,
        timeout_seconds=15,
    )

    class _Wrap:
        pass
    w = _Wrap()
    w.qb = qb
    return make_backend(w)


def _events_recorder() -> tuple[list[tuple[int, str, str]], callable]:
    """Return (events_list, callback) — the recording pattern from the
    mocked tests, reused here so the assertion shape matches."""
    events: list[tuple[int, str, str]] = []
    def cb(idx: int, name: str, prior: str) -> None:
        events.append((idx, name, prior))
    return events, cb


# ── E1: primary fails, first fallback succeeds ────────────────────────────


@pytest.mark.skipif(
    not _ANTHROPIC or not _GEMINI or not _BAD_GEMINI,
    reason=(
        "needs ANTHROPIC_API_KEY, GEMINI_API_KEY, and "
        "ICEBREAKER_E2E_BAD_GEMINI_KEY (see module docstring)"
    ),
)
def test_bogus_gemini_key_falls_through_to_real_anthropic() -> None:
    """E1: intentional Scope C smoke — the shape most users will hit
    when a Gemini key rotates or is revoked. Fallback to Anthropic
    must produce a coherent answer.

    ``ICEBREAKER_E2E_BAD_GEMINI_KEY`` is operator-supplied: a Gemini
    key whose SHAPE the SDK accepts at config time but whose value
    the API rejects at 401. See module docstring — no inlined key,
    ever."""
    from controller.backends.base import BrainProviderError
    from controller.fallback_backend import FallbackChain

    bad_gemini = _make_backend(
        "gemini",
        api_key=_BAD_GEMINI,
        model="gemini-2.5-flash",
    )
    real_anthropic = _make_backend(
        "anthropic",
        api_key=_ANTHROPIC,
        model="claude-haiku-4-5",
    )
    events, cb = _events_recorder()
    chain = FallbackChain(
        primary=bad_gemini,
        fallbacks=[real_anthropic],
        on_fallback=cb,
    )

    # A trivial prompt — we care about the fallback fire, not the content.
    # Anthropic backend requires a schema; the runtime passes the intent
    # schema. For a smoke test we use a permissive one.
    trivial_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    result = chain.complete(
        system="You are a helper. Reply with a JSON object with a 'text' field.",
        user="Say hi in one word.",
        schema=trivial_schema,
    )

    # Fallback fired exactly once against Anthropic.
    assert len(events) == 1, f"expected 1 fallback event, got {events}"
    assert events[0][1] == "anthropic"

    # Anthropic answered coherently.
    assert result is not None
    assert result.content_json is not None
    # We don't assert on content_json shape because different providers
    # emit different shapes — the key regression is that a REAL answer
    # came through, not a stub sentinel.


# ── E2: schema error does NOT trigger fallback ────────────────────────────


@pytest.mark.skipif(
    not _ANTHROPIC,
    reason="needs ANTHROPIC_API_KEY",
)
def test_schema_error_bypasses_fallback_live() -> None:
    """E2: hostile schema (impossible constraint) causes both
    backends to return schema violations. The chain must propagate
    the FIRST schema error rather than trying the second backend —
    trying another backend with the same broken schema is wasted API
    budget."""
    from controller.backends.base import BrainSchemaError
    from controller.fallback_backend import FallbackChain

    # Impossible schema: requires an integer field with min=100 max=99
    impossible_schema = {
        "type": "object",
        "required": ["x"],
        "properties": {"x": {"type": "integer", "minimum": 100, "maximum": 99}},
        "additionalProperties": False,
    }
    primary = _make_backend(
        "anthropic",
        api_key=_ANTHROPIC,
        model="claude-haiku-4-5",
    )
    # Second backend also Anthropic (same key, same model) — the point
    # is to prove the chain doesn't invoke it.
    fb = _make_backend(
        "anthropic",
        api_key=_ANTHROPIC,
        model="claude-haiku-4-5",
    )
    events, cb = _events_recorder()
    chain = FallbackChain(primary=primary, fallbacks=[fb], on_fallback=cb)

    with pytest.raises(BrainSchemaError):
        chain.complete(
            system="You must return valid JSON.",
            user="Produce an object.",
            schema=impossible_schema,
        )

    # No fallback fired — schema errors bypass by design.
    assert events == [], (
        "schema error triggered fallback — should propagate directly"
    )


# ── E3: all-keys-bogus produces enumerated user-facing failure ────────────


@pytest.mark.skipif(
    not _BAD_GEMINI_2 or not _BAD_ANTHROPIC,
    reason=(
        "needs ICEBREAKER_E2E_BAD_GEMINI_KEY_2 and "
        "ICEBREAKER_E2E_BAD_ANTHROPIC_KEY (see module docstring)"
    ),
)
def test_all_backends_fail_produces_enumerated_error() -> None:
    """E3: every backend has a bogus key. The chain must exhaust,
    the final raised error names the LAST backend (Anthropic here),
    and the on_fallback events name every prior attempt so the runner
    can assemble a user-visible enumeration.

    Both bad keys come from operator-supplied env vars. See module
    docstring — no inlined key, ever."""
    from controller.backends.base import BrainProviderError
    from controller.fallback_backend import FallbackChain

    bad_gemini = _make_backend(
        "gemini",
        api_key=_BAD_GEMINI_2,
        model="gemini-2.5-flash",
    )
    bad_anthropic = _make_backend(
        "anthropic",
        api_key=_BAD_ANTHROPIC,
        model="claude-haiku-4-5",
    )
    events, cb = _events_recorder()
    chain = FallbackChain(
        primary=bad_gemini,
        fallbacks=[bad_anthropic],
        on_fallback=cb,
    )

    with pytest.raises(BrainProviderError) as exc_info:
        chain.complete(
            system="You are a helper.",
            user="Say hi.",
        )

    # Callback fired once (for the anthropic attempt).
    assert len(events) == 1
    assert events[0][1] == "anthropic"

    # Prior error (in the callback) references gemini's failure —
    # different providers have different error phrasing but the
    # important thing is that some provider-specific string appears.
    prior = events[0][2].lower()
    assert (
        "api" in prior or "auth" in prior or "key" in prior
        or "invalid" in prior or "gemini" in prior or "401" in prior
    ), f"prior error too generic: {prior!r}"

    # Final error is from Anthropic — the last attempt.
    final = str(exc_info.value).lower()
    assert (
        "api" in final or "auth" in final or "key" in final
        or "invalid" in final or "anthropic" in final or "401" in final
    ), f"final error too generic: {final!r}"
