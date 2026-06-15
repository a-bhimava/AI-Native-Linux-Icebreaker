"""Interactive terminal audit log viewer for the Icebreaker Controller.

Two access paths:
  1. REPL:  /audit — filtered to current session
  2. CLI:   python -m controller.audit_viewer [--verify] [--json] [PATH]

Features:
  - Lazy-loaded byte-offset index (handles 100K+ entries, ~1.6 MB index)
  - Progressive disclosure: summary table → detail view on Enter
  - Filter by session, tier, outcome, action, backend, time range
  - Regex search across non-redacted fields
  - Hash-chain verification with per-entry status
  - Statistics view (outcome/tier distribution, cost, token totals)
  - $NO_COLOR + ASCII fallback (BP-3, BP-11)
  - Non-TTY: JSONL dump to stdout (scripting-friendly)
"""

from __future__ import annotations

import json
import os
import re
import select
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .audit import (
    GENESIS_HASH,
    REDACTED_PLACEHOLDER,
    REQUIRED_FIELDS,
    AuditLog,
    _canonical_line,
    _hash_line,
)
from .hitl import (
    _BOLD,
    _DIM,
    _GREEN,
    _RED,
    _RESET,
    _YELLOW,
    _ascii_safe,
    _cbreak,
    _colors_enabled,
    _g,
    _sanitize_display,
)


# ── Data layer ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LogIndex:
    """Lightweight byte-offset index for lazy-loading audit log entries."""

    path: Path
    offsets: tuple[tuple[int, int], ...]
    total_entries: int
    file_size: int
    malformed_lines: tuple[int, ...]

    @classmethod
    def build(cls, path: Path) -> "LogIndex":
        offsets: list[tuple[int, int]] = []
        malformed: list[int] = []
        file_size = path.stat().st_size
        with open(path, "rb") as f:
            line_num = 0
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                stripped = line.rstrip(b"\n\r")
                if not stripped:
                    line_num += 1
                    continue
                try:
                    json.loads(stripped)
                    offsets.append((offset, len(stripped)))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    malformed.append(line_num)
                line_num += 1
        return cls(
            path=path,
            offsets=tuple(offsets),
            total_entries=len(offsets),
            file_size=file_size,
            malformed_lines=tuple(malformed),
        )

    def read_entry(self, index: int) -> Optional[dict]:
        if index < 0 or index >= self.total_entries:
            return None
        offset, length = self.offsets[index]
        try:
            with open(self.path, "rb") as f:
                f.seek(offset)
                raw = f.read(length)
            return json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def read_page(self, start: int, count: int) -> list[tuple[int, Optional[dict]]]:
        result: list[tuple[int, Optional[dict]]] = []
        for i in range(start, min(start + count, self.total_entries)):
            result.append((i, self.read_entry(i)))
        return result


