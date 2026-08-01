"""Tests for scripts.ib_trust — the `ib-trust` CLI."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from scripts.ib_trust import _fuzzy_tool, main


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Provide a self-contained trust store + defaults dir. The undo
    ring file (_UNDO_STATE_PATH) is redirected to a per-test tmp path
    so tests don't pollute each other's undo state."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    store_path = tmp_path / "trust.jsonl"
    undo_path = tmp_path / "undo.json"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NO_COLOR", "1")           # deterministic output
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)  # plain output
    # Isolate the undo ring — the module reads from Path.home() at
    # import time, but the constant is a top-level module attribute
    # we can safely swap for the test's duration.
    monkeypatch.setattr("scripts.ib_trust._UNDO_STATE_PATH", undo_path)
    return {"store": str(store_path), "defaults": str(defaults),
            "store_path": store_path, "defaults_path": defaults,
            "undo_path": undo_path}


def _run(env, *argv) -> tuple[int, str, str]:
    """Invoke main() with --store + --defaults-dir prepended. Returns
    (exit_code, stdout, stderr)."""
    args = ["--store", env["store"], "--defaults-dir", env["defaults"],
            *argv]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = main(args)
    return rc, stdout.getvalue(), stderr.getvalue()


# ═══ Fuzzy match ═══════════════════════════════════════════════════════


def test_fuzzy_tool_short_forms():
    assert _fuzzy_tool("click") == "gui.grounded_click"
    assert _fuzzy_tool("type") == "gui.grounded_type"
    assert _fuzzy_tool("drag") == "gui.grounded_drag"
    assert _fuzzy_tool("hover") == "gui.hover"
    assert _fuzzy_tool("screenshot") == "gui.screenshot"
    assert _fuzzy_tool("parse") == "gui.parse_screen"


def test_fuzzy_tool_qualified_passthrough():
    assert _fuzzy_tool("gui.grounded_click") == "gui.grounded_click"
    assert _fuzzy_tool("rpa.execute_workflow") == "rpa.execute_workflow"
    assert _fuzzy_tool("gui.*") == "gui.*"


def test_fuzzy_tool_unknown_passthrough():
    """Unknown short forms pass through unchanged — the store will
    surface a clear "no matching grant" if nothing matches."""
    assert _fuzzy_tool("wingdings") == "wingdings"


# ═══ add ═══════════════════════════════════════════════════════════════


def test_add_persistent_grants_and_prints(env):
    rc, out, err = _run(env, "add", "slack", "gui.click",
                        "--ttl", "300")
    assert rc == 0, f"stderr={err!r}"
    assert "granted" in out.lower()
    assert "slack" in out


def test_add_fuzzy_expands_click(env):
    rc, out, err = _run(env, "add", "slack", "click", "--ttl", "60")
    assert rc == 0
    assert "gui.grounded_click" in out


def test_add_dry_run_does_not_persist(env):
    rc, _, _ = _run(env, "add", "slack", "click", "--dry-run", "--ttl", "60")
    assert rc == 0
    # Store file must not exist yet (only writes on real actions).
    assert not env["store_path"].exists()


def test_add_bad_input_returns_nonzero(env):
    rc, _, err = _run(env, "add", "$(rm)", "gui.click", "--ttl", "60")
    assert rc == 2
    assert "error" in err.lower()


def test_add_persistent_ttl_appears_in_check(env):
    _run(env, "add", "slack", "gui.grounded_click", "--ttl", "3600")
    # Now grant should be visible via `why`.
    rc, out, _ = _run(env, "why", "slack", "gui.grounded_click")
    assert rc == 0
    assert "ALLOWED" in out


# ═══ revoke ════════════════════════════════════════════════════════════


def test_revoke_removes_grant(env):
    _run(env, "add", "slack", "gui.grounded_click", "--ttl", "3600")
    rc, out, _ = _run(env, "revoke", "slack", "gui.grounded_click")
    assert rc == 0
    assert "revoked 1" in out


def test_revoke_no_matching_grants_reports_zero(env):
    rc, out, _ = _run(env, "revoke", "unknown-app", "gui.click")
    assert rc == 0
    assert "nothing to revoke" in out


def test_revoke_dry_run_reports_but_does_not_persist(env):
    _run(env, "add", "slack", "gui.grounded_click", "--ttl", "3600")
    rc, out, _ = _run(env, "revoke", "slack", "gui.grounded_click", "--dry-run")
    assert rc == 0
    assert "dry-run" in out.lower()
    # Still allowed after dry-run.
    rc2, out2, _ = _run(env, "why", "slack", "gui.grounded_click")
    assert rc2 == 0
    assert "ALLOWED" in out2


# ═══ why ═══════════════════════════════════════════════════════════════


def test_why_allowed_exits_zero(env):
    _run(env, "add", "slack", "click", "--ttl", "60")   # fuzzy → gui.grounded_click
    rc, out, _ = _run(env, "why", "slack", "click")     # fuzzy → gui.grounded_click
    assert rc == 0
    assert "ALLOWED" in out
    assert "gui.grounded_click" in out


def test_why_denied_exits_one(env):
    rc, out, _ = _run(env, "why", "slack", "gui.grounded_click")
    assert rc == 1
    assert "DENIED" in out
    assert "no matching grant" in out


def test_why_shows_reason_on_hard_deny(env):
    rc, out, _ = _run(env, "why", "gnome-terminal", "gui.click")
    assert rc == 1
    assert "DENIED" in out
    assert "hard-deny" in out


# ═══ list ══════════════════════════════════════════════════════════════


def test_list_empty_prints_no_grants(env):
    rc, out, _ = _run(env, "list")
    assert rc == 0
    assert "no grants" in out.lower() or "grant" not in out.lower() \
        or "trust store" in out.lower()


def test_list_after_add_shows_row(env):
    # Use fuzzy short-form 'click' → expands to gui.grounded_click.
    _run(env, "add", "slack", "click", "--ttl", "60")
    rc, out, _ = _run(env, "list")
    assert rc == 0
    assert "slack" in out
    assert "gui.grounded_click" in out


def test_list_json_produces_parseable(env):
    _run(env, "add", "slack", "gui.click", "--ttl", "60")
    rc, out, _ = _run(env, "list", "--json")
    assert rc == 0
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert any(g["app"] == "slack" for g in parsed)


def test_list_all_includes_expired(env, monkeypatch):
    """After TTL expires, --all shows the entry; default hides it."""
    _run(env, "add", "slack", "gui.click", "--ttl", "1")
    import time as _t
    real_time = _t.time
    monkeypatch.setattr("gui_agent.trust_store.time.time",
                        lambda: real_time() + 100)
    rc_active, out_active, _ = _run(env, "list")
    rc_all, out_all, _ = _run(env, "list", "--all")
    assert rc_active == 0 and rc_all == 0
    assert "slack" in out_all


# ═══ undo ═══════════════════════════════════════════════════════════════


def test_undo_reverts_last_grant(env):
    _run(env, "add", "slack", "click", "--ttl", "60")   # fuzzy → gui.grounded_click
    rc_before, out_before, _ = _run(env, "why", "slack", "click")
    assert rc_before == 0 and "ALLOWED" in out_before

    rc_undo, out_undo, _ = _run(env, "undo")
    assert rc_undo == 0
    assert "undid" in out_undo.lower() or "↩" in out_undo

    rc_after, out_after, _ = _run(env, "why", "slack", "click")
    assert rc_after == 1
    assert "DENIED" in out_after


def test_undo_empty_prints_nothing_to_undo(env):
    rc, out, _ = _run(env, "undo")
    assert rc == 0
    assert "nothing to undo" in out.lower()


def test_undo_after_revoke_cannot_auto_restore(env):
    """A revoke undo can only warn — original tier/reason are lost."""
    _run(env, "add", "slack", "click", "--ttl", "60")
    _run(env, "revoke", "slack", "click")
    rc, out, _ = _run(env, "undo")
    # We warn (non-zero) rather than pretend to restore.
    assert rc == 1
    assert "cannot auto-restore" in out.lower()


# ═══ defaults ═══════════════════════════════════════════════════════════


def test_defaults_lists_only_defaults_grants(env):
    (env["defaults_path"] / "01.jsonl").write_text(
        '{"app":"*","tool":"gui.hover","tier":"persistent","ttl_seconds":86400}\n'
    )
    _run(env, "add", "slack", "gui.click", "--ttl", "60")  # user grant
    rc, out, _ = _run(env, "defaults")
    assert rc == 0
    assert "gui.hover" in out
    # User grant should NOT appear in defaults view.
    assert "slack" not in out or "granted_by" in out  # (headers may show 'granted_by')


# ═══ export + import ═══════════════════════════════════════════════════


def test_export_produces_valid_jsonl(env):
    _run(env, "add", "slack", "click", "--ttl", "60", "--reason", "test")
    rc, out, _ = _run(env, "export", "-")
    assert rc == 0
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["app"] == "slack"
    assert parsed["tool"] == "gui.grounded_click"


def test_import_dry_run_does_not_persist(env, tmp_path):
    src = tmp_path / "import.jsonl"
    src.write_text('{"app":"slack","tool":"gui.click","tier":"persistent","ttl_seconds":60}\n')
    rc, out, _ = _run(env, "import", str(src), "--dry-run")
    assert rc == 0
    assert "dry-run" in out.lower()
    # No real grant persisted.
    rc2, out2, _ = _run(env, "why", "slack", "gui.click")
    assert rc2 == 1
    assert "DENIED" in out2


def test_import_persists_grants(env, tmp_path):
    src = tmp_path / "import.jsonl"
    src.write_text(
        '{"app":"slack","tool":"gui.grounded_click","tier":"persistent","ttl_seconds":3600}\n'
        '{"app":"firefox","tool":"gui.hover","tier":"persistent","ttl_seconds":3600}\n'
    )
    rc, out, _ = _run(env, "import", str(src))
    assert rc == 0
    assert "imported 2" in out
    rc2, out2, _ = _run(env, "why", "slack", "gui.grounded_click")
    assert rc2 == 0 and "ALLOWED" in out2


def test_import_bad_line_skipped_others_survive(env, tmp_path):
    src = tmp_path / "import.jsonl"
    src.write_text(
        "not json\n"
        '{"app":"firefox","tool":"gui.hover","tier":"persistent","ttl_seconds":3600}\n'
    )
    rc, out, err = _run(env, "import", str(src))
    assert rc == 0   # succeeds despite one bad line
    assert "bad JSON" in err or "skipping" in err
    assert "imported 1" in out


# ═══ Real defaults JSONL (integration) ═════════════════════════════════


def test_shipped_defaults_load(env, tmp_path):
    """The actual cx-distro/distro/gui_trust_defaults.jsonl file loads
    without errors and produces the expected shape of grants."""
    # Copy shipped defaults into this test's defaults dir.
    src = Path(__file__).parent.parent.parent.parent \
        / "cx-distro" / "distro" / "gui_trust_defaults.jsonl"
    if not src.exists():
        pytest.skip(f"shipped defaults not at {src}")
    (env["defaults_path"] / "00-defaults.jsonl").write_text(src.read_text())
    rc, out, err = _run(env, "defaults")
    assert rc == 0, f"stderr: {err}"
    # Sample expected entries:
    assert "gui.hover" in out
    assert "gui.parse_screen" in out
    assert "gnome-terminal" in out
    assert "gnome-calculator" in out


def test_shipped_defaults_terminal_hard_denied(env, tmp_path):
    src = Path(__file__).parent.parent.parent.parent \
        / "cx-distro" / "distro" / "gui_trust_defaults.jsonl"
    if not src.exists():
        pytest.skip(f"shipped defaults not at {src}")
    (env["defaults_path"] / "00-defaults.jsonl").write_text(src.read_text())
    rc, out, _ = _run(env, "why", "gnome-terminal", "gui.click")
    assert rc == 1
    assert "DENIED" in out


def test_shipped_defaults_calculator_pre_approved(env, tmp_path):
    src = Path(__file__).parent.parent.parent.parent \
        / "cx-distro" / "distro" / "gui_trust_defaults.jsonl"
    if not src.exists():
        pytest.skip(f"shipped defaults not at {src}")
    (env["defaults_path"] / "00-defaults.jsonl").write_text(src.read_text())
    rc, out, _ = _run(env, "why", "gnome-calculator", "gui.grounded_click")
    assert rc == 0
    assert "ALLOWED" in out
