"""Phase 6 Scope B — schema_reader helper regression tests.

Bounds and descriptions come from `controller_config.json` via
`get_field_spec()`. Any GUI page that hardcodes limits diverging from
the schema is a UI bug — the daemon validates on load, so a UI-typeable
value the schema rejects is a lockout waiting to happen.

These tests ensure the reader:
  * finds the shipped schema on this dev tree,
  * returns concrete bounds for every field the Scope B pages consume,
  * returns None (not raises) for unknown fields — pages fall back
    to hardcoded defaults gracefully.
"""

from __future__ import annotations

import pytest

from gui.control.schema_reader import FieldSpec, get_field_spec, get_schema


# ── Sanity: schema loadable ───────────────────────────────────────────────


def test_schema_loads_from_repo_tree() -> None:
    """The schema resolver finds the in-repo file when there's no ISO
    install."""
    schema = get_schema()
    assert isinstance(schema, dict)
    assert schema.get("title") == "Icebreaker Controller Config"
    # Scope B guarantees top-level additionalProperties: true.
    assert schema.get("additionalProperties") is True


# ── Field specs for every Scope B knob ────────────────────────────────────


@pytest.mark.parametrize(
    ("section", "key", "min_expected", "max_expected", "reload"),
    [
        (("run",), "turn_timeout_seconds", 30.0, 7200.0, "restart"),
        (("run",), "model_probe_timeout_seconds", 1.0, 120.0, "hot"),
        (("session",), "max_shell_context_chars", 128.0, 8192.0, "hot"),
        (("session",), "max_recent_commands", 1.0, 50.0, "hot"),
        (("daemon",), "reader_recv_timeout_seconds", 0.1, 10.0, "restart"),
        (("daemon",), "max_reconnect_delay_seconds", 1.0, 300.0, "restart"),
        # Pre-existing but Scope B surfaced them in the GUI.
        (("verifier",), "votes", 1.0, 9.0, "unspecified"),
        (("verifier",), "require", 0.0, 9.0, "unspecified"),
        (("run",), "qb_max_retries", 1.0, 10.0, "hot"),
    ],
)
def test_scope_b_fields_have_schema_bounds(
    section, key, min_expected, max_expected, reload
) -> None:
    """Each new field has bounds + a description + the expected reload
    semantics. If any of these change the GUI page must be revisited."""
    spec = get_field_spec(section, key)
    assert spec is not None, f"schema missing {section} / {key}"
    assert spec.minimum == min_expected
    assert spec.maximum == max_expected
    assert isinstance(spec.description, str) and spec.description, (
        f"{section}/{key} needs a schema description"
    )
    # `unspecified` = schema didn't tag it; GUI falls back to default.
    assert spec.reload == reload


def test_unknown_field_returns_none() -> None:
    """Callers rely on None as "fall back to hardcoded default". Never
    raise for an unknown key — that would break future config additions
    landed by Scope C+."""
    spec = get_field_spec(("nonexistent",), "field")
    assert spec is None


def test_unknown_key_in_known_section_returns_none() -> None:
    """Same, but with a section that DOES exist."""
    spec = get_field_spec(("run",), "not_a_real_field")
    assert spec is None


# ── Field spec shape ───────────────────────────────────────────────────────


def test_field_spec_is_frozen_dataclass() -> None:
    """Downstream code assumes FieldSpec is immutable — mutations must
    be new instances so the schema cache stays authoritative."""
    spec = get_field_spec(("run",), "turn_timeout_seconds")
    assert spec is not None
    with pytest.raises(Exception):  # dataclass frozen=True → FrozenInstanceError
        spec.minimum = 0  # type: ignore[misc]
