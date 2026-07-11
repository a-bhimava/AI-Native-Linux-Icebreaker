"""Phase 6 Scope B — legacy config regression tests.

The rollback promise (JSON Schema `additionalProperties: true` at
top-level + explicit defaults on every new dataclass field) requires
that a `controller.toml` written by a v6.65 daemon still loads on a
v1.0-rc1 daemon without missing-key exceptions or field loss.

These tests pin that promise: adding a new required field, or removing
a default, breaks a test here — before it breaks a shipping user's
config on first boot after upgrade.
"""

from __future__ import annotations

import pathlib
import tempfile
import textwrap

import pytest

from controller.config import load


# ── Fixtures ──────────────────────────────────────────────────────────────


_MIN_LEGACY_CONFIG = textwrap.dedent("""\
    # Minimal v6.65-shaped TOML — the bare minimum the schema required
    # before Scope B added new fields. Must still load post-Scope-B.
    [qb]
    backend = "gemini"

    [qb.gemini]
    model = "gemini-2.5-flash"
    api_key_env = "GEMINI_API_KEY"
    max_tokens = 4096
    timeout_seconds = 60
""")


_UNKNOWN_TOP_LEVEL_SECTION = textwrap.dedent("""\
    # A daemon released AFTER v1.0-rc1 might introduce a new top-level
    # section. Rollback safety: an OLDER daemon (this test) must load
    # the config anyway, not reject it with additionalProperties=false.
    [qb]
    backend = "gemini"

    [qb.gemini]
    model = "gemini-2.5-flash"
    api_key_env = "GEMINI_API_KEY"
    max_tokens = 4096
    timeout_seconds = 60

    [phase_9_future_section]
    something = "we haven't shipped this yet"
    another = 42
""")


@pytest.fixture
def toml_path():
    """Yield a factory that writes given TOML text to a fresh temp file."""
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)

        def _make(contents: str) -> pathlib.Path:
            p = d / "controller.toml"
            p.write_text(contents)
            return p

        yield _make


# ── The tests ─────────────────────────────────────────────────────────────


def test_legacy_config_loads_with_new_field_defaults(toml_path):
    """A pre-Scope-B config must still load, and every new field must
    resolve to its dataclass default (not raise KeyError)."""
    cfg = load(toml_path(_MIN_LEGACY_CONFIG))

    # RunConfig — 2 new fields.
    assert cfg.run.turn_timeout_seconds == 600.0
    assert cfg.run.model_probe_timeout_seconds == 5.0

    # SessionConfig — 2 new fields.
    assert cfg.session.max_shell_context_chars == 512
    assert cfg.session.max_recent_commands == 5

    # DaemonConfig — 2 new fields (+ socket_group added at Scope B).
    assert cfg.daemon.reader_recv_timeout_seconds == 1.0
    assert cfg.daemon.max_reconnect_delay_seconds == 30.0
    assert cfg.daemon.socket_group == "icebreaker-users"


def test_new_field_values_override_dataclass_defaults(toml_path):
    """When a config DOES set a new field, the value wins over the default."""
    tomltext = _MIN_LEGACY_CONFIG + textwrap.dedent("""

        [run]
        turn_timeout_seconds = 1800
        model_probe_timeout_seconds = 15

        [session]
        max_shell_context_chars = 2048
        max_recent_commands = 10

        [daemon]
        reader_recv_timeout_seconds = 2.5
        max_reconnect_delay_seconds = 60.0
    """)
    cfg = load(toml_path(tomltext))
    assert cfg.run.turn_timeout_seconds == 1800
    assert cfg.run.model_probe_timeout_seconds == 15
    assert cfg.session.max_shell_context_chars == 2048
    assert cfg.session.max_recent_commands == 10
    assert cfg.daemon.reader_recv_timeout_seconds == 2.5
    assert cfg.daemon.max_reconnect_delay_seconds == 60.0


def test_unknown_top_level_section_does_not_reject_config(toml_path):
    """Rollback safety: an older daemon must tolerate a config that
    carries sections it doesn't know about (Confluent FULL compat)."""
    # This must NOT raise. That's the whole contract — if the schema
    # rejects unknown top-level sections, a config written by a future
    # daemon breaks the current one, which is a rollback-breaker.
    cfg = load(toml_path(_UNKNOWN_TOP_LEVEL_SECTION))
    # Sanity: known sections still resolved.
    assert cfg.qb.name == "gemini"


def test_unknown_per_section_key_still_rejected(toml_path):
    """Per-section `additionalProperties: false` catches typos in known
    sections — schema loosening at the top level must NOT bleed down."""
    tomltext = _MIN_LEGACY_CONFIG + textwrap.dedent("""

        [run]
        turn_timeout_seconds = 900
        # Typo in a known section — should be rejected.
        typoed_field_that_does_not_exist = 42
    """)
    with pytest.raises(Exception) as exc_info:
        load(toml_path(tomltext))
    # Reason should mention the offending key so operators can fix it.
    assert "typoed_field_that_does_not_exist" in str(exc_info.value)


def test_schema_bounds_are_enforced_at_load(toml_path):
    """A value outside the schema bounds must fail loudly, not clamp
    silently — the daemon's schema check is the last line of defense
    when a user hand-edits the TOML past the GUI's Adw.SpinRow guard."""
    # turn_timeout_seconds max is 7200; try 8000.
    tomltext = _MIN_LEGACY_CONFIG + textwrap.dedent("""

        [run]
        turn_timeout_seconds = 8000
    """)
    with pytest.raises(Exception) as exc_info:
        load(toml_path(tomltext))
    assert "turn_timeout_seconds" in str(exc_info.value) or "7200" in str(exc_info.value)
