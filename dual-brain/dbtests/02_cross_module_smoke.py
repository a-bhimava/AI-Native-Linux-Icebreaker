#!/usr/bin/env python3
"""
02_cross_module_smoke.py — end-to-end exercise of M2.0–M2.3 in one file.

Until M2.4 wires up the brain backends, none of the four shipped modules
(intent_schema, risk_classifier, intent_store, audit) is hit by the same
test as the others. This script does that integration walk so an
operator can see, in one pass, that:

  1. A well-formed intent flows validate → classify → store → audit
     and the audit row carries the right fields.
  2. A Tier 3 intent reaches the classifier with requires_hitl=True
     and the simulated HITL_DENIED outcome lands in the audit log.
  3. A malformed intent is rejected by the schema BEFORE it can reach
     the classifier or store, and the rejection envelope makes it to
     the audit log.
  4. Secrets in params are redacted in the audit log.

No mcpd, no brains. Hand-crafted intents. Fast (< 1 second). Run from
anywhere — script auto-mounts dual-brain/ on sys.path.

Exit code 0 = all 4 scenarios passed. 1 = any assertion failed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Mount dual-brain/ so `from controller.X import Y` works regardless of CWD.
_HERE = Path(__file__).resolve().parent
_DUAL_BRAIN = _HERE.parent
sys.path.insert(0, str(_DUAL_BRAIN))

from controller import intent_schema  # noqa: E402
from controller import intent_store  # noqa: E402
from controller import risk_classifier  # noqa: E402
from controller.audit import (  # noqa: E402
    AuditFields,
    AuditLog,
    Outcome,
    REDACTED_PLACEHOLDER,
)


# ── Output helpers ──────────────────────────────────────────────────────────

def _colour(code: str) -> str:
    return code if sys.stdout.isatty() else ""

GRN = _colour("\033[32m")
RED = _colour("\033[31m")
YLW = _colour("\033[33m")
CYN = _colour("\033[36m")
DIM = _colour("\033[2m")
BOLD = _colour("\033[1m")
RST = _colour("\033[0m")


def step(n: int, total: int, label: str) -> None:
    print(f"\n{CYN}── scenario {n}/{total} ─ {label} ──{RST}")


def info(msg: str) -> None:
    print(f"  {DIM}·{RST} {msg}")


def ok(msg: str) -> None:
    print(f"  {GRN}✓{RST} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗ FAIL{RST} {msg}")


# ── Audit log helper ────────────────────────────────────────────────────────

def _new_audit_log(tmp_dir: Path) -> tuple[AuditLog, Path]:
    p = tmp_dir / f"audit-{uuid.uuid4().hex[:8]}.log"
    return AuditLog(path=p), p


def _read_only_line(p: Path) -> dict:
    """Read exactly one JSON line from the audit file; raise if not 1."""
    lines = p.read_text().splitlines()
    if len(lines) != 1:
        raise AssertionError(f"expected 1 line in {p}, got {len(lines)}")
    return json.loads(lines[0])


# ── Scenarios ───────────────────────────────────────────────────────────────

def scenario_tier0_happy_path(tmp: Path) -> list[str]:
    """Tier 0 read — auto-execute, audit EXECUTED. The happy path."""
    failures: list[str] = []
    step(1, 4, "Tier 0 happy path — fs.read /etc/hostname")

    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "fs.read",
        "target": "/etc/hostname",
        "params": {"path": "/etc/hostname"},
        "reason": "user_requested",
        "risk_level": "read_only",
    }
    info(f"input intent action={intent['action']} target={intent['target']}")

    # (1) Schema validation
    try:
        validated = intent_schema.validate(intent)
        ok(f"intent_schema.validate → ValidatedIntent (schema v{validated.schema_version})")
    except intent_schema.IntentValidationError as e:
        fail(f"intent_schema.validate raised: {e}")
        return [str(e)]

    # (2) Classification — Tier 0
    classification = risk_classifier.classify(validated.intent)
    if classification.tier == risk_classifier.Tier.READ_ONLY:
        ok(f"risk_classifier.classify → Tier {classification.tier.value} "
           f"({classification.tier.name}) — auto_execute={classification.auto_execute}, "
           f"requires_hitl={classification.requires_hitl}")
    else:
        fail(f"expected Tier.READ_ONLY, got {classification.tier.name}")
        failures.append(f"tier {classification.tier.name} != READ_ONLY")

    # (3) Store → opaque UUID
    ref_id = intent_store.put(validated.intent)
    info(f"intent_store.put → ref_id={ref_id}  ({DIM}note: ref_id ≠ intent.intent_id by design{RST})")
    fetched = intent_store.get(ref_id)
    if fetched == validated.intent:
        ok("intent_store.get returns the original intent")
    else:
        fail(f"round-trip mismatch: stored {validated.intent} got {fetched}")
        failures.append("intent_store round-trip mismatch")

    # (4) Audit EXECUTED outcome
    log, log_path = _new_audit_log(tmp)
    try:
        log.write_fields(AuditFields(
            session_id="smoke-sess-1", turn_index=0,
            intent_id=ref_id,
            action=validated.intent["action"],
            target=validated.intent["target"],
            tier=classification.tier.value,
            reason=validated.intent["reason"],
            risk_level=validated.intent["risk_level"],
            outcome=Outcome.EXECUTED,
            duration_ms=12.5,
            backend="mock", model="smoke-test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
        ))
    finally:
        log.close()

    entry = _read_only_line(log_path)
    checks = [
        ("audit row has matching intent_id", entry["intent_id"] == ref_id),
        ("audit row tier == 0",              entry["tier"] == 0),
        ("audit row outcome == executed",    entry["outcome"] == "executed"),
        ("audit row action == fs.read",      entry["action"] == "fs.read"),
        ("audit file mode == 0o640",         (log_path.stat().st_mode & 0o777) == 0o640),
    ]
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            fail(label)
            failures.append(label)

    intent_store.delete(ref_id)
    return failures


def scenario_tier3_hitl_denied(tmp: Path) -> list[str]:
    """Tier 3 destructive — HITL is required; we simulate user pressing [D]."""
    failures: list[str] = []
    step(2, 4, "Tier 3 HITL path — fs.delete /var/log/app.log")

    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "fs.delete",
        "target": "/var/log/app.log",
        "params": {"path": "/var/log/app.log"},
        "reason": "user_requested",
        "risk_level": "high",
    }
    info(f"input intent action={intent['action']} target={intent['target']}")

    validated = intent_schema.validate(intent)
    ok("intent_schema.validate → ValidatedIntent")

    classification = risk_classifier.classify(validated.intent)
    if classification.tier == risk_classifier.Tier.HIGH and classification.requires_hitl:
        ok(f"risk_classifier.classify → Tier {classification.tier.value} HIGH "
           f"(requires_hitl=True) — would block + prompt user")
    else:
        fail(f"expected Tier.HIGH with requires_hitl, got {classification.tier.name} "
             f"requires_hitl={classification.requires_hitl}")
        failures.append("tier 3 misclassification")

    ref_id = intent_store.put(validated.intent)
    info(f"intent_store.put → ref_id={ref_id}")

    # Simulate the operator pressing [D]eny at the HITL prompt.
    info(f"{YLW}simulating user pressing [D]eny at HITL prompt{RST}")
    log, log_path = _new_audit_log(tmp)
    try:
        log.write_fields(AuditFields(
            session_id="smoke-sess-2", turn_index=0,
            intent_id=ref_id,
            action=validated.intent["action"],
            target=validated.intent["target"],
            tier=classification.tier.value,
            reason=validated.intent["reason"],
            risk_level=validated.intent["risk_level"],
            outcome=Outcome.HITL_DENIED,
            duration_ms=4200.0,
            backend="mock", model="smoke-test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
        ))
    finally:
        log.close()

    entry = _read_only_line(log_path)
    checks = [
        ("audit row tier == 3",                 entry["tier"] == 3),
        ("audit row outcome == hitl_denied",    entry["outcome"] == "hitl_denied"),
        ("audit row action == fs.delete",       entry["action"] == "fs.delete"),
        ("audit row target == /var/log/app.log",entry["target"] == "/var/log/app.log"),
    ]
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            fail(label)
            failures.append(label)

    intent_store.delete(ref_id)
    return failures


def scenario_schema_rejection(tmp: Path) -> list[str]:
    """Malformed intent: shell metachars in target → schema rejects → audit row
    carries the rejection envelope and NEVER reaches classify / store."""
    failures: list[str] = []
    step(3, 4, "Schema-rejected path — shell metachars in target")

    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "fs.read",
        "target": "/etc/hosts; rm -rf /",   # ; is in the metachar denylist
        "params": {},
        "reason": "user_requested",
        "risk_level": "read_only",
    }
    info(f"input intent target={intent['target']!r}  ({YLW}deliberately malicious{RST})")

    rejection: intent_schema.IntentValidationError
    try:
        intent_schema.validate(intent)
        fail("schema accepted a metachar in target — INV-2 violation")
        failures.append("metachar target accepted")
        return failures
    except intent_schema.IntentValidationError as e:
        ok(f"intent_schema.validate rejected — field={e.field_path}, type={e.error_type}")
        rejection = e

    info(f"{DIM}schema rejected before reaching classifier; not calling classify(){RST}")
    info(f"{DIM}schema rejected before reaching intent_store; not calling put(){RST}")

    # Audit row for the schema rejection. The orchestration layer (M2.12)
    # would still record provenance — backend/model are populated with the
    # backend that *generated* the malformed intent (mock here).
    log, log_path = _new_audit_log(tmp)
    try:
        log.write_fields(AuditFields(
            session_id="smoke-sess-3", turn_index=0,
            intent_id="rejected",
            action=intent["action"],
            target=intent["target"],
            tier=3,  # treat malformed worst-case for the audit row
            reason=intent["reason"],
            risk_level="critical",
            outcome=Outcome.SCHEMA_REJECTED,
            duration_ms=0.5,
            backend="mock", model="smoke-test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
            extra=rejection.as_audit_fields(),
        ))
    finally:
        log.close()

    entry = _read_only_line(log_path)
    checks = [
        ("outcome == schema_rejected",  entry["outcome"] == "schema_rejected"),
        ("rejection_field == /target",  entry.get("rejection_field") == "/target"),
        ("rejection_type == pattern",   entry.get("rejection_type") == "pattern"),
        ("rejection_message present",   bool(entry.get("rejection_message"))),
    ]
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            fail(label)
            failures.append(label)

    return failures


def scenario_redaction(tmp: Path) -> list[str]:
    """Intent with an api_key in params: schema accepts (it's syntactically
    valid), but the audit log MUST redact the value before persisting."""
    failures: list[str] = []
    step(4, 4, "Redaction path — api_key in params survives schema, redacted in log")

    fake_secret = "sk-ant-A1B2C3D4E5F6G7H8I9J0K1L2M3N4"
    intent = {
        "intent_id": str(uuid.uuid4()),
        "action": "package.install",
        "target": "curl",
        "params": {"package": "curl", "api_key": fake_secret},
        "reason": "user_requested",
        "risk_level": "medium",
    }
    info(f"input intent params={{'package': 'curl', 'api_key': '{fake_secret[:10]}…'}}")

    try:
        validated = intent_schema.validate(intent)
        ok("intent_schema accepted (syntactically valid — schema doesn't know about secrets)")
    except intent_schema.IntentValidationError as e:
        fail(f"schema rejected unexpectedly: {e}")
        return [str(e)]

    classification = risk_classifier.classify(validated.intent)
    info(f"risk_classifier.classify → Tier {classification.tier.value} {classification.tier.name}")
    ref_id = intent_store.put(validated.intent)

    log, log_path = _new_audit_log(tmp)
    try:
        log.write_fields(AuditFields(
            session_id="smoke-sess-4", turn_index=0,
            intent_id=ref_id,
            action=validated.intent["action"],
            target=validated.intent["target"],
            tier=classification.tier.value,
            reason=validated.intent["reason"],
            risk_level=validated.intent["risk_level"],
            outcome=Outcome.EXECUTED,
            duration_ms=8.0,
            backend="mock", model="smoke-test",
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
            extra={"params": validated.intent["params"]},
        ))
    finally:
        log.close()

    entry = _read_only_line(log_path)
    redacted = entry.get("params", {}).get("api_key")
    package = entry.get("params", {}).get("package")
    raw_text = log_path.read_text()

    checks = [
        ("api_key redacted to placeholder",          redacted == REDACTED_PLACEHOLDER),
        ("package field preserved (not redacted)",   package == "curl"),
        ("raw secret value NOT present in log file", fake_secret not in raw_text),
        ("placeholder appears in raw log",           REDACTED_PLACEHOLDER in raw_text),
        ("caller's original intent dict not mutated",
            validated.intent["params"]["api_key"] == fake_secret),
    ]
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            fail(label)
            failures.append(label)

    intent_store.delete(ref_id)
    return failures


# ── Driver ──────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"{BOLD}{CYN}02_cross_module_smoke{RST}  validate → classify → store → audit")
    print(f"{DIM}exercises M2.0 + M2.1-skeleton + M2.2 + M2.3 together (no brains, no mcpd){RST}")

    with tempfile.TemporaryDirectory(prefix="dbtests-smoke-") as d:
        tmp = Path(d)
        all_failures: list[str] = []
        all_failures += scenario_tier0_happy_path(tmp)
        all_failures += scenario_tier3_hitl_denied(tmp)
        all_failures += scenario_schema_rejection(tmp)
        all_failures += scenario_redaction(tmp)

    print()
    if all_failures:
        print(f"{RED}{BOLD}FAIL{RST}  {len(all_failures)} assertion(s) failed across scenarios")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    print(f"{GRN}{BOLD}PASS{RST}  4/4 scenarios — pipeline wired correctly end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