@dataclass(frozen=True)
class ChainStatus:
    """Result of hash-chain verification."""

    ok: bool
    first_bad_seq: Optional[int]
    total_entries: int
    verified_count: int
    entry_status: dict[int, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class FilterSpec:
    """Immutable filter specification."""

    session_id: Optional[str] = None
    tier: Optional[int] = None
    outcome: Optional[str] = None
    action: Optional[str] = None
    backend: Optional[str] = None
    time_after: Optional[str] = None
    time_before: Optional[str] = None
    search_pattern: Optional[re.Pattern] = None  # type: ignore[type-arg]

    def matches(self, entry: dict) -> bool:
        if self.session_id and entry.get("session_id", "") != self.session_id:
            return False
        if self.tier is not None and entry.get("tier") != self.tier:
            return False
        if self.outcome and entry.get("outcome", "") != self.outcome:
            return False
        if self.action and entry.get("action", "") != self.action:
            return False
        if self.backend and entry.get("backend", "") != self.backend:
            return False
        ts = entry.get("ts", "")
        if self.time_after and ts < self.time_after:
            return False
        if self.time_before and ts > self.time_before:
            return False
        if self.search_pattern:
            found = False
            for v in entry.values():
                if isinstance(v, str) and v != REDACTED_PLACEHOLDER:
                    if self.search_pattern.search(v):
                        found = True
                        break
            if not found:
                return False
        return True

    def describe(self) -> str:
        parts: list[str] = []
        if self.session_id:
            sid = self.session_id
            parts.append(f"session={sid[:8]}…" if len(sid) > 8 else f"session={sid}")
        if self.tier is not None:
            parts.append(f"tier={self.tier}")
        if self.outcome:
            parts.append(f"outcome={self.outcome}")
        if self.action:
            parts.append(f"action={self.action}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.time_after:
            parts.append(f"after={self.time_after}")
        if self.time_before:
            parts.append(f"before={self.time_before}")
        if self.search_pattern:
            parts.append(f"search=/{self.search_pattern.pattern}/")
        return " ".join(parts) if parts else "none"

    def is_empty(self) -> bool:
        return not any([
            self.session_id, self.tier is not None, self.outcome,
            self.action, self.backend, self.time_after, self.time_before,
            self.search_pattern,
        ])


# ── View modes / actions ─────────────────────────────────────────────────────


class ViewMode(Enum):
    SUMMARY = "summary"
    DETAIL = "detail"
    HELP = "help"
    STATS = "stats"


class ViewerAction(Enum):
    UP = "up"
    DOWN = "down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    HOME = "home"
    END = "end"
    SELECT = "select"
    BACK = "back"
    FILTER = "filter"
    SEARCH = "search"
    VERIFY = "verify"
    STATS = "stats"
    HELP = "help"
    QUIT = "quit"


_VIEWER_KEYS: dict[ViewerAction, tuple[str, ...]] = {
    ViewerAction.UP:        ("k",),
    ViewerAction.DOWN:      ("j",),
    ViewerAction.PAGE_UP:   ("K", "b"),
    ViewerAction.PAGE_DOWN: ("J", " "),
    ViewerAction.HOME:      ("g",),
    ViewerAction.END:       ("G",),
    ViewerAction.SELECT:    ("\r", "\n"),
    ViewerAction.BACK:      ("q",),
    ViewerAction.FILTER:    ("f",),
    ViewerAction.SEARCH:    ("/",),
    ViewerAction.VERIFY:    ("v",),
    ViewerAction.STATS:     ("s",),
    ViewerAction.HELP:      ("?",),
    ViewerAction.QUIT:      ("Q",),
}

_KEY_TO_ACTION: dict[str, ViewerAction] = {}
for _act, _keys in _VIEWER_KEYS.items():
    for _k in _keys:
        _KEY_TO_ACTION[_k] = _act


def _viewer_lookup(key: str) -> Optional[ViewerAction]:
    if key == "\x1b":
        return ViewerAction.BACK
    return _KEY_TO_ACTION.get(key)


# ── Tier labels ──────────────────────────────────────────────────────────────

_TIER_LABELS = {0: "READ_ONLY", 1: "LOW", 2: "MEDIUM", 3: "HIGH"}


# ── AuditViewer ──────────────────────────────────────────────────────────────


class AuditViewer:
    """Interactive terminal audit log viewer."""

    _CHROME_LINES = 7

    def __init__(
        self,
        path: Path,
        *,
        verify: bool = False,
        filter_spec: Optional[FilterSpec] = None,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        self._verify_on_open = verify
        self._color = _colors_enabled()
        self._ascii = _ascii_safe()

        self._index: Optional[LogIndex] = None
        self._filter_spec: Optional[FilterSpec] = filter_spec
        self._filtered_indices: Optional[list[int]] = None
        self._cursor: int = 0
        self._mode: ViewMode = ViewMode.SUMMARY
        self._chain_status: Optional[ChainStatus] = None

    # ── Public ────────────────────────────────────────────────────────────

    def run(self) -> int:
        if not self._path.exists():
            print(f"Error: file not found: {self._path}", file=sys.stderr)
            return 2

        try:
            self._index = LogIndex.build(self._path)
        except PermissionError:
            print(f"Error: permission denied: {self._path}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

        if not sys.stdin.isatty():
            return self._dump_json()

        if self._filter_spec and not self._filter_spec.is_empty():
            self._filtered_indices = self._apply_filter(self._filter_spec)

        if self._verify_on_open:
            self._chain_status = self._verify_chain()
            if not self._chain_status.ok:
                print(
                    f"Warning: chain tampered at seq {self._chain_status.first_bad_seq}",
                    file=sys.stderr,
                )

        if self._index.total_entries == 0:
            self._clear_screen()
            print(self._render_header())
            print(f"\n  No entries found in {self._path.name}\n")
            print(f"  Press {_g('fail') or 'q'} to quit.")
            self._wait_any_key()
            return 0

        try:
            with _cbreak(sys.stdin):
                while True:
                    self._clear_screen()
                    print(self._render(), end="", flush=True)
                    key = self._read_key()
                    if key is None:
                        continue
                    if not self._handle_key(key):
                        break
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print()

        if self._chain_status and not self._chain_status.ok:
            return 1
        return 0

    # ── Index / filtering ─────────────────────────────────────────────────

    def _apply_filter(self, spec: FilterSpec) -> list[int]:
        assert self._index is not None
        matched: list[int] = []
        for i in range(self._index.total_entries):
            entry = self._index.read_entry(i)
            if entry and spec.matches(entry):
                matched.append(i)
        return matched

    def _prompt_filter(self) -> Optional[FilterSpec]:
        print("\n" + self._render_header())
        print("\n  Filter (press Enter to skip any field):\n")
        try:
            sid = input("  Session ID [empty=any]: ").strip() or None
            tier_s = input("  Tier [0-3, empty=any]: ").strip()
            tier = int(tier_s) if tier_s else None
            if tier is not None and tier not in (0, 1, 2, 3):
                print("  Invalid tier (must be 0-3).")
                return None
            outcome = input("  Outcome [empty=any]: ").strip() or None
            action = input("  Action [empty=any]: ").strip() or None
            backend = input("  Backend [empty=any]: ").strip() or None
            after = input("  After [ISO-8601, empty=any]: ").strip() or None
            before = input("  Before [ISO-8601, empty=any]: ").strip() or None
        except (EOFError, KeyboardInterrupt):
            return None

        spec = FilterSpec(
            session_id=sid, tier=tier, outcome=outcome,
            action=action, backend=backend,
            time_after=after, time_before=before,
        )
        if spec.is_empty():
            return spec
        return spec

    def _prompt_search(self) -> Optional[re.Pattern]:  # type: ignore[type-arg]
        try:
            pattern_str = input("\n  Search [regex]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not pattern_str:
            return None
        try:
            return re.compile(pattern_str, re.IGNORECASE)
        except re.error as e:
            print(f"  Invalid regex: {e}")
            self._wait_any_key()
            return None

    # ── Verification ──────────────────────────────────────────────────────

    def _verify_chain(self) -> ChainStatus:
        assert self._index is not None
        entry_status: dict[int, bool] = {}
        prev_hash = GENESIS_HASH
        first_bad: Optional[int] = None
        verified = 0

        for i in range(self._index.total_entries):
            entry = self._index.read_entry(i)
            if entry is None:
                entry_status[i] = False
                if first_bad is None:
                    first_bad = i
                continue

            seq = entry.get("seq")
            eprev = entry.get("prev_hash")

            if seq is None or eprev is None or seq != i or eprev != prev_hash:
                entry_status[i] = False
                if first_bad is None:
                    first_bad = i
                prev_hash = _hash_line(_canonical_line(entry))
            else:
                entry_status[i] = True
                prev_hash = _hash_line(_canonical_line(entry))

            verified += 1

        return ChainStatus(
            ok=(first_bad is None),
            first_bad_seq=first_bad,
            total_entries=self._index.total_entries,
            verified_count=verified,
            entry_status=entry_status,
        )

    # ── Statistics ────────────────────────────────────────────────────────

    def _compute_stats(self) -> dict[str, Any]:
        assert self._index is not None
        indices = self._filtered_indices if self._filtered_indices is not None else list(range(self._index.total_entries))
        outcomes: dict[str, int] = {}
        tiers: dict[int, int] = {}
        backends: dict[str, int] = {}
        sessions: set[str] = set()
        total_cost = 0.0
        total_tokens_in = 0
        total_tokens_out = 0
        first_ts = ""
        last_ts = ""

        for i in indices:
            entry = self._index.read_entry(i)
            if not entry:
                continue
            o = entry.get("outcome", "unknown")
            outcomes[o] = outcomes.get(o, 0) + 1
            t = entry.get("tier", -1)
            tiers[t] = tiers.get(t, 0) + 1
            b = entry.get("backend", "unknown")
            backends[b] = backends.get(b, 0) + 1
            sessions.add(entry.get("session_id", ""))
            total_cost += entry.get("cost_estimate_usd", 0.0)
            total_tokens_in += entry.get("tokens_in", 0)
            total_tokens_out += entry.get("tokens_out", 0)
            ts = entry.get("ts", "")
            if not first_ts or ts < first_ts:
                first_ts = ts
            if not last_ts or ts > last_ts:
                last_ts = ts

        return {
            "total": len(indices),
            "filtered": len(indices) != self._index.total_entries,
            "all_total": self._index.total_entries,
            "outcomes": dict(sorted(outcomes.items(), key=lambda x: -x[1])),
            "tiers": dict(sorted(tiers.items())),
            "backends": dict(sorted(backends.items(), key=lambda x: -x[1])),
            "sessions": len(sessions),
            "total_cost": total_cost,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "first_ts": first_ts,
            "last_ts": last_ts,
        }

    # ── Rendering ─────────────────────────────────────────────────────────

    def _render(self) -> str:
        if self._mode == ViewMode.SUMMARY:
            return self._render_summary()
        elif self._mode == ViewMode.DETAIL:
            return self._render_detail_current()
        elif self._mode == ViewMode.HELP:
            return self._render_help()
        elif self._mode == ViewMode.STATS:
            return self._render_stats()
        return ""

    def _render_header(self) -> str:
        c = self._color
        name = self._path.name
        total = self._index.total_entries if self._index else 0
        dline = _g("dline") * 72 if not self._ascii else "=" * 72
        title = f"Icebreaker Audit Viewer {_g('line')} {name} ({total} entries)"
        if c:
            return f"  {_BOLD}{title}{_RESET}\n  {dline}"
        return f"  {title}\n  {dline}"

    def _render_summary(self) -> str:
        assert self._index is not None
        cols, rows = self._get_terminal_size()
        page_size = max(1, rows - self._CHROME_LINES)
        indices = self._filtered_indices if self._filtered_indices is not None else list(range(self._index.total_entries))
        total = len(indices)

        if total == 0:
            lines = [self._render_header(), ""]
            lines.append("  No entries match the current filter.")
            lines.append("")
            lines.append(self._render_legend())
            lines.append(self._render_status_bar(0, total))
            return "\n".join(lines)

        page_start = max(0, self._cursor - self._cursor % page_size)
        page_end = min(page_start + page_size, total)

        lines = [self._render_header(), ""]

        wide = cols >= 100
        if wide:
            hdr = f"  {'#':>4}  {'Time':<20}  {'Action':<18}  {'Target':<20}  {'Tier':<4}  {'Outcome':<16}"
            sep_ch = _g("line") if not self._ascii else "-"
            sep = f"  {sep_ch * 4}  {sep_ch * 20}  {sep_ch * 18}  {sep_ch * 20}  {sep_ch * 4}  {sep_ch * 16}"
        else:
            hdr = f"  {'#':>4}  {'Time':<20}  {'Action':<16}  {'Tier':<4}  {'Outcome':<16}"
            sep_ch = _g("line") if not self._ascii else "-"
            sep = f"  {sep_ch * 4}  {sep_ch * 20}  {sep_ch * 16}  {sep_ch * 4}  {sep_ch * 16}"

        if self._color:
            lines.append(f"  {_DIM}{hdr.strip()}{_RESET}")
            lines.append(f"  {_DIM}{sep.strip()}{_RESET}")
        else:
            lines.append(hdr)
            lines.append(sep)

        for vi in range(page_start, page_end):
            real_idx = indices[vi]
            entry = self._index.read_entry(real_idx)
            is_cursor = vi == self._cursor
            lines.append(self._format_summary_row(entry, real_idx, is_cursor, wide))

        lines.append("")
        lines.append(self._render_legend())
        lines.append(self._render_status_bar(total, self._index.total_entries))
        return "\n".join(lines)

    def _format_summary_row(
        self, entry: Optional[dict], seq: int, is_cursor: bool, wide: bool
    ) -> str:
        c = self._color
        if entry is None:
            marker = "> " if is_cursor else "  "
            return f"{marker}  {seq:>3}  (malformed entry)"

        ts = _sanitize_display(entry.get("ts", "")[:19], max_len=20)
        action = _sanitize_display(entry.get("action", ""), max_len=18 if wide else 16)
        target = _sanitize_display(entry.get("target", ""), max_len=20) if wide else ""
        tier = entry.get("tier", -1)
        outcome = _sanitize_display(entry.get("outcome", ""), max_len=16)
        tier_str = f"T{tier}"

        marker = "> " if is_cursor else "  "

        if c:
            tier_colored = self._format_tier_colored(tier)
            outcome_colored = self._format_outcome_colored(outcome)
            if wide:
                row = f"{marker}{seq:>4}  {ts:<20}  {action:<18}  {target:<20}  {tier_colored:<4}  {outcome_colored}"
            else:
                row = f"{marker}{seq:>4}  {ts:<20}  {action:<16}  {tier_colored:<4}  {outcome_colored}"
            if is_cursor:
                return f"{_BOLD}{row}{_RESET}"
            return row
        else:
            if wide:
                return f"{marker}{seq:>4}  {ts:<20}  {action:<18}  {target:<20}  {tier_str:<4}  {outcome:<16}"
            return f"{marker}{seq:>4}  {ts:<20}  {action:<16}  {tier_str:<4}  {outcome:<16}"

    def _render_detail_current(self) -> str:
        assert self._index is not None
        indices = self._filtered_indices if self._filtered_indices is not None else list(range(self._index.total_entries))
        if not indices or self._cursor >= len(indices):
            return "  No entry selected.\n"
        real_idx = indices[self._cursor]
        entry = self._index.read_entry(real_idx)
        if entry is None:
            return f"  Entry #{real_idx} is malformed.\n"
        return self._render_detail(entry, real_idx)

    def _render_detail(self, entry: dict, seq: int) -> str:
        c = self._color
        line_ch = _g("line") if not self._ascii else "-"
        lines: list[str] = []

        header = f"  {line_ch}{line_ch} Entry #{seq} {line_ch * 60}"
        if c:
            lines.append(f"{_BOLD}{header}{_RESET}")
        else:
            lines.append(header)
        lines.append("")

        def _row(label: str, value: str) -> str:
            label_fmt = f"{_DIM}{label:<15}{_RESET}" if c else f"{label:<15}"
            return f"  {label_fmt}{_sanitize_display(str(value), max_len=80)}"

        lines.append(_row("Timestamp", entry.get("ts", "")))
        lines.append(_row("Session", entry.get("session_id", "")))
        lines.append(_row("Turn", str(entry.get("turn_index", ""))))
        lines.append(_row("Intent ID", entry.get("intent_id", "")))
        lines.append(_row("Action", entry.get("action", "")))
        lines.append(_row("Target", entry.get("target", "")))

        tier = entry.get("tier", -1)
        tier_label = _TIER_LABELS.get(tier, str(tier))
        tier_display = f"{tier} ({tier_label})"
        if c:
            tier_display = self._format_tier_colored(tier) + f" ({tier_label})"
        lines.append(_row("Tier", tier_display))

        lines.append(_row("Risk", entry.get("risk_level", "")))
        lines.append(_row("Reason", entry.get("reason", "")))

        outcome = entry.get("outcome", "")
        if c:
            lines.append(_row("Outcome", self._format_outcome_colored(outcome)))
        else:
            lines.append(_row("Outcome", outcome))

        dur = entry.get("duration_ms", 0.0)
        lines.append(_row("Duration", f"{dur:.0f}ms"))
        lines.append(_row("User", entry.get("user", "")))
        lines.append(_row("Backend", entry.get("backend", "")))
        lines.append(_row("Model", entry.get("model", "")))

        tin = entry.get("tokens_in", 0)
        tout = entry.get("tokens_out", 0)
        lines.append(_row("Tokens", f"{tin:,} in / {tout:,} out"))

        cost = entry.get("cost_estimate_usd", 0.0)
        lines.append(_row("Cost", f"${cost:.4f}"))

        if self._chain_status:
            ok = self._chain_status.entry_status.get(seq)
            if ok is True:
                chain_str = f"{_g('ok')} (seq {seq}, hash matches)" if not c else f"{_GREEN}{_g('ok')}{_RESET} (seq {seq}, hash matches)"
            elif ok is False:
                chain_str = f"{_g('fail')} (seq {seq}, BROKEN)" if not c else f"{_RED}{_g('fail')}{_RESET} (seq {seq}, BROKEN)"
            else:
                chain_str = "not verified"
            lines.append(_row("Chain", chain_str))

        skip_keys = set(REQUIRED_FIELDS) | {"seq", "prev_hash"}
        extra = {k: v for k, v in entry.items() if k not in skip_keys}
        if extra:
            lines.append("")
            lines.append(f"  {'Extra fields:' if not c else f'{_DIM}Extra fields:{_RESET}'}")
            for k, v in extra.items():
                val = _sanitize_display(str(v), max_len=60)
                if val == REDACTED_PLACEHOLDER and c:
                    val = f"{_DIM}{val}{_RESET}"
                lines.append(f"    {k:<20} {val}")

        lines.append("")
        sep = f"  {line_ch}{line_ch} [q/Esc] Back  [j/k] Prev/Next entry  [?] Help {line_ch * 20}"
        if c:
            lines.append(f"{_DIM}{sep}{_RESET}")
        else:
            lines.append(sep)

        return "\n".join(lines)

    def _render_help(self) -> str:
        c = self._color
        line_ch = _g("line") if not self._ascii else "-"
        lines: list[str] = []

        header = f"  {line_ch}{line_ch} Keybindings {line_ch * 60}"
        if c:
            lines.append(f"{_BOLD}{header}{_RESET}")
        else:
            lines.append(header)
        lines.append("")

        def _section(title: str) -> str:
            return f"  {_BOLD}{title}{_RESET}" if c else f"  {title}"

        def _bind(keys: str, desc: str) -> str:
            return f"    {keys:<20} {desc}"

        lines.append(_section("Navigation"))
        lines.append(_bind("j / Down", "Next entry"))
        lines.append(_bind("k / Up", "Previous entry"))
        lines.append(_bind("J / Space", "Next page"))
        lines.append(_bind("K / b", "Previous page"))
        lines.append(_bind("g", "First entry"))
        lines.append(_bind("G", "Last entry"))
        lines.append("")
        lines.append(_section("Views"))
        lines.append(_bind("Enter", "Detail view"))
        lines.append(_bind("q / Esc", "Back / Quit"))
        lines.append(_bind("Q", "Quit (always)"))
        lines.append("")
        lines.append(_section("Tools"))
        lines.append(_bind("f", "Filter entries"))
        lines.append(_bind("/", "Search (regex)"))
        lines.append(_bind("v", "Verify hash-chain"))
        lines.append(_bind("s", "Statistics"))
        lines.append(_bind("?", "This help"))
        lines.append("")

        sep = f"  {line_ch}{line_ch} Press any key to return {line_ch * 48}"
        if c:
            lines.append(f"{_DIM}{sep}{_RESET}")
        else:
            lines.append(sep)

        return "\n".join(lines)

    def _render_stats(self) -> str:
        stats = self._compute_stats()
        c = self._color
        line_ch = _g("line") if not self._ascii else "-"
        lines: list[str] = []

        header = f"  {line_ch}{line_ch} Statistics {line_ch * 61}"
        if c:
            lines.append(f"{_BOLD}{header}{_RESET}")
        else:
            lines.append(header)
        lines.append("")

        total_str = f"{stats['total']} total"
        if stats["filtered"]:
            total_str += f" ({stats['all_total']} unfiltered)"
        lines.append(f"  {'Entries':<15} {total_str}")

        if stats["first_ts"] and stats["last_ts"]:
            lines.append(f"  {'Time span':<15} {stats['first_ts'][:19]} .. {stats['last_ts'][:19]}")
        lines.append(f"  {'Sessions':<15} {stats['sessions']}")
        lines.append("")

        if stats["outcomes"]:
            lines.append(f"  {'Outcome distribution:' if not c else f'{_DIM}Outcome distribution:{_RESET}'}")
            total = stats["total"] or 1
            for o, count in stats["outcomes"].items():
                pct = count / total * 100
                lines.append(f"    {o:<22} {count:>5}  ({pct:>5.1f}%)")
            lines.append("")

        if stats["tiers"]:
            lines.append(f"  {'Tier distribution:' if not c else f'{_DIM}Tier distribution:{_RESET}'}")
            total = stats["total"] or 1
            for t, count in stats["tiers"].items():
                label = _TIER_LABELS.get(t, str(t))
                lines.append(f"    Tier {t} ({label:<10}) {count:>5}  ({count / total * 100:>5.1f}%)")
            lines.append("")

        if stats["backends"]:
            lines.append(f"  {'Backend distribution:' if not c else f'{_DIM}Backend distribution:{_RESET}'}")
            total = stats["total"] or 1
            for b, count in stats["backends"].items():
                lines.append(f"    {b:<22} {count:>5}  ({count / total * 100:>5.1f}%)")
            lines.append("")

        lines.append(f"  {'Total cost':<15} ${stats['total_cost']:.4f}")
        lines.append(f"  {'Total tokens':<15} {stats['total_tokens_in']:,} in / {stats['total_tokens_out']:,} out")
        lines.append("")

        sep = f"  {line_ch}{line_ch} Press any key to return {line_ch * 48}"
        if c:
            lines.append(f"{_DIM}{sep}{_RESET}")
        else:
            lines.append(sep)

        return "\n".join(lines)

    def _render_legend(self) -> str:
        c = self._color
        line_ch = _g("line") if not self._ascii else "-"
        parts = [
            "[j/k] Navigate",
            "[Enter] Detail",
            "[f] Filter",
            "[/] Search",
            "[v] Verify",
            "[?] Help",
        ]
        legend = "  ".join(parts)
        sep = f"  {line_ch}{line_ch} {legend} {line_ch}{line_ch}"
        if c:
            return f"{_DIM}{sep}{_RESET}"
        return sep

    def _render_status_bar(self, visible: int, total: int) -> str:
        parts: list[str] = []
        parts.append(f"{visible} of {total} entries" if visible != total else f"{total} entries")

        if self._filter_spec and not self._filter_spec.is_empty():
            parts.append(f"filter: {self._filter_spec.describe()}")
        else:
            parts.append("filter: none")

        if self._chain_status:
            if self._chain_status.ok:
                chain_str = f"chain: {_g('ok')} verified"
            else:
                chain_str = f"chain: {_g('fail')} broken at seq {self._chain_status.first_bad_seq}"
        else:
            chain_str = "chain: not verified"
        parts.append(chain_str)

        return "  " + " | ".join(parts)

    # ── Formatting helpers ────────────────────────────────────────────────

    def _format_tier_colored(self, tier: int) -> str:
        if not self._color:
            return f"T{tier}"
        colors = {0: _DIM, 1: "", 2: _YELLOW, 3: _RED}
        c = colors.get(tier, "")
        return f"{c}T{tier}{_RESET}" if c else f"T{tier}"

    def _format_outcome_colored(self, outcome: str) -> str:
        if not self._color:
            return outcome
        if outcome in ("executed", "trust_applied", "trust_granted"):
            return f"{_GREEN}{outcome}{_RESET}"
        elif outcome in ("hitl_denied", "hitl_timeout", "hitl_non_tty"):
            return outcome
        elif outcome in ("tool_error", "brain_error", "schema_rejected"):
            return f"{_RED}{outcome}{_RESET}"
        elif outcome in ("limit_exceeded", "cost_exceeded"):
            return f"{_YELLOW}{outcome}{_RESET}"
        return outcome

    # ── Terminal ──────────────────────────────────────────────────────────

    def _clear_screen(self) -> None:
        if self._color:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        else:
            print("\n" * 40)

    def _get_terminal_size(self) -> tuple[int, int]:
        try:
            ts = os.get_terminal_size()
            return (ts.columns, ts.lines)
        except OSError:
            return (80, 24)

    # ── Input ─────────────────────────────────────────────────────────────

    def _read_key(self) -> Optional[str]:
        fd = sys.stdin.fileno()
        r, _, _ = select.select([fd], [], [], 1.0)
        if r:
            try:
                data = os.read(fd, 1)
                return data.decode("utf-8", errors="replace") if data else None
            except OSError:
                return None
        return None

    def _wait_any_key(self) -> None:
        try:
            with _cbreak(sys.stdin):
                self._read_key()
        except (KeyboardInterrupt, EOFError):
            pass

    def _handle_key(self, key: str) -> bool:
        action = _viewer_lookup(key)
        if action is None:
            return True

        indices = self._filtered_indices if self._filtered_indices is not None else list(range(self._index.total_entries))  # type: ignore[union-attr]
        total = len(indices)

        if action == ViewerAction.QUIT:
            return False

        if self._mode == ViewMode.HELP:
            self._mode = ViewMode.SUMMARY
            return True

        if self._mode == ViewMode.STATS:
            self._mode = ViewMode.SUMMARY
            return True

        if self._mode == ViewMode.DETAIL:
            if action == ViewerAction.BACK:
                self._mode = ViewMode.SUMMARY
                return True
            elif action == ViewerAction.DOWN:
                if self._cursor < total - 1:
                    self._cursor += 1
                return True
            elif action == ViewerAction.UP:
                if self._cursor > 0:
                    self._cursor -= 1
                return True
            elif action == ViewerAction.HELP:
                self._mode = ViewMode.HELP
                return True
            return True

        # Summary mode
        if action == ViewerAction.BACK:
            return False
        elif action == ViewerAction.DOWN:
            if self._cursor < total - 1:
                self._cursor += 1
        elif action == ViewerAction.UP:
            if self._cursor > 0:
                self._cursor -= 1
        elif action == ViewerAction.PAGE_DOWN:
            _, rows = self._get_terminal_size()
            page = max(1, rows - self._CHROME_LINES)
            self._cursor = min(self._cursor + page, total - 1) if total > 0 else 0
        elif action == ViewerAction.PAGE_UP:
            _, rows = self._get_terminal_size()
            page = max(1, rows - self._CHROME_LINES)
            self._cursor = max(self._cursor - page, 0)
        elif action == ViewerAction.HOME:
            self._cursor = 0
        elif action == ViewerAction.END:
            self._cursor = max(0, total - 1)
        elif action == ViewerAction.SELECT:
            if total > 0:
                self._mode = ViewMode.DETAIL
        elif action == ViewerAction.HELP:
            self._mode = ViewMode.HELP
        elif action == ViewerAction.STATS:
            self._mode = ViewMode.STATS
        elif action == ViewerAction.VERIFY:
            self._clear_screen()
            print(self._render_header())
            print("\n  Verifying hash-chain...")
            self._chain_status = self._verify_chain()
            if self._chain_status.ok:
                print(f"  {_g('ok')} Chain intact ({self._chain_status.verified_count} entries verified).")
            else:
                print(f"  {_g('fail')} Chain broken at seq {self._chain_status.first_bad_seq}.")
            print("\n  Press any key to continue.")
            self._read_key()
        elif action == ViewerAction.FILTER:
            self._handle_filter_prompt()
        elif action == ViewerAction.SEARCH:
            self._handle_search_prompt()

        return True

    def _handle_filter_prompt(self) -> None:
        self._clear_screen()
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            spec = self._prompt_filter()
        finally:
            pass  # cbreak restored by outer context

        if spec is None:
            return
        if spec.is_empty():
            self._filter_spec = None
            self._filtered_indices = None
            self._cursor = 0
            return
        self._filter_spec = spec
        self._filtered_indices = self._apply_filter(spec)
        self._cursor = 0

    def _handle_search_prompt(self) -> None:
        self._clear_screen()
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            pattern = self._prompt_search()
        finally:
            pass  # cbreak restored by outer context

        if pattern is None:
            return
        spec = FilterSpec(search_pattern=pattern)
        self._filter_spec = spec
        self._filtered_indices = self._apply_filter(spec)
        self._cursor = 0

    # ── Non-TTY fallback ──────────────────────────────────────────────────

    def _dump_json(self) -> int:
        assert self._index is not None
        for i in range(self._index.total_entries):
            entry = self._index.read_entry(i)
            if entry is None:
                continue
            if self._filter_spec and not self._filter_spec.matches(entry):
                continue
            print(json.dumps(entry, separators=(",", ":"), default=str))
        return 0


# ── CLI entry point ──────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m controller.audit_viewer",
        description="Interactive audit log viewer for Icebreaker Controller",
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="Path to audit log (default: standard location)",
    )
    parser.add_argument("--verify", action="store_true", help="Verify hash-chain on startup")
    parser.add_argument("--json", action="store_true", help="Output as JSON (non-interactive)")
    parser.add_argument("--filter-session", metavar="ID")
    parser.add_argument("--filter-tier", type=int, choices=[0, 1, 2, 3])
    parser.add_argument("--filter-outcome", metavar="NAME")
    parser.add_argument("--filter-action", metavar="NAME")
    parser.add_argument("--filter-backend", metavar="NAME")
    parser.add_argument("--filter-after", metavar="ISO")
    parser.add_argument("--filter-before", metavar="ISO")

    args = parser.parse_args(argv)

    path = Path(args.path).expanduser() if args.path else AuditLog._default_path()

    has_filter = any([
        args.filter_session, args.filter_tier is not None,
        args.filter_outcome, args.filter_action, args.filter_backend,
        args.filter_after, args.filter_before,
    ])
    spec = FilterSpec(
        session_id=args.filter_session,
        tier=args.filter_tier,
        outcome=args.filter_outcome,
        action=args.filter_action,
        backend=args.filter_backend,
        time_after=args.filter_after,
        time_before=args.filter_before,
    ) if has_filter else None

    viewer = AuditViewer(path, verify=args.verify, filter_spec=spec)

    if args.json:
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            return 2
        try:
            viewer._index = LogIndex.build(path)
        except (PermissionError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        return viewer._dump_json()

    return viewer.run()


if __name__ == "__main__":
    sys.exit(main())
