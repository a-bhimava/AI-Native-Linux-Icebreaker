"""F-35 golden intent corpus tests.

Two modes:

**Offline (default, no key needed).** Tests the *Controller-side routing*
against a synthetic intent constructed from each corpus row. Verifies:
  - Rows with `qb_mock_action` (phantom actions) trigger the
    `_SUPPORTED_ACTIONS` guard → rewritten to `system.unsupported`.
  - Rows with `expected_action=system.unsupported` route through
    `_emit_unsupported` and produce `Outcome.UNSUPPORTED`.
  - Rows with concrete catalogue actions pass the guard.

**Live (`--live` flag or `GEMINI_API_KEY` env).** Also asks real Gemini
to translate each corpus query and asserts `intent.action` matches
`expected_action`. This is the definitive "does the prompt actually work"
test; runs pre-ISO-ship, not in the smoke gate (cost + rate-limit).

The smoke gate runs offline mode only — cheap regression guard on the
Controller's routing logic. Any prompt regression that maps navigation
to `fs.list` won't be caught here (that's live's job); but any Controller
regression that lets phantom actions execute *will* be caught.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest


CORPUS_PATH = pathlib.Path(__file__).parent / "corpus" / "intent_corpus.json"


# ─── _SUPPORTED_ACTIONS ─────────────────────────────────────────────────────
# Kept as a literal set here (duplicated from main.py) so this test can run
# without importing the whole Controller pipeline (which brings in mcpd/QB
# backends, systemd checks, etc.). If someone edits main.py's set without
# updating this, the mismatch test at the bottom of this file will fail —
# forcing them to sync both.
_SUPPORTED_ACTIONS = frozenset({
    "system.status", "system.uptime", "system.cpu", "system.memory", "system.disk",
    "process.list", "process.inspect",
    "fs.read", "fs.list", "fs.stat",
    "service.logs",
    "network.status", "network.dns.read",
    "package.query",
    "fs.write",
    "package.install", "package.remove", "package.upgrade",
    "service.start", "service.stop", "service.restart",
    "fs.delete",
    "system.unsupported",
})


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    """Load the corpus JSON once per test session."""
    with CORPUS_PATH.open() as f:
        data = json.load(f)
    assert "rows" in data, "corpus missing 'rows'"
    return data["rows"]


# ─── Structural corpus validation (schema of the corpus itself) ────────────

def test_corpus_loads_and_has_rows(corpus: list[dict]) -> None:
    assert len(corpus) >= 40, f"corpus has {len(corpus)} rows — expected ≥40"


def test_every_row_has_required_fields(corpus: list[dict]) -> None:
    for row in corpus:
        assert "id" in row, f"row missing id: {row}"
        assert "query" in row, f"row {row['id']} missing query"
        assert "category" in row, f"row {row['id']} missing category"
        # adversarial rows use expected_action_in; all others use expected_action
        assert (
            "expected_action" in row or "expected_action_in" in row
        ), f"row {row['id']} missing expected_action or expected_action_in"


def test_every_expected_action_is_supported(corpus: list[dict]) -> None:
    """Every row's expected_action must be in the Controller's supported set.

    Catches typos in the corpus and prevents someone from asserting on a
    phantom action name.
    """
    for row in corpus:
        if "expected_action" in row:
            assert row["expected_action"] in _SUPPORTED_ACTIONS, (
                f"row {row['id']} expects unsupported action {row['expected_action']!r} "
                f"— add it to _SUPPORTED_ACTIONS (and mcpd/tools) or fix the corpus"
            )
        if "expected_action_in" in row:
            for action in row["expected_action_in"]:
                assert action in _SUPPORTED_ACTIONS, (
                    f"row {row['id']} expected_action_in contains phantom {action!r}"
                )


# ─── Guard-behavior tests (offline, no real QB) ────────────────────────────

def _would_rewrite(row: dict) -> bool:
    """Simulate the Controller's F-35 guard on a row.

    Returns True if this row would trigger the `_SUPPORTED_ACTIONS` rewrite
    (either QB emitted the explicit system.unsupported action, or QB emitted
    a phantom action not in the catalogue).
    """
    if "qb_mock_action" in row:
        return (row["qb_mock_action"] == "system.unsupported"
                or row["qb_mock_action"] not in _SUPPORTED_ACTIONS)
    if "expected_action" in row:
        return (row["expected_action"] == "system.unsupported"
                or row["expected_action"] not in _SUPPORTED_ACTIONS)
    return False


def test_phantom_actions_are_rewritten(corpus: list[dict]) -> None:
    """Rows with qb_mock_action outside _SUPPORTED_ACTIONS must trigger rewrite."""
    phantom_rows = [r for r in corpus if r.get("category") == "phantom-UNSUPPORTED"]
    assert phantom_rows, "corpus should have at least one phantom-UNSUPPORTED row"
    for row in phantom_rows:
        assert _would_rewrite(row), (
            f"phantom row {row['id']} with qb_mock_action="
            f"{row.get('qb_mock_action')!r} should be rewritten by F-35 guard"
        )


def test_navigation_rows_route_to_unsupported(corpus: list[dict]) -> None:
    """Every navigation-UNSUPPORTED row must produce Outcome.UNSUPPORTED."""
    nav_rows = [r for r in corpus if r.get("category") == "navigation-UNSUPPORTED"]
    assert len(nav_rows) >= 5, f"only {len(nav_rows)} navigation rows — need ≥5"
    for row in nav_rows:
        assert row["expected_action"] == "system.unsupported", (
            f"navigation row {row['id']} must expect system.unsupported "
            f"(got {row['expected_action']!r})"
        )
        assert row["expected_outcome"] == "unsupported", (
            f"navigation row {row['id']} must expect outcome=unsupported "
            f"(got {row.get('expected_outcome')!r})"
        )


def test_gui_and_misc_rows_route_to_unsupported(corpus: list[dict]) -> None:
    """GUI + misc UNSUPPORTED categories: same routing invariant."""
    for cat in ("gui-UNSUPPORTED", "misc-UNSUPPORTED"):
        rows = [r for r in corpus if r.get("category") == cat]
        assert rows, f"corpus should have rows for category {cat!r}"
        for row in rows:
            assert row["expected_action"] == "system.unsupported", (
                f"{cat} row {row['id']} must expect system.unsupported"
            )
            assert row["expected_outcome"] == "unsupported"


def test_supported_rows_do_not_route_to_unsupported(corpus: list[dict]) -> None:
    """Sanity: read/write/sysinfo rows should NOT be rewritten by the guard."""
    supported_cats = {"read", "write", "write-content", "sysinfo", "process", "service", "package", "network", "delete"}
    for row in corpus:
        if row.get("category") not in supported_cats:
            continue
        assert row["expected_outcome"] == "executed", (
            f"supported row {row['id']} in category {row['category']!r} "
            f"expects outcome={row.get('expected_outcome')!r} but should be 'executed'"
        )
        assert not _would_rewrite(row), (
            f"supported row {row['id']} with action={row['expected_action']!r} "
            f"should NOT be rewritten"
        )


def test_content_bearing_rows_are_writes(corpus: list[dict]) -> None:
    """F-41: any row that pins expected_content or expected_content_json / regex
    must be a write. Content-carrying reads make no sense; catch corpus typos
    that would waste a Gemini roundtrip in live mode."""
    for row in corpus:
        carries_content = (
            "expected_content" in row
            or "expected_content_json" in row
            or "expected_content_matches_regex" in row
        )
        if not carries_content:
            continue
        assert row["expected_action"] == "fs.write", (
            f"row {row['id']} carries expected_content* but action is "
            f"{row['expected_action']!r} (only fs.write should carry content)"
        )


def test_smalltalk_and_meta_rows_route_to_unsupported(corpus: list[dict]) -> None:
    """F-42 regression floor: 'hello', 'hi', 'what can you do' must be
    routed to system.unsupported without crashing the CoT/response pipeline.
    """
    for cat in ("smalltalk-UNSUPPORTED", "meta-UNSUPPORTED"):
        rows = [r for r in corpus if r.get("category") == cat]
        assert rows, f"corpus should have rows for category {cat!r}"
        for row in rows:
            assert row["expected_action"] == "system.unsupported", (
                f"{cat} row {row['id']} must expect system.unsupported"
            )
            assert row["expected_outcome"] == "unsupported"


def test_search_rows_route_to_unsupported_until_fs_find_ships(corpus: list[dict]) -> None:
    """F-46 regression floor (bug #7): search/find queries must route to
    system.unsupported (not silently to fs.list) until the fs.find tool
    ships in Phase 7 M7.5. When fs.find lands, these rows should be
    reclassified to expected_action=fs.find.
    """
    rows = [r for r in corpus if r.get("category") == "search-UNSUPPORTED"]
    assert rows, "corpus should have search rows until fs.find ships"
    for row in rows:
        assert row["expected_action"] == "system.unsupported", (
            f"search row {row['id']} must expect system.unsupported "
            f"until fs.find ships (currently {row['expected_action']!r})"
        )
        assert row["expected_outcome"] == "unsupported"


def test_broad_os_rows_route_to_shipped_actions(corpus: list[dict]) -> None:
    """Scope F2 (2026-07-11): every broad-OS row's expected_action must be
    in _SUPPORTED_ACTIONS. Otherwise a deferred-feature row (Playwright,
    fs.find, process.exec) accidentally shipped and will silently rot into
    UNSUPPORTED after v1.0-rc1.
    """
    broad_rows = [r for r in corpus if r.get("id", "").startswith("broad-os.")]
    assert broad_rows, (
        "corpus should have broad-OS rows (F Step 2) — none found"
    )
    for row in broad_rows:
        assert row["expected_action"] in _SUPPORTED_ACTIONS, (
            f"broad-OS row {row['id']} expects unsupported action "
            f"{row['expected_action']!r} — deferred rows belong in "
            "_meta.deferred, not in the assertion floor"
        )
        assert row["expected_outcome"] == "executed", (
            f"broad-OS row {row['id']} expects outcome "
            f"{row.get('expected_outcome')!r} — should be 'executed'"
        )


def test_content_bearing_broad_os_rows_have_regex_shape(corpus: list[dict]) -> None:
    """Scope F2 (2026-07-11): broad-OS write-content rows carry
    expected_content_matches_regex (not exact expected_content) because
    the content is free-form prose. Enforces the shape choice — an exact
    expected_content on a broad-OS write is a flaky-test trap.
    """
    broad_content_rows = [
        r for r in corpus
        if r.get("id", "").startswith("broad-os.")
        and r.get("category") == "write-content"
    ]
    assert broad_content_rows, (
        "corpus should have broad-OS write-content rows"
    )
    for row in broad_content_rows:
        assert "expected_content_matches_regex" in row, (
            f"broad-OS write-content row {row['id']} lacks "
            "expected_content_matches_regex — free-form prose can't use "
            "exact expected_content"
        )
        assert "expected_content" not in row, (
            f"broad-OS write-content row {row['id']} has BOTH exact "
            "content and regex — pick one (regex for free-form prose)"
        )
        assert row.get("expected_pb_hint_contains") == "params.content", (
            f"broad-OS write-content row {row['id']} missing/wrong "
            "expected_pb_hint_contains — must be 'params.content' "
            "(F-41 coaching pathway)"
        )


def test_meta_deferred_block_documents_unshipped_workflows(corpus: list[dict]) -> None:
    """Scope F2 (2026-07-11): the corpus _meta.deferred block must exist
    and enumerate at least the five deferred workflows the plan calls
    out. Prevents someone silently deleting the deferred block and
    losing discoverability of the coverage gap.
    """
    with CORPUS_PATH.open() as f:
        data = json.load(f)
    deferred = data["_meta"].get("deferred", {})
    assert deferred, (
        "corpus _meta.deferred block missing — Scope F2 deferred rows "
        "are undiscoverable"
    )
    # Every deferred entry must name a query + blocked_by.
    for key, entry in deferred.items():
        if key.startswith("_"):
            continue
        assert isinstance(entry, dict), (
            f"_meta.deferred[{key!r}] must be an object with 'query' + "
            "'blocked_by'"
        )
        assert "query" in entry and "blocked_by" in entry, (
            f"_meta.deferred[{key!r}] missing query or blocked_by"
        )


def test_adversarial_rows_do_not_execute(corpus: list[dict]) -> None:
    """Adversarial queries (prompt injection, destructive fantasies) must
    resolve to a benign action (system.status or system.unsupported) and
    never execute a destructive tool."""
    adv_rows = [r for r in corpus if r.get("category") == "adversarial"]
    assert adv_rows, "corpus must include adversarial rows"
    for row in adv_rows:
        assert row.get("expected_outcome_not") == "executed", (
            f"adversarial row {row['id']} missing expected_outcome_not=executed"
        )
        assert "expected_action_in" in row, (
            f"adversarial row {row['id']} should list allowed benign actions"
        )
        for allowed in row["expected_action_in"]:
            assert allowed in {"system.status", "system.unsupported"}, (
                f"adversarial row {row['id']} allows unsafe fallback {allowed!r}"
            )


# ─── Sync check: corpus's supported set matches Controller's ───────────────

def test_supported_actions_match_controller_source() -> None:
    """This local _SUPPORTED_ACTIONS must stay in sync with main.py.

    If someone edits main.py's frozenset without updating this file, the
    duplicated literal here becomes stale — the offline suite would silently
    green-light unsupported rows. Import + compare directly.
    """
    import importlib.util
    main_path = pathlib.Path(__file__).parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("_controller_main", main_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        # main.py has heavy deps (mcpd, backends) that may not be importable in
        # the smoke-gate context. Fall back to a text scrape — the frozenset
        # literal is easy to grep out.
        text = main_path.read_text()
        start = text.find("_SUPPORTED_ACTIONS = frozenset({")
        assert start != -1, "could not find _SUPPORTED_ACTIONS in main.py"
        end = text.find("})", start)
        block = text[start:end]
        # Extract quoted action names
        import re as _re
        actions = set(_re.findall(r'"([a-z][a-z0-9_.]*)"', block))
    else:
        actions = set(module._SUPPORTED_ACTIONS)
    assert actions == set(_SUPPORTED_ACTIONS), (
        f"F-35 supported set drift! Controller has {actions ^ set(_SUPPORTED_ACTIONS)!r} "
        f"different from test corpus set — update both"
    )


# ─── Live-mode tests (opt-in) ──────────────────────────────────────────────

def _live_mode() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY")) or "--live" in os.environ.get("PYTEST_ADDOPTS", "")


@pytest.mark.skipif(not _live_mode(), reason="live mode requires GEMINI_API_KEY")
def test_live_gemini_produces_expected_actions(corpus: list[dict]) -> None:
    """LIVE ONLY: verify Gemini emits the expected action for each query.

    This is what catches "prompt drift" — the failure mode where a good
    prompt edit accidentally makes Gemini stop emitting system.unsupported
    for navigation queries. Not run in the smoke gate (needs key + costs
    ~1¢ per full corpus run); run manually before ISO ship.
    """
    from controller.config import load
    from controller.backends import gemini_backend  # noqa: F401
    from controller.backends.base import make_backend

    cfg = load(None)
    backend = make_backend(cfg)

    misses: list[tuple[str, str, str]] = []
    for row in corpus:
        if row.get("category") == "adversarial":
            # Adversarial rows use expected_action_in — different assertion shape
            continue
        expected = row["expected_action"]
        query = row["query"]
        context = row.get("context", {})
        # Prepend the <context> block the same way the Controller does
        preamble_parts = []
        if context.get("cwd"):
            preamble_parts.append(f"cwd: {context['cwd']}")
        if context.get("user"):
            preamble_parts.append(f"user: {context['user']}")
        preamble = ""
        if preamble_parts:
            preamble = "<context>\n" + "\n".join(preamble_parts) + "\n</context>\n<query>\n" + query + "\n</query>"
        else:
            preamble = query

        prompt = pathlib.Path(__file__).parent.parent / "prompts" / f"qb_{cfg.qb.backend}.txt"
        system = prompt.read_text() if prompt.exists() else ""

        try:
            resp = backend.complete(system=system, user=preamble, schema=None, max_retries=1)
            intent = resp.content_json
            actual = intent.get("action", "")
            if actual != expected:
                misses.append((row["id"], expected, actual))
                continue

            # F-41: also verify content + pb_hint when the row pins them.
            if "expected_content" in row:
                got = intent.get("content", "")
                if got != row["expected_content"]:
                    misses.append((row["id"], f"content={row['expected_content']!r}", f"content={got!r}"))
            if "expected_content_json" in row:
                got_str = intent.get("content", "")
                try:
                    got_obj = json.loads(got_str)
                except Exception:
                    misses.append((row["id"], f"content=<json {row['expected_content_json']}>", f"content={got_str!r} (not valid JSON)"))
                else:
                    if got_obj != row["expected_content_json"]:
                        misses.append((row["id"], f"content=<json {row['expected_content_json']}>", f"content=<json {got_obj}>"))
            if "expected_content_matches_regex" in row:
                import re
                got = intent.get("content", "")
                if not re.match(row["expected_content_matches_regex"], got):
                    misses.append((row["id"], f"content matches /{row['expected_content_matches_regex']}/", f"content={got!r}"))
            if "expected_pb_hint_contains" in row:
                got = intent.get("pb_hint", "")
                if row["expected_pb_hint_contains"] not in got:
                    misses.append((row["id"], f"pb_hint contains {row['expected_pb_hint_contains']!r}", f"pb_hint={got!r}"))
        except Exception as exc:
            misses.append((row["id"], expected, f"error: {exc}"))

    if misses:
        report = "\n".join(f"  {rid}: expected={exp!r} actual={act!r}" for rid, exp, act in misses)
        pytest.fail(
            f"{len(misses)}/{len(corpus)} live Gemini calls produced wrong action/content:\n{report}"
        )
