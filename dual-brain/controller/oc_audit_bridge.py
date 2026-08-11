"""oc_audit_bridge — v6.13_OC INV-8 audit enrichment bridge.

Runs as an in-process watchdog observer thread inside the Python
controller daemon when ``cfg.qb.backend == "opencode_oc"``. Watches
mcpd's audit log (``/var/log/mcpd/audit.log``), parses each new line,
enriches with Icebreaker session context, and writes an INV-8-shape
entry to the Python controller's audit log via the existing shared
:class:`~controller.audit.AuditLog` instance (hash chain preserved
automatically by that class's internal lock).

## Why this exists

In the OC edition, opencode spawns mcpd directly. The Python daemon
never sees the tool calls. Without this bridge, INV-8 audit would only
capture tool-level events (mcpd's own log) and lose the intent-level
context (session_id, backend, model). The bridge fills that gap so
the INV-8 hash chain remains a useful forensics artifact.

## v1 scope

Per the per-fix plan (2026-07-25):

- session_id = fixed sentinel ``"opencode-oc"`` (v2: PID → opencode PID → session-map file).
- turn_index = ``-1`` (v2: fill from opencode's ``/event`` SSE stream).
- model, tokens_in, tokens_out, cost = ``""`` / ``0`` (v2: SSE fills).
- intent_id = fresh UUID per bridged line.
- All other fields (action, target, tier, outcome, duration_ms,
  backend, reason, risk_level) are derived deterministically from
  mcpd's own line.

## R18 discipline

Never drop an audit line. If enrichment fails, still write with
sentinel fields — the operator sees ``session_id="opencode-oc"``,
``action="unknown"``, ``extra.enrichment_error=<msg>`` in forensics
rather than a gap.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Deque, Mapping, Optional

if TYPE_CHECKING:  # avoid runtime import of watchdog when only reading types
    from watchdog.observers.api import BaseObserver

from .audit import AuditFields, AuditLog, Outcome

_log = logging.getLogger("controller.oc_audit_bridge")

#: Session ID stamped on every bridged entry. Fixed sentinel in v1 —
#: distinguishes bridge-authored INV-8 rows from natively-authored
#: Python-controller rows at forensics time. R18: never blank.
OC_SESSION_SENTINEL = "opencode-oc"


# ── v6.17 M7.6a-1f: native-dispatch correlation ring ────────────────
#
# When the Python controller (via Controller.run_turn_from_intent, the
# M7.6a-1c OC submit_intent flow) dispatches an mcpd call, it writes
# an INV-8 audit row natively via Controller._audit.write_fields at
# Step 5 + Step 9. mcpd ALSO writes its own audit log line to
# /var/log/mcpd/audit.log which the bridge tails + enriches. Without
# deduplication, EACH real tool call gets TWO INV-8 rows: one native
# (real session_id + turn_index) + one bridge (sentinel session_id).
#
# Fix (M7.6a-1f, per user decision 2026-08-09): correlate via
# (method, target, timestamp-window) instead of an mcpd protocol
# change. Both Controller and Bridge run in the same daemon process,
# so a module-level singleton ring works — no IPC, no serialization.
#
# Design:
# - Controller calls mark_native_dispatch(method, target) right BEFORE
#   its self._mcpd.call(...) — records that this specific
#   (method, target) is a native dispatch.
# - Bridge's process_line calls is_native_dispatch(method, target) on
#   every mcpd audit line; if match within window, skip writing the
#   bridge-enriched row (native row already wrote it).
# - Consume-on-match semantics: is_native_dispatch removes the entry
#   so a follow-up mcpd audit line with the same key doesn't get
#   inadvertently skipped (each native dispatch → exactly ONE mcpd
#   audit line → exactly ONE skip).
# - 500-entry deque cap + 10-min TTL: bounds memory + prevents stale
#   entries from bleeding across daemon uptime.
# - Thread-safe via _RING_LOCK; both Controller's turn threads and the
#   bridge's watchdog thread may access concurrently.

@dataclass
class _NativeDispatchEntry:
    """One recorded native-dispatch event. Ring content."""
    method: str
    target: str
    ts_monotonic: float


_NATIVE_RING: Deque[_NativeDispatchEntry] = deque(maxlen=500)
_RING_LOCK = threading.Lock()
_RING_TTL_SECONDS = 600.0  # 10 minutes; matches the plan's design


def mark_native_dispatch(method: str, target: str) -> None:
    """Record that the Python controller is about to dispatch an mcpd
    call with (method, target). The bridge will skip the corresponding
    mcpd audit log line to prevent double-audit.

    Called from Controller (main.py) OR McpdClient.call — either works;
    both are inside the same daemon process. Safe to call from any
    thread. Safe to call even when bridge isn't running (ring just
    accumulates + expires harmlessly; costs ~5KB steady-state).

    See M7.6a-1f in F-111 (incremental/GROUND_TRUTH.md § 7) for full
    design rationale.
    """
    now = time.monotonic()
    with _RING_LOCK:
        # Sweep expired entries so the ring never holds stale garbage.
        # deque doesn't support O(1) age-based eviction, so we do a
        # left-scan (bounded by TTL; typical ring size is ≤ 500).
        while _NATIVE_RING and (now - _NATIVE_RING[0].ts_monotonic) > _RING_TTL_SECONDS:
            _NATIVE_RING.popleft()
        _NATIVE_RING.append(
            _NativeDispatchEntry(method=method, target=target, ts_monotonic=now)
        )


def is_native_dispatch(
    method: str, target: str, window_secs: float = 30.0,
) -> bool:
    """True iff a native dispatch with matching (method, target) was
    marked within `window_secs`. Consumes the matching entry on
    return so follow-up mcpd audit lines with the same key are NOT
    also skipped.

    Called from oc_audit_bridge.process_line for each mcpd audit line
    it processes. Returns True → bridge skips writing the enriched
    INV-8 row for that line. Returns False → bridge writes normally.

    30-second window default matches the mcpd_timeout_seconds ceiling
    from controller.toml (mcpd calls timing out beyond 30s are handled
    as errors before the audit line lands, so a wider window would only
    absorb spurious cross-turn matches).
    """
    now = time.monotonic()
    with _RING_LOCK:
        for i, entry in enumerate(_NATIVE_RING):
            if entry.method == method and entry.target == target:
                if (now - entry.ts_monotonic) <= window_secs:
                    # Consume the match — see docstring for why.
                    del _NATIVE_RING[i]
                    return True
        return False


def _reset_native_ring_for_tests() -> None:
    """Test-only: clear the ring so tests don't leak state to each other.
    NEVER call from production code."""
    with _RING_LOCK:
        _NATIVE_RING.clear()


def _native_ring_size_for_tests() -> int:
    """Test-only: current ring size for state-observation assertions."""
    with _RING_LOCK:
        return len(_NATIVE_RING)

#: mcpd tool name → Tier lookup. Derived from mcpd's tool descriptors
#: (`src/mcpd/src/tools/mod.rs` + `src/mcpd/schemas/*.json`) as of
#: v6.13_OC. Manifest-registered tools default to tier 0 (safe
#: assumption; the enrichment marks unknown tools with
#: `extra.tier_source = "default_unknown_tool"` for operator awareness).
#:
#: This map is intentionally hand-maintained rather than harvested at
#: runtime — the alternative (spawn mcpd, call `tools/list`) would add
#: a subprocess dependency + 50-100ms of startup latency for a value
#: that changes with every mcpd release. Keeping it here makes the
#: bridge deterministic + testable in pure Python.
#:
#: When mcpd's tool set changes, update this map. A regression guard
#: test asserts every listed tool exists in mcpd's `tools/list` output
#: at integration-test time.
_TIER_MAP: Mapping[str, int] = {
    # tier 0 — read-only
    "system.status": 0, "system.uptime": 0, "system.cpu": 0,
    "system.memory": 0, "system.disk": 0, "system.unsupported": 0,
    "process.list": 0,
    "fs.read": 0, "fs.list": 0, "fs.stat": 0,
    "network.status": 0, "network.dns.read": 0,
    "package.query": 0,
    "service.logs": 0,
    "tools/list": 0, "tools/call": 0, "initialize": 0,
    # tier 1 — write in $HOME (mcpd's runtime escalates to tier 2+ for
    # out-of-home paths; that decision is not visible in the audit line,
    # so this map records the SAFE floor)
    "fs.write": 1,
    "process.inspect": 1,
    # tier 2 — system-wide effects
    "fs.delete": 2,
    "service.start": 2, "service.stop": 2, "service.restart": 2,
    "package.install": 2, "package.remove": 2, "package.upgrade": 2,
}

#: mcpd `result_class` → :class:`Outcome` mapping. Anything not listed
#: falls through to ``TOOL_ERROR`` (conservative). Extend when mcpd
#: gains new result classes.
_OUTCOME_MAP: Mapping[str, Outcome] = {
    "ok": Outcome.EXECUTED,
    "err": Outcome.TOOL_ERROR,
    "schema_error": Outcome.SCHEMA_REJECTED,
    "unknown_method": Outcome.SCHEMA_REJECTED,
    "parse_error": Outcome.TOOL_ERROR,
}

#: tier → risk_level mapping. Kept simple; matches the Python
#: controller's own risk-classifier output for equivalent Tier values.
_RISK_MAP: Mapping[int, str] = {0: "low", 1: "low", 2: "medium", 3: "critical"}

#: Ordered probe list — first present key wins as ``target``. Reflects
#: mcpd's per-tool primary-target parameter convention.
_TARGET_KEYS = ("path", "unit", "package", "pattern", "pid", "hostname", "workflow")


def enrich_mcpd_entry(mcpd_entry: Mapping[str, Any]) -> AuditFields:
    """Convert one mcpd audit dict into a Python :class:`AuditFields`.

    Pure function — no I/O, no side effects. Tested independently of
    the file watcher. Public so the ``ib-debug audit replay`` tool (if
    added later) can convert historical mcpd logs to INV-8 shape.

    Never raises — even a partial/garbage input dict yields a
    sentinel-filled AuditFields (R18: audit data must land).
    """
    method = str(mcpd_entry.get("method") or "unknown")
    params_raw = mcpd_entry.get("params_redacted")
    params: Mapping[str, Any] = params_raw if isinstance(params_raw, Mapping) else {}
    result_class = str(mcpd_entry.get("result_class") or "err")
    try:
        latency_us = int(mcpd_entry.get("latency_us") or 0)
    except (TypeError, ValueError):
        latency_us = 0
    mcpd_ts = str(mcpd_entry.get("timestamp") or "")

    outcome = _OUTCOME_MAP.get(result_class, Outcome.TOOL_ERROR)
    tier = _TIER_MAP.get(method, 0)
    risk_level = _RISK_MAP.get(tier, "low")

    target = ""
    for key in _TARGET_KEYS:
        if key in params:
            target = str(params[key])[:512]  # cap oversize params
            break

    extra: dict[str, Any] = {
        "source": "oc_audit_bridge",
        "mcpd_ts": mcpd_ts,
        "mcpd_result_class": result_class,
    }
    if method not in _TIER_MAP:
        # Unknown tool (probably manifest-registered). Tier defaulted to 0;
        # flag for operator awareness so an inspection can confirm.
        extra["tier_source"] = "default_unknown_tool"

    return AuditFields(
        session_id=OC_SESSION_SENTINEL,
        turn_index=-1,
        intent_id=str(uuid.uuid4()),
        action=method,
        target=target,
        tier=tier,
        reason="opencode_oc",
        risk_level=risk_level,
        outcome=outcome,
        duration_ms=latency_us / 1000.0,
        backend="opencode_oc",
        model="",
        tokens_in=0,
        tokens_out=0,
        cost_estimate_usd=0.0,
        extra=extra,
    )


@dataclass
class BridgeStats:
    """Runtime counters exposed via :meth:`OCAuditBridge.stats` for
    Control Center's health widget and debugging."""

    lines_ingested: int = 0
    lines_dropped_malformed: int = 0
    write_errors: int = 0
    started_at: float = 0.0


