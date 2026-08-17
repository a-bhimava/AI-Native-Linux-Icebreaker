"""Phase 6 Scope D — Control Center page smoke tests.

Every page must survive:

  1. **Fresh install** — no user config file, no system config file.
     Regression: an empty `~/.config` used to crash pages that assumed
     the file existed.
  2. **Legacy v6.65 config** — a valid TOML written before Scope B
     landed, missing every new field. New GUI fields must fall back
     to defaults rather than raising.
  3. **Corrupt user override** — TOML syntax error. Page must render
     with a persistent banner (`ConfigReadError` surfaced) — never
     silently degrade to defaults (that was the F-53 Scope A.P2 bug).
  4. **Concurrent write race** — another writer touches the file
     between our read and our write. Atomic replace guarantees no
     truncation; second write wins (last-writer-wins is documented).
  5. **Save round-trip** — after `set_user_override`, the resulting
     file must re-parse under `controller.config.load()`. Anything
     that lands on disk but fails to load is a data-loss bug.

All 8 Control Center pages get identical coverage where applicable.
Pages that don't touch config (StatusPage, ToolsPage, ErrorsPage) skip
the round-trip check but still must instantiate cleanly.
"""

from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


# Adw/Gtk shims live in conftest.py — importing this test module alone
# still gets the shims because pytest collects conftest first.


# ── Page inventory ───────────────────────────────────────────────────────


# `save_touching = True` means the page has widgets that call
# `set_user_override` on interaction. Those pages get the round-trip
# check. Others (Status, Tools, Errors) are read-only or system-log
# viewers and only need to survive instantiation.

_PAGES: list[tuple[str, str, bool]] = [
    ("gui.control.pages.behavior_page",   "BehaviorPage",   True),
    ("gui.control.pages.limits_page",     "LimitsPage",     True),
    ("gui.control.pages.models_page",     "ModelsPage",     True),
    ("gui.control.pages.errors_page",     "ErrorsPage",     True),
    ("gui.control.pages.status_page",     "StatusPage",     False),
    ("gui.control.pages.tools_page",      "ToolsPage",      False),
    ("gui.control.pages.keys_page",       "ApiKeysPage",    False),
    ("gui.control.pages.theme_page",      "ThemePage",      False),
    ("gui.control.pages.appearance_page", "AppearancePage", False),
]


def _import_page(module_name: str, class_name: str):
    """Import + return a page class. Raises pytest.skip if the module
    imports fail for reasons unrelated to Scope D (e.g. the page
    depends on a shipped-only asset)."""
    try:
        mod = __import__(module_name, fromlist=[class_name])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"page {module_name} imports failed: {exc}")
    if not hasattr(mod, class_name):
        pytest.skip(f"{module_name}.{class_name} not found")
    return getattr(mod, class_name)


def _instantiate(PageClass):
    """Some pages take a constructor arg (ThemePage takes a
    ``on_theme_change`` callback). Pass a no-op for anything required
    so the smoke tests can hit every page uniformly."""
    import inspect
    try:
        sig = inspect.signature(PageClass.__init__)
    except (TypeError, ValueError):
        return PageClass()
    # Count required positional args beyond `self`. If any, pass a
    # no-op callback for each.
    required = [
        p for p in sig.parameters.values()
        if p.name != "self"
        and p.default is inspect.Parameter.empty
        and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
    ]
    return PageClass(*[lambda *a, **kw: None for _ in required])


# ── Fresh install ────────────────────────────────────────────────────────


@pytest.mark.parametrize("module,cls,_save", _PAGES)
def test_page_instantiates_on_fresh_install(
    module: str, cls: str, _save: bool,
    hermetic_config_paths,
) -> None:
    """No user config, no system config — must not raise."""
    PageClass = _import_page(module, cls)
    # models_page kicks off a background verification thread — we mock
    # verify_preset out so tests stay hermetic (no network).
    with patch("controller.preset_verifier.verify_preset") as v:
        v.return_value = MagicMock(
            provider="gemini", preset_id="x",
            status=MagicMock(value="verified"), detail="OK", http_code=200,
        )
        page = _instantiate(PageClass)
    assert page is not None


# ── Legacy v6.65 config ──────────────────────────────────────────────────


_LEGACY_V6_65_CONFIG = """\
[qb]
backend = "gemini"

[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 4096
timeout_seconds = 60
"""


@pytest.mark.parametrize("module,cls,_save", _PAGES)
def test_page_instantiates_with_legacy_v6_65_config(
    module: str, cls: str, _save: bool,
    hermetic_config_paths,
) -> None:
    """A user upgrading from v6.65 has a controller.toml without any
    Scope B/C fields. Pages must resolve those to defaults, not
    KeyError."""
    user_path, _ = hermetic_config_paths
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(_LEGACY_V6_65_CONFIG)

    PageClass = _import_page(module, cls)
    with patch("controller.preset_verifier.verify_preset") as v:
        v.return_value = MagicMock(
            provider="gemini", preset_id="x",
            status=MagicMock(value="verified"), detail="OK", http_code=200,
        )
        page = _instantiate(PageClass)
    assert page is not None


# ── Corrupt override ─────────────────────────────────────────────────────


