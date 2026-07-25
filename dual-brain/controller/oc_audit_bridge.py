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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:  # avoid runtime import of watchdog when only reading types
    from watchdog.observers.api import BaseObserver

from .audit import AuditFields, AuditLog, Outcome

_log = logging.getLogger("controller.oc_audit_bridge")

#: Session ID stamped on every bridged entry. Fixed sentinel in v1 —
#: distinguishes bridge-authored INV-8 rows from natively-authored
#: Python-controller rows at forensics time. R18: never blank.
OC_SESSION_SENTINEL = "opencode-oc"

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

        self._observer = Observer()
        self._observer.schedule(_Handler(), path=str(parent), recursive=False)
        self._observer.start()
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