class OCAuditBridge:
    """Watches mcpd's audit log and mirrors each line to the Python
    controller's INV-8 audit log with enrichment.

    Not thread-safe to construct — call :meth:`start` from one thread
    (the daemon's main thread at boot). After start, the watchdog
    observer runs on its own thread and calls ``_on_file_modified``
    under ``self._lock`` so partial reads don't interleave.

    :class:`AuditLog` has its own internal lock; we release ours before
    calling ``write_fields`` to keep the watchdog thread from blocking
    on other audit writers.
    """

    def __init__(
        self,
        *,
        audit_log: AuditLog,
        mcpd_audit_path: Path,
        catchup_on_start: bool = False,
    ) -> None:
        """
        Args:
            audit_log: the Python controller's INV-8 audit sink (shared
                singleton constructed in ``__main__.py``).
            mcpd_audit_path: path to ``/var/log/mcpd/audit.log`` (or
                whatever ``MCPD_AUDIT_LOG`` points to).
            catchup_on_start: if True, replay every existing line in the
                mcpd log at startup. Default False — otherwise the
                INV-8 log fills with stale entries on every daemon
                restart. v1 only cares about lines produced AFTER
                the daemon started.
        """
        self._audit_log = audit_log
        self._mcpd_audit_path = Path(mcpd_audit_path)
        self._catchup_on_start = catchup_on_start
        self._observer: Optional["BaseObserver"] = None
        self._offset: int = 0
        self._buffer: bytes = b""
        self._lock = threading.Lock()
        self._stats = BridgeStats()
        # v6.17 M7.6a-1f: clean the native-dispatch ring on every bridge
        # init so residual state from a prior test / prior daemon-in-
        # daemon lifecycle doesn't skip legitimate mcpd audit lines.
        # Safe: production only constructs OCAuditBridge once at daemon
        # boot; the reset is a no-op in that case.
        _reset_native_ring_for_tests()

    def start(self) -> None:
        """Begin watching. Idempotent — calling twice is a no-op."""
        if self._observer is not None:
            return
        # Local import — keeps watchdog off the critical import path
        # for the current-edition daemon (which never instantiates
        # this class). Also lets tests exercise enrichment without
        # requiring watchdog installed.
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        parent = self._mcpd_audit_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("oc_audit_bridge: cannot mkdir %s: %s", parent, exc)
            # We still try to start — the file may exist even if we
            # can't create the parent.

        # F-91 (regression guard: F-91-bridge-degrade) — parent dir must
        # exist before Observer.schedule() — watchdog's inotify_add_watch
        # raises FileNotFoundError otherwise, and that exception previously
        # propagated up through _maybe_start_oc_bridge and killed the
        # daemon with `Fatal: [Errno 2]` (systemd then crash-looped every
        # 5s). BP-10 + INV-8 R18: the audit bridge is optional enrichment
        # — its absence must never take down the controller. Degrade to
        # disabled + audit-visible if we can't watch.
        if not parent.exists():
            _log.error(
                "oc_audit_bridge: parent dir %s does not exist and could not "
                "be created — bridge disabled. INV-8 audit will continue via "
                "the Python controller's native path; mcpd tool calls "
                "originating from opencode will NOT be enriched into the "
                "controller audit log. Fix: add LogsDirectory=mcpd to the "
                "icebreaker-controller.service unit, or set "
                "[qb.opencode_oc] audit_bridge_enabled=false to silence "
                "this at boot.", parent,
            )
            return

        if self._mcpd_audit_path.exists():
            self._offset = (
                0 if self._catchup_on_start
                else self._mcpd_audit_path.stat().st_size
            )
        # If the file doesn't exist yet, start at offset 0 and pick it
        # up when mcpd creates it — the parent-dir watch catches
        # the on_created event.

        bridge = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event) -> None:  # type: ignore[override]
                if getattr(event, "src_path", "") == str(bridge._mcpd_audit_path):
                    bridge._on_file_modified()

            def on_created(self, event) -> None:  # type: ignore[override]
                if getattr(event, "src_path", "") == str(bridge._mcpd_audit_path):
                    bridge._on_file_modified()

            def on_moved(self, event) -> None:  # type: ignore[override]
                # Log rotation: file was moved out. Reset offset so we
                # pick up the new file on the next on_created event.
                if getattr(event, "src_path", "") == str(bridge._mcpd_audit_path):
                    with bridge._lock:
                        bridge._offset = 0
                        bridge._buffer = b""

        # F-99: even with parent.exists() True, Observer.schedule/start
        # can still fail (inotify watch limit, permission race, watchdog
        # backend swap under qemu-user). Same degrade rule applies —
        # never propagate to the caller.
        try:
            observer = Observer()
            observer.schedule(_Handler(), path=str(parent), recursive=False)
            observer.start()
        except (OSError, RuntimeError) as exc:
            _log.error(
                "oc_audit_bridge: watchdog failed to start on %s: %s: %s — "
                "bridge disabled (see previous log entry for remediation).",
                parent, type(exc).__name__, exc,
            )
            return
        self._observer = observer
        self._stats.started_at = time.monotonic()
        _log.info(
            "oc_audit_bridge started: watching %s, offset=%d, catchup=%s",
            self._mcpd_audit_path, self._offset, self._catchup_on_start,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop watching. Safe to call multiple times."""
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        self._observer = None
        _log.info(
            "oc_audit_bridge stopped: ingested=%d dropped=%d write_errors=%d",
            self._stats.lines_ingested,
            self._stats.lines_dropped_malformed,
            self._stats.write_errors,
        )

    def stats(self) -> BridgeStats:
        return self._stats

    # ── internal ──────────────────────────────────────────────────────

    def _on_file_modified(self) -> None:
        """Read newly-appended bytes and dispatch complete lines. Runs
        on the watchdog thread; the lock guards ``_offset`` and
        ``_buffer`` for the read+split step. Writes happen after
        releasing our lock — :class:`AuditLog` has its own lock.
        """
        with self._lock:
            if not self._mcpd_audit_path.exists():
                return
            try:
                with open(self._mcpd_audit_path, "rb") as f:
                    f.seek(self._offset)
                    new_bytes = f.read()
                    self._offset = f.tell()
            except OSError as exc:
                _log.warning("oc_audit_bridge read failed: %s", exc)
                return

            self._buffer += new_bytes
            lines: list[bytes] = []
            while b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                if line.strip():
                    lines.append(line)

        # Write lock-free — AuditLog has its own internal locking.
        for raw in lines:
            self._ingest_line(raw)

    def _ingest_line(self, raw: bytes) -> None:
        """Parse + enrich + write one mcpd audit line. R18: on any
        enrichment failure, still write a sentinel-filled entry."""
        try:
            mcpd_entry = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(mcpd_entry, dict):
                raise ValueError("audit line is not a JSON object")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            _log.warning(
                "oc_audit_bridge dropped malformed line: %s: %r",
                exc, raw[:120],
            )
            self._stats.lines_dropped_malformed += 1
            return

        # v6.17 M7.6a-1f: skip lines the Python controller already
        # audited natively (submit_intent flow via
        # Controller.run_turn_from_intent → McpdClient.call →
        # mark_native_dispatch). Prevents double-audit for OC-mode
        # submit_intent turns. Consume-on-match semantics (see
        # is_native_dispatch docstring) ensure follow-up mcpd audit
        # lines with the same (method, target) aren't inadvertently
        # skipped.
        _method = str(mcpd_entry.get("method") or "")
        _params = mcpd_entry.get("params_redacted") or {}
        if isinstance(_params, Mapping):
            _target = ""
            for _key in _TARGET_KEYS:
                if _key in _params:
                    _target = str(_params[_key])[:512]
                    break
        else:
            _target = ""
        if _method and is_native_dispatch(_method, _target):
            _log.debug(
                "oc_audit_bridge: skipping native-dispatched line %s target=%s",
                _method, _target,
            )
            # Count against ingested so operators see the throughput,
            # but not against write_errors — this is a correct skip.
            self._stats.lines_ingested += 1
            return

        try:
            fields = enrich_mcpd_entry(mcpd_entry)
        except Exception as exc:  # noqa: BLE001 — R18: never drop
            _log.warning(
                "oc_audit_bridge enrichment failed, writing sentinel: %s", exc,
            )
            fields = AuditFields(
                session_id=OC_SESSION_SENTINEL,
                turn_index=-1,
                intent_id=str(uuid.uuid4()),
                action=str(mcpd_entry.get("method") or "unknown"),
                target="",
                tier=0,
                reason="opencode_oc",
                risk_level="low",
                outcome=Outcome.TOOL_ERROR,
                duration_ms=0.0,
                backend="opencode_oc",
                model="",
                tokens_in=0,
                tokens_out=0,
                cost_estimate_usd=0.0,
                extra={
                    "source": "oc_audit_bridge",
                    "enrichment_error": str(exc)[:200],
                },
            )

        try:
            self._audit_log.write_fields(fields)
            self._stats.lines_ingested += 1
        except Exception as exc:  # noqa: BLE001 — never crash the daemon
            self._stats.write_errors += 1
            _log.error("oc_audit_bridge write_fields failed: %s", exc)