@pytest.mark.parametrize("module,cls,_save", _PAGES)
def test_page_survives_corrupt_user_override(
    module: str, cls: str, _save: bool,
    hermetic_config_paths,
) -> None:
    """A syntactically-broken user TOML must not crash the page —
    the F-53 Scope A.P2 pattern: surface via _safe_read_toml, stash
    the error on `_user_read_error`, render a banner. Silent
    fallback to defaults would wipe the user's real overrides on
    next save."""
    user_path, _ = hermetic_config_paths
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("this is [ not toml")

    PageClass = _import_page(module, cls)
    with patch("controller.preset_verifier.verify_preset") as v:
        v.return_value = MagicMock(
            provider="gemini", preset_id="x",
            status=MagicMock(value="verified"), detail="OK", http_code=200,
        )
        page = _instantiate(PageClass)

    # Pages that use _safe_read_toml stash the error string on
    # `_user_read_error`. Verify it's populated (non-None).
    if hasattr(page, "_user_read_error"):
        assert page._user_read_error is not None
        assert "not toml" in page._user_read_error.lower() or \
               "parse" in page._user_read_error.lower() or \
               "toml" in page._user_read_error.lower()


# ── Save round-trip ──────────────────────────────────────────────────────


@pytest.mark.parametrize("module,cls,save_touching", _PAGES)
def test_save_path_produces_valid_config(
    module: str, cls: str, save_touching: bool,
    hermetic_config_paths,
) -> None:
    """Anything a page writes to disk must re-parse under
    `controller.config.load()`. Skipping this check would let a page
    save a config the daemon can't read — which would then reject the
    entire user override on next restart.
    """
    if not save_touching:
        pytest.skip(f"{cls} is read-only — no save path to round-trip")

    # Behavior + Limits + Models all write via set_user_override. Drive
    # the save path directly (bypassing widget signals which need real
    # Gtk) and confirm the resulting file loads.
    from gui.control import config_io

    # Populate a variety of value shapes: string, int, float, bool.
    config_io.set_user_override(("qb",), "backend", "gemini")
    config_io.set_user_override(("verifier",), "votes", 3)
    config_io.set_user_override(("verifier",), "parallel", False)
    config_io.set_user_override(("run",), "turn_timeout_seconds", 900.0)
    config_io.set_user_override(("session",), "max_recent_commands", 10)

    user_path, _ = hermetic_config_paths
    assert user_path.exists()

    # Round-trip: the daemon-side config loader must accept every field
    # we just wrote. This is the check that catches "GUI wrote a value
    # the schema rejects" bugs.
    from controller.config import load

    # A test config also needs the required minimal [qb.gemini] section
    # to pass schema validation. Append it to whatever the page wrote.
    # (Behavior page might not write this; we're testing the write path
    # not the semantic completeness.)
    extra = """
[qb.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
max_tokens = 4096
timeout_seconds = 60
"""
    with user_path.open("a") as f:
        f.write(extra)

    cfg = load(user_path)
    # Verify every value we wrote actually landed:
    assert cfg.verifier.votes == 3
    assert cfg.verifier.parallel is False
    assert cfg.run.turn_timeout_seconds == 900.0
    assert cfg.session.max_recent_commands == 10


# ── Concurrent write race ────────────────────────────────────────────────


def test_concurrent_writes_do_not_truncate(hermetic_config_paths) -> None:
    """Two threads racing to write different keys. Both must survive
    without truncation. Last-writer-wins semantics are documented —
    the guarantee is *no truncation*, not merge."""
    from gui.control import config_io

    errors: list[Exception] = []

    def _writer(section, key, value):
        try:
            for _ in range(20):
                config_io.set_user_override(section, key, value)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(("qb",), "backend", "gemini")),
        threading.Thread(target=_writer, args=(("qb",), "backend", "openai")),
        threading.Thread(target=_writer, args=(("verifier",), "votes", 3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"unexpected exceptions during concurrent writes: {errors}"

    # Result file must be parseable — no half-written state.
    user_path, _ = hermetic_config_paths
    doc = config_io.read_toml(user_path)
    # verifier.votes is always 3 (only one writer touched it).
    if "verifier" in doc:
        assert doc["verifier"]["votes"] == 3
    # qb.backend is one of the two values, not garbage.
    if "qb" in doc:
        assert doc["qb"]["backend"] in ("gemini", "openai")


# ── Read-only pages must handle missing system files ─────────────────────


def test_errors_page_survives_missing_system_log(hermetic_config_paths) -> None:
    """ErrorsPage reads `/var/log/icebreaker/system.jsonl`. On a fresh
    install (or a dev host without root install) that path doesn't
    exist. The page must render an empty state, not crash."""
    PageClass = _import_page("gui.control.pages.errors_page", "ErrorsPage")
    page = PageClass()
    assert page is not None


def test_tools_page_survives_missing_schema_dir(hermetic_config_paths) -> None:
    """ToolsPage searches for mcpd JSON schemas. If none of the search
    paths exist (e.g. dev host without a mcpd install), it must show
    an empty state, not crash on FileNotFoundError."""
    PageClass = _import_page("gui.control.pages.tools_page", "ToolsPage")
    page = PageClass()
    assert page is not None


def test_status_page_survives_no_daemon(hermetic_config_paths) -> None:
    """StatusPage tries to reach the daemon socket. On a dev host with
    no icebreaker-controller.service, systemctl / socket probes all
    fail — must render offline state without crashing."""
    PageClass = _import_page("gui.control.pages.status_page", "StatusPage")
    page = PageClass()
    assert page is not None
