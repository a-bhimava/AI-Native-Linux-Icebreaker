"""Tests for gui_agent.trust_store."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gui_agent.trust_store import (
    TIER_DENY,
    TIER_ONCE,
    TIER_PERSISTENT,
    TIER_SESSION,
    TrustError,
    TrustGrant,
    TrustStore,
    _wildcard_match,
    _specificity,
)


@pytest.fixture
def tmp_store(tmp_path):
    """Fresh TrustStore with a temp path + empty defaults dir."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    store_path = tmp_path / "trust.jsonl"
    return TrustStore(path=store_path, defaults_dir=defaults, session_id="test-session")


@pytest.fixture
def tmp_store_with_defaults(tmp_path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "01-hover.jsonl").write_text(
        '{"app":"*","tool":"gui.hover","tier":"persistent","reason":"safe read-only"}\n'
        '{"app":"*","tool":"gui.parse_screen","tier":"persistent"}\n'
    )
    store_path = tmp_path / "trust.jsonl"
    return TrustStore(path=store_path, defaults_dir=defaults, session_id="s1")


# ═══ Basic grant + check ══════════════════════════════════════════════


def test_check_no_grant_returns_denied(tmp_store):
    d = tmp_store.check("slack", "gui.click")
    assert not d.allowed
    assert "no matching grant" in d.reason
    assert d.matched_grant is None


def test_grant_persistent_then_check_allowed(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=60)
    d = tmp_store.check("slack", "gui.click")
    assert d.allowed
    assert d.matched_grant is not None
    assert d.matched_grant.app == "slack"


