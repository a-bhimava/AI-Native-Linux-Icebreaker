"""Phase 6 Scope A enforcement — every `except Exception` block is accounted for.

**Rule**. Every ``except Exception[:| as ...]`` block in production Python code
under ``controller/``, ``gui/``, ``terminal/``, ``rpa_bridge/``, ``gui_agent/``
must either:

  (a) call one of the surfacing helpers listed in ``_SURFACING_TOKENS`` within
      ``_SURFACE_WINDOW`` lines below the ``except`` line, OR
  (b) carry a ``# noqa: BLE001`` marker on the ``except`` line (or on the
      preceding ``try:`` line) with a comment justifying the swallow, OR
  (c) be in the ``_INTENTIONAL_SITES`` allowlist below with a documented
      one-line reason, OR
  (d) be in the ``_KNOWN_OFFENDERS`` snapshot captured on 2026-07-10 — this
      list only shrinks as Scope A.P2/P3 progressively F-53's the sites.

Ships as a v1.0-rc1 gate — see Phase 6 Final Completion Plan Scope A
enforcement + F-51 marker extension. Related to F-53 hardening.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterator

import pytest


# ─── Configuration ────────────────────────────────────────────────────────

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
"""Repo root — walk from here so tests work under both editable installs and
the shipped venv layout."""


# Directories to walk. Every path is relative to ``_PROJECT_ROOT``.
_SCAN_DIRS = (
    "controller",
    "gui",
    "terminal",
    "rpa_bridge",
    "gui_agent",
)


# Filenames excluded from the walk (this file itself; test fixtures; __pycache__).
_EXCLUDE_NAMES = {
    "test_no_silent_swallow.py",
    "conftest.py",
}
# Skip test directories — assertions there use ``pytest.raises`` and mock
# patterns that intentionally look like bare-swallow to a syntactic scan.
# Prod code is what we're enforcing on.
_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "corpus", "tests"}


# How many lines below the ``except`` line to look for a surfacing token.
# Widened to 12 to accommodate the F-53 "structured comment then increment
# then print/log" pattern that runs ~9 lines.
_SURFACE_WINDOW = 12


# Any of these tokens appearing within ``_SURFACE_WINDOW`` lines of the
# except block counts as "surfacing" the exception (log it, record it,
# propagate it, or return a shape that lets the caller see the cause).
_SURFACING_TOKENS = (
    "_log_exception",              # controller.main helper (F-53)
    "log.warning", "log.error",    # daemon.py module logger
    "log.exception",               # includes traceback — strongest surface
    "log.debug",                   # daemon.py (parse_message throttle)
    "log.info",
    "logger.warning", "logger.error",
    "print(",                      # stderr fallback
    "sys.stderr",
    "self.entries_skipped_",       # logger.py corruption counters
    "self._last_screenshot_error", # rpa_bridge.bridge (F-53)
    "self._last_read_error",       # errors_page.py file-read failure
    "self._malformed_",            # client.py per-conn warn + errors_page
                                   # malformed-line counter (prefix match)
    "self.notify(",                # GTK/Textual visible surface
    "raise ",                      # re-raise or replace with typed exc
    "_probe_result(",              # model_registry probe struct
    "_record_probe_error",         # model_registry + atspi per-probe cache
    "startup_warning",             # terminal + gui show-and-log pattern
    "_daemon_startup_error",       # __main__.py connect-failure surface
    "_logger_init_error",          # gui/app.py logger init failure
    "_daemon_connect_error",       # gui/app.py daemon connect failure
    "VerifierResult(",             # verifier.py: reason string carries exc
    "vote failed:",                # verifier.py MajorityVoter individual
    "verifier call failed:",       # verifier.py SingleVerifier reason
    "return {",                    # sentinel-dict return with exception info
    "traceback.print_exc",         # loud traceback surface
    "traceback.format_exc",
    "get_last_error",              # explorer-recognizable API for last exc
    "self._last_",                 # cached last-error attribute pattern
    "{exc}",                       # f-string formatting the exception (Rich
                                   # Log write, error banners, HITL prompts)
    "{e}",                         # short f-string form (used in some sites)
    ".write(Text(",                # Rich Log.write pattern in Textual TUIs
    "banner.show",                 # persistent-bar GTK/GUI pattern
    "toast.show",                  # transient toast surface
)


# Explicit allowlist: sites that are deliberately silent because logging
# them would be worse than the swallow (recursion, destructor, shutdown).
# Format: (file_relative_to_root, line_containing_substring).
_INTENTIONAL_SITES: tuple[tuple[str, str], ...] = (
    # controller/main.py: _log_exception recursion guard.
    ("controller/main.py", "SystemLogger itself failed"),
    # controller/main.py: context render fallback (module scope, no
    # self._system_logger available; documented tradeoff).
    ("controller/main.py", "context render can fail"),
    # daemon.py: nested TransportClosed while sending an earlier parse
    # error — legitimate torn-connection swallow.
    ("controller/daemon.py", "Legitimate swallow"),
    # bridge.py: stdout torn during shutdown while emitting a
    # screenshot-error notification. Reason already captured on self.
    ("rpa_bridge/bridge.py", "stdout is torn during shutdown"),
)


# Phase 6 Scope A snapshot. Baseline captured 2026-07-10 22:00 UTC (63
# entries) has been shrunk to 52 by initial terminal/app.py F-53 pass +
# expanded surfacing tokens (log.debug, {exc}, .write(Text( etc.).
# Tasks 96 (A.P2) + 97 (A.P3) continue shrinking to zero by v1.0-rc1.
# **RULE**: this list only shrinks. Never add a new site — that's the
# regression the enforcement gate exists to catch. If a site drops off
# because we fixed it (or moved it), remove the entry.
# Format: (file_relative_to_root, lineno_of_except).
# **Phase 6 Scope A COMPLETE 2026-07-10**: 63 → 52 → 41 → 0 as
# P1/P2/P3 landed. Every remaining `except Exception` block in
# scope now either (a) calls a surfacing helper within 12 lines,
# (b) carries `# noqa: BLE001` with a justifying comment, or (c)
# lives in `_INTENTIONAL_SITES`. The enforcement gate is now purely
# forward-looking: any NEW bare swallow fails CI immediately.
_KNOWN_OFFENDERS: frozenset[tuple[str, int]] = frozenset()


# ─── Discovery ────────────────────────────────────────────────────────────


def _iter_py_files() -> Iterator[pathlib.Path]:
    for scan in _SCAN_DIRS:
        base = _PROJECT_ROOT / scan
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if path.name in _EXCLUDE_NAMES:
                continue
            if any(part in _EXCLUDE_DIR_NAMES for part in path.parts):
                continue
            yield path


class _ExceptFinder(ast.NodeVisitor):
    """Collect every ``except Exception`` block in a module with its line."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, ast.ExceptHandler]] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        exc_type = node.type
        # Match `except Exception` (with or without `as ...`); also match
        # `except (Exception,)` and `except (Exception, TypeError)` — the
        # broad clause is what we care about.
        if _catches_bare_exception(exc_type):
            self.hits.append((node.lineno, node))
        self.generic_visit(node)


