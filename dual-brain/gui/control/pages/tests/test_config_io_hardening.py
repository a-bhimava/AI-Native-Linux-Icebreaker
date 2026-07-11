"""Phase 6 Scope D — config_io hardening regression tests.

Cover the three properties Scope D added to `gui/control/config_io.py`:

  1. Atomic write: `set_user_override` uses tempfile → os.replace, so
     a mid-crash never truncates the user's config.
  2. Env-var overrides: `ICEBREAKER_USER_CONFIG_PATH` +
     `ICEBREAKER_SYSTEM_CONFIG_PATH` point the module at a hermetic
     tempdir without touching the real ~/.config.
  3. Dry-run: `ICEBREAKER_DRY_RUN=1` makes `restart_controller`
     return success without invoking pkexec — so CI can exercise the
     save→restart flow.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from unittest.mock import patch

import pytest

from gui.control import config_io


@pytest.fixture
def hermetic_config(tmp_path, monkeypatch):
    """Point config_io at a tempdir for the duration of one test."""
    user_path = tmp_path / "user" / "controller.toml"
    system_path = tmp_path / "etc" / "controller.toml"
    monkeypatch.setenv("ICEBREAKER_USER_CONFIG_PATH", str(user_path))
    monkeypatch.setenv("ICEBREAKER_SYSTEM_CONFIG_PATH", str(system_path))
    config_io._refresh_paths_from_env()
    yield user_path, system_path
    monkeypatch.delenv("ICEBREAKER_USER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ICEBREAKER_SYSTEM_CONFIG_PATH", raising=False)
    config_io._refresh_paths_from_env()


# ── Env-var overrides ────────────────────────────────────────────────────


def test_user_path_override_from_env(hermetic_config) -> None:
    """USER_CONFIG_PATH must reflect ICEBREAKER_USER_CONFIG_PATH once
    _refresh_paths_from_env is called — the whole point is to let
    tests target a hermetic dir."""
    user_path, _ = hermetic_config
    assert config_io.USER_CONFIG_PATH == user_path


def test_system_path_override_from_env(hermetic_config) -> None:
    _, system_path = hermetic_config
    assert config_io.SYSTEM_CONFIG_PATH == system_path


def test_defaults_when_no_env_override(monkeypatch) -> None:
    """Absent env vars, the module falls back to the XDG path.
    Regression: an operator running Control Center without any test
    scaffolding must still target ~/.config/icebreaker."""
    monkeypatch.delenv("ICEBREAKER_USER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ICEBREAKER_SYSTEM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    config_io._refresh_paths_from_env()
    assert config_io.USER_CONFIG_PATH == (
        pathlib.Path.home() / ".config" / "icebreaker" / "controller.toml"
    )
    assert config_io.SYSTEM_CONFIG_PATH == pathlib.Path(
        "/etc/icebreaker/controller.toml"
    )


# ── Atomic write ─────────────────────────────────────────────────────────


def test_set_user_override_creates_parent_directory(hermetic_config) -> None:
    user_path, _ = hermetic_config
    assert not user_path.parent.exists()
    config_io.set_user_override(("qb",), "backend", "gemini")
    assert user_path.exists()
    assert user_path.parent.is_dir()


def test_set_user_override_atomic_replace_no_partial_file(
    hermetic_config, monkeypatch
) -> None:
    """Simulate a crash between tempfile.write and os.replace. The
    target file must be either the OLD content (from an existing
    baseline write) or absent — never truncated / partial."""
    user_path, _ = hermetic_config
    # Baseline: known-good content on disk.
    config_io.set_user_override(("qb",), "backend", "gemini")
    baseline = user_path.read_bytes()

    # Now simulate os.replace failing (out of space, cross-fs, etc.).
    replace_error = OSError("simulated disk full")
    with patch("os.replace", side_effect=replace_error), \
         pytest.raises(OSError, match="simulated"):
        config_io.set_user_override(("qb",), "backend", "openai")

    # Baseline preserved — no truncation.
    assert user_path.read_bytes() == baseline
    # Cleanup: no orphaned .tmp file lying around.
    orphans = list(user_path.parent.glob(".controller-*.toml.tmp"))
    assert orphans == []


def test_set_user_override_preserves_existing_fields(hermetic_config) -> None:
    """Round-trip: writing key A then key B must preserve both."""
    config_io.set_user_override(("qb",), "backend", "gemini")
    config_io.set_user_override(("verifier",), "votes", 3)

    user_path, _ = hermetic_config
    doc = config_io.read_toml(user_path)
    assert doc["qb"]["backend"] == "gemini"
    assert doc["verifier"]["votes"] == 3


def test_set_user_override_survives_scalar_at_section_slot(
    hermetic_config,
) -> None:
    """Edge case: a legacy config where a section slot holds a scalar
    (`qb = 42` instead of `[qb]`). Overwrite rather than
    AttributeError — the fix trades one obscure crash for one
    obscure data-loss, but the data-loss is limited to the offending
    scalar which was already unusable."""
    user_path, _ = hermetic_config
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text('qb = 42\n')  # legacy garbage

    # Must not raise.
    config_io.set_user_override(("qb",), "backend", "gemini")

    doc = config_io.read_toml(user_path)
    assert doc["qb"]["backend"] == "gemini"


def test_set_user_override_utf8_roundtrip(hermetic_config) -> None:
    """Non-ASCII in string values (e.g. a Chinese model name in a
    custom prompt) must round-trip through the atomic write intact."""
    config_io.set_user_override(("session",), "prompt_prefix", "冰破 →")

    user_path, _ = hermetic_config
    doc = config_io.read_toml(user_path)
    assert doc["session"]["prompt_prefix"] == "冰破 →"


# ── Dry-run gate ─────────────────────────────────────────────────────────


def test_restart_controller_dry_run_returns_success_without_pkexec(
    monkeypatch,
) -> None:
    """ICEBREAKER_DRY_RUN=1 must skip the subprocess entirely — so CI
    can hit the whole save+restart path without a polkit agent."""
    monkeypatch.setenv("ICEBREAKER_DRY_RUN", "1")
    called = False

    def _fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise RuntimeError("subprocess.run should not be called in dry-run")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ok, msg = config_io.restart_controller()
    assert ok is True
    assert "dry-run" in msg
    assert "pkexec" in msg
    assert called is False


def test_restart_controller_no_pkexec_returns_readable_error(
    monkeypatch,
) -> None:
    """Non-desktop installs may not have pkexec at all. Must return
    a clean (False, "pkexec not available.") — not crash."""
    monkeypatch.delenv("ICEBREAKER_DRY_RUN", raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("no pkexec")),
    )
    ok, msg = config_io.restart_controller()
    assert ok is False
    assert "pkexec" in msg.lower()


def test_restart_controller_timeout_returns_readable_error(
    monkeypatch,
) -> None:
    """User cancels polkit → timeout → clear message, not a raw
    TimeoutExpired traceback."""
    monkeypatch.delenv("ICEBREAKER_DRY_RUN", raising=False)

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ok, msg = config_io.restart_controller(timeout=0.1)
    assert ok is False
    assert "timed out" in msg.lower()


def test_restart_controller_non_zero_exit_surfaces_stderr(monkeypatch) -> None:
    """pkexec returning nonzero (systemctl failed, unit not found) must
    surface stderr so the operator knows *what* failed."""
    monkeypatch.delenv("ICEBREAKER_DRY_RUN", raising=False)

    class _FakeProc:
        returncode = 5
        stderr = "unit icebreaker-controller.service not found"
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc())
    ok, msg = config_io.restart_controller()
    assert ok is False
    assert "not found" in msg


# ── read_toml edge cases ─────────────────────────────────────────────────


def test_read_toml_missing_file_returns_empty(hermetic_config) -> None:
    """A fresh install has no user override — must return {} without
    raising. F-53 rules: this is a legitimately-fresh state, not an
    error to surface."""
    user_path, _ = hermetic_config
    result = config_io.read_toml(user_path)
    assert result == {}


def test_read_toml_corrupt_file_raises_config_read_error(
    hermetic_config,
) -> None:
    """A file that exists but doesn't parse must raise
    ConfigReadError — silent fallback to {} silently wipes user
    overrides on next save (this was the F-53 Scope A.P2 bug)."""
    user_path, _ = hermetic_config
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("this is [ not toml")
    with pytest.raises(config_io.ConfigReadError):
        config_io.read_toml(user_path)


def test_read_toml_empty_file_returns_empty_dict(hermetic_config) -> None:
    """An empty file (touched but never written) is a valid empty TOML
    document — must NOT raise."""
    user_path, _ = hermetic_config
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("")


# ── Additional hardening edge cases (2026-07-11) ─────────────────────────


def test_set_user_override_rejects_symlink_target(hermetic_config) -> None:
    """A symlink at the config path is an attack vector."""
    user_path, _ = hermetic_config
    user_path.parent.mkdir(parents=True, exist_ok=True)
    real = user_path.parent / "real.toml"
    real.write_text('[qb]\nbackend = "gemini"\n')
    user_path.symlink_to(real)

    with pytest.raises(RuntimeError, match="symlink"):
        config_io.set_user_override(("qb",), "backend", "openai")

    assert real.read_text() == '[qb]\nbackend = "gemini"\n'


def test_set_user_override_rejects_empty_section(hermetic_config) -> None:
    """Empty section tuple is a typo — top-level keys not supported."""
    with pytest.raises(ValueError, match="non-empty section"):
        config_io.set_user_override((), "backend", "gemini")


def test_set_user_override_large_string_round_trips(hermetic_config) -> None:
    """A 100KB value must round-trip through atomic write without loss."""
    big = "x" * (100 * 1024)
    config_io.set_user_override(("session",), "prompt_prefix", big)
    user_path, _ = hermetic_config
    doc = config_io.read_toml(user_path)
    assert doc["session"]["prompt_prefix"] == big


def test_set_user_override_special_chars_round_trip(hermetic_config) -> None:
    """Backslash, quote, newline preserved through TOML round-trip."""
    tricky = 'contains " and \\ and \n newline'
    config_io.set_user_override(("session",), "prompt_prefix", tricky)
    user_path, _ = hermetic_config
    doc = config_io.read_toml(user_path)
    assert doc["session"]["prompt_prefix"] == tricky

# ── effective() layer semantics ──────────────────────────────────────────


def test_effective_user_overrides_system() -> None:
    system = {"qb": {"backend": "openai"}}
    user = {"qb": {"backend": "gemini"}}
    assert config_io.effective(
        system, user, ("qb",), "backend", "local"
    ) == "gemini"


def test_effective_falls_back_to_system_when_user_missing() -> None:
    system = {"qb": {"backend": "openai"}}
    user: dict = {}
    assert config_io.effective(
        system, user, ("qb",), "backend", "local"
    ) == "openai"


def test_effective_falls_back_to_default_when_both_missing() -> None:
    assert config_io.effective(
        {}, {}, ("qb",), "backend", "local"
    ) == "local"


def test_effective_returns_none_when_walk_hits_non_dict() -> None:
    """If a section slot holds a scalar (legacy corruption), walk()
    returns None → effective() falls back."""
    system = {"qb": "not-a-dict"}
    assert config_io.effective(
        system, {}, ("qb",), "backend", "fallback"
    ) == "fallback"