def test_grant_survives_process_restart(tmp_path):
    """Grants written by one TrustStore instance are visible to the next."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    store_path = tmp_path / "trust.jsonl"

    s1 = TrustStore(path=store_path, defaults_dir=defaults, session_id="s1")
    s1.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=300)

    s2 = TrustStore(path=store_path, defaults_dir=defaults, session_id="s2")
    assert s2.check("slack", "gui.click").allowed


# ═══ Three tiers ══════════════════════════════════════════════════════


def test_tier_once_consumed_after_first_check(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_ONCE)
    d1 = tmp_store.check("slack", "gui.click")
    assert d1.allowed
    d2 = tmp_store.check("slack", "gui.click")
    assert not d2.allowed, "once-grant should be consumed after first check"


def test_tier_session_binds_to_session_id(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_SESSION)
    assert tmp_store.check("slack", "gui.click").allowed
    tmp_store.new_session("different-session")
    assert not tmp_store.check("slack", "gui.click").allowed, \
        "session grant should not apply across sessions"


def test_tier_persistent_expires_after_ttl(tmp_store, monkeypatch):
    """Persistent grant with TTL — after TTL, check() returns False with
    an explicit 'expired' reason (not silent deny)."""
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=1)
    # Fast-forward time.
    real_time = time.time
    monkeypatch.setattr("gui_agent.trust_store.time.time",
                        lambda: real_time() + 10)
    d = tmp_store.check("slack", "gui.click")
    assert not d.allowed
    assert "expired" in d.reason.lower()


def test_tier_persistent_zero_ttl_never_expires(tmp_store, monkeypatch):
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=0)
    real_time = time.time
    monkeypatch.setattr("gui_agent.trust_store.time.time",
                        lambda: real_time() + 10 * 365 * 24 * 3600)
    assert tmp_store.check("slack", "gui.click").allowed


def test_tier_deny_always_blocks_check(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_DENY, reason="paranoid")
    d = tmp_store.check("slack", "gui.click")
    assert not d.allowed
    assert "deny_always" in d.reason


def test_deny_overrides_prior_grant(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=3600)
    tmp_store.grant("slack", "gui.click", tier=TIER_DENY, reason="revoked")
    d = tmp_store.check("slack", "gui.click")
    assert not d.allowed


# ═══ Hard-deny fallback ═══════════════════════════════════════════════


def test_terminal_hard_denied_even_with_grant(tmp_store):
    """A user CAN'T accidentally grant terminal auto-click — the hard-
    deny fallback in the code always wins."""
    tmp_store.grant("gnome-terminal", "gui.click", tier=TIER_PERSISTENT,
                    ttl_seconds=3600)
    d = tmp_store.check("gnome-terminal", "gui.click")
    assert not d.allowed
    assert "hard-deny" in d.reason


def test_hard_deny_respects_normalized_input(tmp_store):
    """Uppercase input still hits the hard-deny for terminal."""
    d = tmp_store.check("GNOME-Terminal", "gui.click")
    assert not d.allowed


# ═══ Wildcards ════════════════════════════════════════════════════════


def test_wildcard_match_helper():
    assert _wildcard_match("*", "anything")
    assert _wildcard_match("gui.*", "gui.click")
    assert _wildcard_match("gui.*", "gui.type_at_coords")
    assert _wildcard_match("gnome-*", "gnome-calculator")
    assert not _wildcard_match("gui.*", "rpa.click")
    assert not _wildcard_match("gnome-*", "xterm")
    assert _wildcard_match("exact", "exact")
    assert not _wildcard_match("exact", "different")


def test_wildcard_app_all_apps(tmp_store):
    tmp_store.grant("*", "gui.hover", tier=TIER_PERSISTENT, ttl_seconds=3600)
    assert tmp_store.check("slack", "gui.hover").allowed
    assert tmp_store.check("firefox", "gui.hover").allowed
    assert not tmp_store.check("slack", "gui.click").allowed


def test_wildcard_tool_all_tools_in_app(tmp_store):
    tmp_store.grant("nautilus", "gui.*", tier=TIER_PERSISTENT, ttl_seconds=3600)
    assert tmp_store.check("nautilus", "gui.click").allowed
    assert tmp_store.check("nautilus", "gui.drag").allowed
    assert not tmp_store.check("firefox", "gui.click").allowed


def test_specificity_prefers_exact_over_wildcard(tmp_store):
    """When both a wildcard grant and an exact grant match, the exact
    one is picked so its reason surfaces (audit clarity)."""
    tmp_store.grant("*", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=3600,
                    reason="global")
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=3600,
                    reason="specific")
    d = tmp_store.check("slack", "gui.click")
    assert d.allowed
    assert "specific" in d.reason


def test_specificity_helper():
    zero = TrustGrant(app="slack", tool="gui.click", tier=TIER_PERSISTENT)
    one = TrustGrant(app="*", tool="gui.click", tier=TIER_PERSISTENT)
    two = TrustGrant(app="*", tool="*", tier=TIER_PERSISTENT)
    assert _specificity(zero) == 0
    assert _specificity(one) == 1
    assert _specificity(two) == 2


# ═══ Defaults loading ══════════════════════════════════════════════════


def test_defaults_loaded_at_init(tmp_store_with_defaults):
    assert tmp_store_with_defaults.check("slack", "gui.hover").allowed
    assert tmp_store_with_defaults.check("random-app", "gui.parse_screen").allowed


def test_defaults_source_tag_recorded(tmp_store_with_defaults):
    d = tmp_store_with_defaults.check("slack", "gui.hover")
    assert "defaults" in d.reason


def test_missing_defaults_dir_does_not_crash(tmp_path):
    """defaults_dir doesn't exist → store still boots (defense in depth)."""
    store = TrustStore(
        path=tmp_path / "trust.jsonl",
        defaults_dir=tmp_path / "does-not-exist",
        session_id="x",
    )
    # Terminal is still hard-denied even without defaults.
    assert not store.check("gnome-terminal", "gui.click").allowed


def test_bad_json_in_defaults_skipped_not_fatal(tmp_path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "bad.jsonl").write_text(
        "not json at all\n"
        '{"app":"*","tool":"gui.hover","tier":"persistent"}\n'  # good line survives
    )
    store = TrustStore(path=tmp_path / "s.jsonl", defaults_dir=defaults,
                       session_id="s")
    assert store.check("any", "gui.hover").allowed


def test_defaults_legacy_shorthand_allow_and_deny(tmp_path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "legacy.jsonl").write_text(
        '{"app":"slack","tool":"gui.click","tier":"allow"}\n'
        '{"app":"slack","tool":"gui.drag","tier":"deny"}\n'
    )
    store = TrustStore(path=tmp_path / "s.jsonl", defaults_dir=defaults,
                       session_id="s")
    assert store.check("slack", "gui.click").allowed
    d = store.check("slack", "gui.drag")
    assert not d.allowed
    assert "deny_always" in d.reason