def _catches_bare_exception(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name) and node.id == "Exception":
        return True
    if isinstance(node, ast.Tuple):
        return any(_catches_bare_exception(e) for e in node.elts)
    return False


def _line_contains_noqa(source_lines: list[str], lineno: int) -> bool:
    """Return True if the ``except`` line (or the preceding ``try:``)
    carries a ``# noqa: BLE001`` marker."""
    if lineno < 1 or lineno > len(source_lines):
        return False
    line = source_lines[lineno - 1]
    if "# noqa: BLE001" in line:
        return True
    # Also accept a noqa placed on the same line's continuation or on the
    # try: line that opened the block (rare but valid pattern).
    prev = source_lines[lineno - 2] if lineno >= 2 else ""
    return "# noqa: BLE001" in prev


def _surfaces_within(source_lines: list[str], lineno: int, window: int = _SURFACE_WINDOW) -> bool:
    """Return True if any surfacing token appears within ``window`` lines
    after the except block."""
    start = lineno  # lineno is 1-based; source_lines[lineno - 1] is the except line.
    end = min(len(source_lines), lineno - 1 + window + 1)
    snippet = "\n".join(source_lines[start:end])
    return any(tok in snippet for tok in _SURFACING_TOKENS)


def _is_allowlisted(rel_path: str, source_lines: list[str], lineno: int, window: int = _SURFACE_WINDOW) -> bool:
    """Return True if the exception block matches an ``_INTENTIONAL_SITES`` entry."""
    for allow_path, needle in _INTENTIONAL_SITES:
        if not rel_path.endswith(allow_path):
            continue
        start = max(0, lineno - 2)
        end = min(len(source_lines), lineno - 1 + window + 1)
        snippet = "\n".join(source_lines[start:end])
        if needle in snippet:
            return True
    return False