# ═══ Revoke ═══════════════════════════════════════════════════════════


def test_revoke_removes_grant_and_persists(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=3600)
    removed = tmp_store.revoke("slack", "gui.click")
    assert removed == 1
    assert not tmp_store.check("slack", "gui.click").allowed


def test_revoke_leaves_deny_alone(tmp_store):
    """revoke() should not accidentally remove a deny — user has to
    explicitly delete/edit the file for that."""
    tmp_store.grant("slack", "gui.click", tier=TIER_DENY)
    removed = tmp_store.revoke("slack", "gui.click")
    assert removed == 0
    d = tmp_store.check("slack", "gui.click")
    assert not d.allowed  # deny still stands


def test_revoke_survives_reload(tmp_path):
    """A revoke event persisted to the JSONL is honored by a fresh store."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    store_path = tmp_path / "s.jsonl"

    s1 = TrustStore(path=store_path, defaults_dir=defaults, session_id="a")
    s1.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=3600)
    s1.revoke("slack", "gui.click")

    s2 = TrustStore(path=store_path, defaults_dir=defaults, session_id="b")
    assert not s2.check("slack", "gui.click").allowed


# ═══ Validation ═══════════════════════════════════════════════════════


def test_grant_rejects_bad_tier(tmp_store):
    with pytest.raises(TrustError):
        tmp_store.grant("slack", "gui.click", tier="maybe")


def test_grant_rejects_empty_app(tmp_store):
    with pytest.raises(TrustError):
        tmp_store.grant("", "gui.click", tier=TIER_PERSISTENT)


def test_grant_rejects_shell_metachars(tmp_store):
    for bad in ("$(rm)", "app`cmd`", "a;b", "a|b", "a&b"):
        with pytest.raises(TrustError):
            tmp_store.grant(bad, "gui.click", tier=TIER_PERSISTENT)


def test_grant_rejects_ttl_out_of_range(tmp_store):
    with pytest.raises(TrustError):
        tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT,
                        ttl_seconds=-1)
    with pytest.raises(TrustError):
        tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT,
                        ttl_seconds=99 * 365 * 24 * 3600)


def test_grant_rejects_overlong_app(tmp_store):
    with pytest.raises(TrustError):
        tmp_store.grant("x" * 200, "gui.click", tier=TIER_PERSISTENT)


# ═══ Normalization ════════════════════════════════════════════════════


def test_app_and_tool_normalized_case_insensitive(tmp_store):
    tmp_store.grant("Slack", "GUI.Click", tier=TIER_PERSISTENT, ttl_seconds=60)
    assert tmp_store.check("SLACK", "gui.click").allowed
    assert tmp_store.check("slack", "GUI.CLICK").allowed


# ═══ list_active / list_all ══════════════════════════════════════════


def test_list_active_excludes_expired(tmp_store, monkeypatch):
    tmp_store.grant("a", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=1)
    tmp_store.grant("b", "gui.hover", tier=TIER_PERSISTENT, ttl_seconds=3600)
    real_time = time.time
    monkeypatch.setattr("gui_agent.trust_store.time.time",
                        lambda: real_time() + 10)
    active = tmp_store.list_active()
    apps = {g.app for g in active}
    assert apps == {"b"}


def test_list_all_includes_expired(tmp_store, monkeypatch):
    tmp_store.grant("a", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=1)
    real_time = time.time
    monkeypatch.setattr("gui_agent.trust_store.time.time",
                        lambda: real_time() + 10)
    all_ = tmp_store.list_all()
    assert any(g.app == "a" for g in all_)


# ═══ File format ══════════════════════════════════════════════════════


def test_persisted_file_is_valid_jsonl(tmp_store):
    tmp_store.grant("slack", "gui.click", tier=TIER_PERSISTENT, ttl_seconds=60,
                    reason="testing")
    content = tmp_store._path.read_text()  # noqa: SLF001 — inspecting for test
    lines = [l for l in content.splitlines() if l.strip()]
    assert len(lines) == 1
    import json
    parsed = json.loads(lines[0])
    assert parsed["app"] == "slack"
    assert parsed["tool"] == "gui.click"
    assert parsed["event"] == "grant"
    assert parsed["tier"] == TIER_PERSISTENT