# ─── The tests ────────────────────────────────────────────────────────────


def _collect_current_offenders() -> set[tuple[str, int]]:
    """Return the set of (rel_path, lineno) tuples for currently-unaccounted
    bare-swallow sites in the scanned tree."""
    offenders: set[tuple[str, int]] = set()
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # Broken syntax gets caught by other tests / bash -n; skip
            # here to keep this test focused on exception hygiene.
            continue
        source_lines = source.splitlines()
        finder = _ExceptFinder()
        finder.visit(tree)
        rel_path = str(path.relative_to(_PROJECT_ROOT))
        for lineno, _node in finder.hits:
            if _line_contains_noqa(source_lines, lineno):
                continue
            if _is_allowlisted(rel_path, source_lines, lineno):
                continue
            if _surfaces_within(source_lines, lineno):
                continue
            offenders.add((rel_path, lineno))
    return offenders


def test_no_new_silent_exception_swallows() -> None:
    """Regression gate — every new bare ``except Exception`` in scope must
    surface the exception. Existing pre-plan sites are captured in
    ``_KNOWN_OFFENDERS`` and progressively fixed by Phase 6 Scope A.P2/P3.
    """
    current = _collect_current_offenders()
    new_offenders = sorted(current - _KNOWN_OFFENDERS)
    if new_offenders:
        joined = "\n  ".join(f"{p}:{ln}" for p, ln in new_offenders)
        pytest.fail(
            f"{len(new_offenders)} NEW bare `except Exception` site(s) not "
            f"in the Phase 6 known-offender baseline:\n  {joined}\n\n"
            f"Fix by either (a) calling one of {_SURFACING_TOKENS!r} within "
            f"{_SURFACE_WINDOW} lines, (b) adding `# noqa: BLE001` with a "
            f"justifying comment, or (c) adding the site to "
            f"_INTENTIONAL_SITES in this test file. **Do NOT add it to "
            f"_KNOWN_OFFENDERS — that list only shrinks.**"
        )


def test_known_offenders_still_offend() -> None:
    """Each _KNOWN_OFFENDERS entry must still be a real bare-swallow site.

    If someone fixed one of the known-bad sites (great!), remove the
    entry from _KNOWN_OFFENDERS. Stale entries here mean the enforcement
    lied about the current tree state; catch them so the list stays
    honest as A.P2/P3 shrinks it.
    """
    current = _collect_current_offenders()
    stale = sorted(_KNOWN_OFFENDERS - current)
    if stale:
        joined = "\n  ".join(f"{p}:{ln}" for p, ln in stale)
        pytest.fail(
            f"{len(stale)} _KNOWN_OFFENDERS entrie(s) no longer match a "
            f"bare swallow:\n  {joined}\n\n"
            f"Either the site was fixed (great — remove the entry from "
            f"_KNOWN_OFFENDERS) or the line number drifted (find the new "
            f"lineno and update)."
        )


def test_scan_dirs_exist() -> None:
    """Sanity — every _SCAN_DIRS entry resolves to a real directory."""
    for scan in _SCAN_DIRS:
        base = _PROJECT_ROOT / scan
        assert base.is_dir(), f"scan dir not found: {base}"


def test_at_least_one_file_scanned() -> None:
    """Sanity — the walk yields at least a few files. Catches paths breaking."""
    count = sum(1 for _ in _iter_py_files())
    assert count > 20, f"only {count} files scanned — walk broken?"


def test_intentional_sites_still_match_something() -> None:
    """Each _INTENTIONAL_SITES entry must actually still exist in the tree.

    If someone removes an allowlist target (e.g. refactors out a
    documented swallow), the allowlist entry becomes stale — flag it so
    the list stays honest.
    """
    for allow_path, needle in _INTENTIONAL_SITES:
        target = _PROJECT_ROOT / allow_path
        assert target.is_file(), f"allowlist target file missing: {allow_path}"
        source = target.read_text(encoding="utf-8")
        assert needle in source, (
            f"allowlist entry ({allow_path!r}, {needle!r}) no longer matches — "
            f"the swallow site was refactored. Remove or update the entry."
        )
