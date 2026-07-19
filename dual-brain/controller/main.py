"""Controller — 14-step orchestration pipeline.

Wires QB, PB, mcpd, HITL, audit, and session into a single run_turn() call.
All dependencies are injected so the class is fully testable without live servers.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional

import jsonschema

from .audit import AuditFields, AuditLog, Outcome, make_entry
from .hitl import Decision, HitlPresenter, HitlPrompt, TerminalPresenter
from .presenters import make_presenter
from .intent_schema import IntentValidationError, validate
from .intent_store import IntentStore
from .mcpd_client import JsonRpcError, McpdClient, McpdProcessError, McpdTimeoutError, ToolResult
from .risk_classifier import ClassificationResult, Tier, classify
from .tier0_fast_path import try_fast_path as _try_tier0_fast_path
from .tier2_review import Tier2Reviewer, get_reviewer
from .trust_store import TrustStore
from .logger import SystemLogger
from .backends.base import BrainSchemaError
from .verifier import VerifierConfig, VerifierStrategy, make_verifier, should_skip_verifier

_SUMMARISE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
    },
    "required": ["explanation"],
    "additionalProperties": False,
}

_MAX_MODIFY_CYCLES = 3

_PIPELINE_STEPS = [
    ("qb_intent",           "Generating intent..."),
    ("schema_validation",   "Validating schema..."),
    ("risk_classification", "Classifying risk..."),
    ("tier2_review",        "Reviewing (Tier 2)..."),
    ("trust_consult",       "Checking trust..."),
    ("hitl_gate",           "Awaiting approval..."),
    ("intent_store",        "Storing intent..."),
    ("pb_tool_call",        "Generating tool call..."),
    ("tool_validation",     "Validating tool call..."),
    ("qb_verify",           "Verifying intent..."),
    ("mcpd_dispatch",       "Executing tool..."),
    ("cow_approval",        "Awaiting COW approval..."),
    ("qb_summarize",        "Summarizing result..."),
    ("audit",               "Recording audit..."),
]

_COT_HEADINGS: dict[str, str] = {
    "qb_intent":           "Intent Generation",
    "schema_validation":   "Schema Validation",
    "risk_classification": "Risk Classification",
    "tier2_review":        "Tier-2 Review",
    "trust_consult":       "Trust Check",
    "hitl_gate":           "HITL Approval",
    "intent_store":        "Intent Store",
    "pb_tool_call":        "Tool Call Generation",
    "tool_validation":     "Tool Call Validation",
    "qb_verify":           "Intent Verification",
    "mcpd_dispatch":       "Tool Execution",
    "cow_approval":        "COW Preview",
    "qb_summarize":        "Result Summary",
    "audit":               "Audit Record",
}

_STEP_INDEX: dict[str, int] = {
    name: i for i, (name, _) in enumerate(_PIPELINE_STEPS)
}


# F-42: module-level CoT event builder. `run_turn_streaming` also defines a
# nested `_cot` closure that captures `t0`; sites outside that closure (e.g.
# `_emit_unsupported`) must use this factory instead — passing `t0` explicitly.
# Without this helper, `_emit_unsupported` raised `NameError: name '_cot' is
# not defined` on every `system.unsupported` intent, crashing the turn.
def _make_cot(
    t0: float, name: str, state: str, body: str = "", **data: Any,
):
    """Build a CotEvent for use outside `run_turn_streaming`'s closure.

    Deferred import of CotEvent avoids a top-of-module circular import with
    `.turn_events`; the local import inside `run_turn_streaming` uses the same
    module and gets it from bytecode cache after the first turn.
    """
    from .turn_events import CotEvent
    return CotEvent(
        step_index=_STEP_INDEX.get(name, 0),
        step_name=name,
        step_state=state,
        heading=_COT_HEADINGS.get(name, name),
        body=body,
        data=dict(data) if data else {},
        timestamp_ms=(time.monotonic() - t0) * 1000,
    )


# F-43 + F-47 (2026-07-09): server-owned intent fields must NEVER be trusted
# from model output. Gemini 2.5-flash memorized placeholder values from
# training data and echoed them back — 'd9f0e1c2-b3a4-5678-…' / '12345' /
# 'TBD' / 'DO_NOT_EMIT' for intent_id; '1.0' for schema_version which needs
# the pattern ^\d+\.\d+\.\d+$. Both bypass any prompt instruction to "not
# emit" because Gemini's response_schema constrains it to emit *something*
# for every documented field.
#
# Strategy: whatever the model produced for a server-owned field gets
# discarded and replaced before schema validation runs. intent_store.put()
# generates the ACTUAL opaque UUID that PB sees; the intent_id here is only
# for schema conformance + audit trail. schema_version is bumped to the
# current catalog version; timestamp is server clock at parse time.
#
# Must be called from BOTH run_turn_streaming and _run_turn_inner — the
# streaming path had the F-43 fix but the non-streaming RPC-dispatch path
# didn't, so direct socket callers still saw placeholder UUIDs (F-47b).
def _normalize_server_owned_fields(raw_intent: dict) -> None:
    """Overwrite server-generated intent fields with authoritative values.

    Mutates the dict in place. Idempotent — safe to call multiple times
    with the same input.

    Extend when adding new server-owned fields to the intent schema.
    """
    raw_intent["intent_id"] = str(uuid.uuid4())
    raw_intent["schema_version"] = "1.0.0"
    raw_intent["timestamp"] = time.time()


# F-53 v6.65 (2026-07-10): pipeline-wide exception logging helper. Every
# `except Exception:` site in the daemon should call this before deciding
# what to do with the swallowed exception (surface, retry, fall back, or
# genuinely ignore in a destructor-safe context). Writes a structured line
# to SystemLogger with {source, exc_type, exc_message, traceback_hash} so
# the audit trail records what got swallowed. Users can turn on
# `[dev] verbose_errors = true` in controller.toml to include the full
# traceback in user-visible reasons (off by default).
def _log_exception(logger, source: str, exc: BaseException) -> None:
    """Log a swallowed exception. Never raises."""
    if logger is None:
        return
    import hashlib
    import traceback as _tb
    try:
        tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        tb_hash = hashlib.sha256(tb_text.encode("utf-8", "replace")).hexdigest()[:16]
        logger.log("controller", "exception_swallowed", {
            "source": source,
            "exc_type": type(exc).__name__,
            "exc_message": str(exc)[:1000],
            "traceback_hash": tb_hash,
        })
    except Exception:
        # SystemLogger itself failed — swallow this one for real to avoid
        # infinite recursion when the log file is unwritable.
        pass


def _format_exception_reason(exc: BaseException, *, verbose: bool = False) -> str:
    """Produce a user-visible reason string from an exception."""
    base = f"{type(exc).__name__}: {exc}"[:400]
    if not verbose:
        return base
    import traceback as _tb
    tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    return (base + "\n\n" + tb)[:4000]


# F-52 (2026-07-10): fields the server generates post-parse. The backend
# response_schema is relaxed on these (see _relax_server_owned_for_backend);
# the downstream Controller-level validate() re-enforces the full schema
# AFTER _normalize_server_owned_fields has run.
_SERVER_OWNED_INTENT_FIELDS = ("intent_id", "schema_version", "timestamp")


def _relax_server_owned_for_backend(schema: dict) -> dict:
    """Return a copy of the intent schema with server-owned fields relaxed.

    Concretely: (a) remove them from the top-level ``required`` array so the
    backend doesn't retry when the model omits them; (b) strip their
    ``format`` constraint (UUID / regex) so the backend doesn't reject when
    the model emits placeholder strings like 'DO_NOT_EMIT' or '12345'.

    Retains everything else in the schema unchanged — including the strict
    format on `action`, `target`, and `content`. Only the fields the server
    unconditionally rewrites are relaxed.
    """
    import copy
    relaxed = copy.deepcopy(schema)
    if "required" in relaxed and isinstance(relaxed["required"], list):
        relaxed["required"] = [
            f for f in relaxed["required"] if f not in _SERVER_OWNED_INTENT_FIELDS
        ]
    props = relaxed.get("properties", {})
    for field in _SERVER_OWNED_INTENT_FIELDS:
        entry = props.get(field)
        if not isinstance(entry, dict):
            continue
        entry.pop("format", None)
        entry.pop("pattern", None)
    return relaxed


# F-49 (2026-07-10): verifier retry dispatch.
def _should_retry_verifier(retry_mode: str, vresult: Any) -> bool:
    """Decide whether to run a second verifier vote after the first rejects.

    Modes:
      off                 — never retry
      on_call_failed_only — retry only when the failure reason contains the
                            substring 'verifier call failed' (network flake
                            or single-shot LLM error, not a semantic reject).
                            Matches F-44's original behaviour.
      on_any_rejection    — retry once on any verified=false, regardless of
                            reason. Trades an extra Gemini roundtrip for
                            tolerance of single-vote flake.
      skip_tier_01        — handled at the caller by short-circuiting the
                            whole verifier step for Tier 0/1 intents; when
                            we DO run it (Tier 2/3), retry behaves like
                            on_call_failed_only.
    """
    if retry_mode == "off":
        return False
    reason = (getattr(vresult, "reason", "") or "").lower()
    if retry_mode == "on_any_rejection":
        return True
    if retry_mode == "skip_tier_01":
        # Skip already handled at the caller; retry logic falls back to the
        # conservative default so Tier 2/3 still gets the call-failed retry.
        return "verifier call failed" in reason
    # Default: on_call_failed_only
    return "verifier call failed" in reason

# F-32: actions whose params require a concrete target path — if QB ships
# an empty target or "/" for any of these, the Controller short-circuits with
# a friendly error before hitting the sandbox's cryptic whitelist rejection.
_PATH_REQUIRING_ACTIONS = frozenset({
    "fs.list", "fs.read", "fs.stat", "fs.write", "fs.delete",
    "service.logs", "process.inspect", "package.query",
})

# F-35: single source of truth for actions QB is allowed to emit.
# If QB emits an action outside this set, the Controller REWRITES the intent
# into system.unsupported before risk-classification — never silently invokes
# a bogus tool.
#
# v6.9 P2-4 (2026-07-16 CT scan): sourced from the auto-generated
# `_intent_corpus_supported.SUPPORTED_ACTIONS`, which itself derives
# from `controller/tool_catalogue.yaml` (see
# `scripts/export_mcpd_catalogue.py emit`). Import here instead of
# hand-maintaining a duplicate frozenset — the previous copy drifted
# invisibly whenever someone added a tool to main.py without
# regenerating the corpus artifact. Now drift is structurally
# impossible: main.py and test_intent_corpus.py alias the same object.
from ._intent_corpus_supported import SUPPORTED_ACTIONS as _SUPPORTED_ACTIONS


def _build_qb_input(session: Any, user_input: str) -> str:
    """V6B Stage 2: prepend a <context> XML block to the user query when
    the daemon received a ShellContext for this turn.

    Absent context: return user_input unchanged (backwards-compatible with
    every call path that never sets shell_context).
    Present context: return
        <context>...</context>
        <query>
        <user_input>
        </query>
    XML tags are the pattern QB prompts (see prompts/qb_*.txt "CONTEXT USAGE")
    are trained to key off; wrapping the user's raw text in <query> defends
    against context injection by an untrusted terminal.
    """
    ctx = getattr(session, "shell_context", None)
    if ctx is None:
        return user_input
    preamble = ""
    render = getattr(ctx, "render", None)
    if callable(render):
        # Phase 6 Scope B: pass user-configured caps if available.
        # session.cfg is the SessionConfig dataclass; falls back to
        # render()'s built-in defaults if the session has no cfg
        # attached (unit tests / degraded modes).
        render_kwargs: dict = {}
        session_cfg = getattr(session, "cfg", None)
        if session_cfg is not None:
            max_chars = getattr(session_cfg, "max_shell_context_chars", None)
            max_recent = getattr(session_cfg, "max_recent_commands", None)
            if isinstance(max_chars, int):
                render_kwargs["max_chars"] = max_chars
            if isinstance(max_recent, int):
                render_kwargs["max_recent"] = max_recent
        # v6.9 Task #149 shipping-scope (2026-07-17) — surface prior
        # turn (query, result) pairs so QB can resolve pronouns like
        # "it" / "that" / "the previous result". User's exact ask
        # 2026-07-17: "run the second part of the command after seeing
        # the output of the first" — cross-turn instead of one-shot
        # compound (Plan mode, Task #148). Feed comes from
        # SessionState._qb_messages populated by add_user_message +
        # add_tool_result_summary at main.py:541/1497/1570/2017.
        recent_turns_block = ""
        rrt = getattr(session, "render_recent_turns", None)
        if callable(rrt):
            try:
                # Bounded per-daemon defaults (3 turns, 800 chars). Tunable
                # via cfg.session — see SessionConfig for the knobs.
                _max_turns = 3
                _max_ctx_chars = 800
                if session_cfg is not None:
                    _max_turns = int(
                        getattr(session_cfg, "recent_turns_count", 3) or 0
                    )
                    _max_ctx_chars = int(
                        getattr(session_cfg, "recent_turns_max_chars", 800) or 0
                    )
                if _max_turns > 0:
                    recent_turns_block = rrt(
                        max_turns=_max_turns, max_chars=_max_ctx_chars,
                    ) or ""
            except Exception:  # noqa: BLE001
                # Render failure must never break the turn — degrade to
                # bare context. F-53 pattern.
                recent_turns_block = ""
        if recent_turns_block:
            render_kwargs["recent_turns_block"] = recent_turns_block
        try:
            preamble = render(**render_kwargs) or ""
        except Exception:  # noqa: BLE001
            # F-53: context render can fail if the shell state is unavailable
            # (no cwd, no session). Fall back to bare query; logging here
            # would require plumbing the logger down — not worth the noise.
            preamble = ""
    if not preamble:
        return user_input
    return f"{preamble}\n<query>\n{user_input}\n</query>"


@dataclass(frozen=True)
class TurnResult:
    success: bool
    output: str             # user-facing text (QB summary or error message)
    outcome: Outcome
    tier: int = 0
    backend: str = ""
    duration_ms: float = 0.0
    cost_usd: Optional[float] = None
    tokens_in: int = 0
    tokens_out: int = 0


class Controller:
    """Orchestrates one multi-turn conversation session.

    All collaborators are injected — no live servers required for testing.
    """

    def __init__(
        self,
        cfg: Any,
        *,
        qb_backend: Any,
        pb_backend: Any,
        mcpd_client: McpdClient,
        audit_log: AuditLog,
        store: IntentStore,
        prompt_loader: Any,
        trust_store: TrustStore | None = None,
        presenter_factory: Any = None,
    ) -> None:
        self._cfg = cfg
        self._qb = qb_backend
        self._pb = pb_backend
        self._mcpd = mcpd_client
        self._audit = audit_log
        self._store = store
        self._prompts = prompt_loader
        self._presenter_factory = presenter_factory
        self._trust_store = trust_store or (
            TrustStore() if cfg.hitl.trust_ttl_seconds > 0 else None
        )
        self._tier2: Tier2Reviewer | None = None
        if getattr(cfg, "tier2", None) and cfg.tier2.enabled:
            self._tier2 = get_reviewer(
                cfg.tier2.strategy,
                qb_backend=qb_backend,
                max_retries=cfg.tier2.max_retries,
            )
        self._schemas_dir = self._resolve_schemas_dir()
        self._intent_schema: dict = json.loads(
            (Path(__file__).parent / "schemas" / "intent.json").read_text(encoding="utf-8")
        )
        # v6.9 Scope O Layer 2 Part A — load declarative controller-side
        # tool manifests from controller/manifests/*.yaml. Every manifest
        # is validated against schemas/tool_manifest.json at load time;
        # a malformed manifest raises here, blocking daemon startup.
        # session_op (nav.cd) is the only shipped impl.kind for Part A;
        # Part B adds fs_read/fs_write_cow/dbus_call/exec_pipeline via
        # the mcpd-side loader.
        from .manifest_loader import load as _load_manifests
        self._manifests = _load_manifests()
        # manifest_loader logs per-registered-manifest at INFO.
        # F-52 (2026-07-10): the backend applies response_schema BEFORE
        # returning content_json, so any field the model gets "wrong" here
        # triggers a retry loop and eventually leaks as an error. But we
        # regenerate `intent_id`, `schema_version`, and `timestamp`
        # server-side (see _normalize_server_owned_fields) — the model's
        # value is discarded before validate() ever sees it. Presenting a
        # schema that requires format=uuid on intent_id creates a false
        # rejection loop: Gemini 2.5-flash memorizes and echoes placeholder
        # UUIDs like 'DO_NOT_EMIT', backend rejects, retries, gets the same
        # placeholder, gives up. Solve by handing the backend a schema that
        # drops the "required" flag and "format" constraint on these three
        # fields. Downstream validate() still enforces the full schema
        # AFTER normalization.
        self._backend_intent_schema: dict = _relax_server_owned_for_backend(
            self._intent_schema
        )
        vcfg = getattr(cfg, "verifier", None) or VerifierConfig()
        self._verifier: VerifierStrategy = make_verifier(vcfg)
        self._rpa_step_events: list = []
        self._system_logger = SystemLogger("/var/log/icebreaker/system.jsonl")

    def backend_name(self) -> str:
        return self._cfg.qb.name

    # v6.8 Task #147: lazy AgentGraph accessor. Defers the SqliteSaver +
    # graph.compile() cost to the first turn that actually needs it.
    # Held in a private attr; init to None in the class body so the
    # accessor is idempotent + testable via `ctrl._agent_graph = mock`.
    _agent_graph: Any = None

    def _get_agent_graph(self) -> Any:
        if self._agent_graph is None:
            from .agent_graph import AgentGraph
            from .session_store import SessionStore
            from .risk_classifier import classify
            agent_cfg = getattr(self._cfg, "agent_graph", None)
            self._agent_graph = AgentGraph(
                cfg=agent_cfg,
                session_store=SessionStore(),
                qb_backend=self._qb,
                pb_backend=self._pb,
                mcpd_client=self._mcpd,
                audit_log=self._audit,
                risk_classify=classify,
                verifier=self._verifier,
                prompts=self._prompts,
                intent_schema=self._intent_schema,
                controller_cfg=self._cfg,
                intent_store=self._store,
            )
        return self._agent_graph

    def run_turn(self, user_input: str, session: Any) -> TurnResult:
        t0 = time.monotonic()
        try:
            result = self._run_turn_inner(user_input, session, t0)
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            _log_exception(self._system_logger, "controller.run_turn", exc)
            try:
                self._audit.write_fields(self._make_error_fields(session, str(exc), duration))
            except Exception as audit_exc:
                # F-53: audit failure is rare but critical to know about.
                _log_exception(self._system_logger, "controller.audit_write", audit_exc)
            session.touch()
            return TurnResult(
                success=False,
                output=f"Internal error: {type(exc).__name__}: {exc}",
                outcome=Outcome.BRAIN_ERROR,
                backend=session.backend,
                duration_ms=duration,
            )
        return result

    def run_turn_streaming(
        self, user_input: str, session: Any
    ) -> Generator:
        """Yield TurnEvent objects for each pipeline step.

        The existing ``run_turn()`` is unchanged (BP-2 backward compat).
        """
        # v6.8 Task #147 (2026-07-13): AgentGraph migration branch. When
        # `cfg.agent_graph.enabled` is True (default False), delegate to
        # the LangGraph runtime via the bridge. Old pipeline stays
        # unchanged for the flag-off default so shipped v6.7 behavior is
        # preserved until Task #154 flips the flag after UTM sweep.
        _agent_cfg = getattr(self._cfg, "agent_graph", None)
        if _agent_cfg is not None and getattr(_agent_cfg, "enabled", False):
            from .agent_graph_bridge import run_via_agent_graph
            yield from run_via_agent_graph(
                self._get_agent_graph(),
                self._build_presenter(),
                user_input, session,
                backend=self.backend_name(),
            )
            return

        from .turn_events import (
            CotEvent,
            ErrorEvent,
            GuiEvent,
            InfoEvent,
            ProgressEvent,
            ResultEvent,
            RpaEvent,
            TokenEvent,
        )

        t0 = time.monotonic()
        step_idx = 0
        total = len(_PIPELINE_STEPS)
        current_step = ""

        def _cot(
            name: str, state: str, body: str = "", **data: Any,
        ) -> CotEvent:
            return CotEvent(
                step_index=_STEP_INDEX.get(name, 0),
                step_name=name,
                step_state=state,
                heading=_COT_HEADINGS.get(name, name),
                body=body,
                data=dict(data) if data else {},
                timestamp_ms=(time.monotonic() - t0) * 1000,
            )

        def _progress(name: str) -> ProgressEvent:
            nonlocal step_idx, current_step
            for i, (sn, sl) in enumerate(_PIPELINE_STEPS):
                if sn == name:
                    step_idx = i
                    current_step = name
                    return ProgressEvent(
                        step_name=sn,
                        step_label=sl,
                        step_index=i,
                        total_steps=total,
                        elapsed_ms=(time.monotonic() - t0) * 1000,
                    )
            current_step = name
            return ProgressEvent(
                step_name=name,
                step_label=name,
                step_index=step_idx,
                total_steps=total,
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

        try:
            qb_system = self._prompts.get(f"qb_{session.backend}")
            qb_cost = 0.0
            qb_tokens_in = qb_tokens_out = 0

            # Step 1: QB → Intent Object
            yield _progress("qb_intent")
            yield _cot("qb_intent", "active",
                        body="Parsing natural language into structured intent")
            session.add_user_message(user_input)
            # V6B Stage 2: prepend a <context> block if the caller provided
            # shell state (cwd, recent commands). Lets QB resolve "here",
            # "this folder", relative paths against the actual environment.
            qb_input = _build_qb_input(session, user_input)
            qb_response = self._qb.complete(
                system=qb_system, user=qb_input,
                schema=self._backend_intent_schema,
                max_retries=self._cfg.run.qb_max_retries,
            )
            raw_intent = qb_response.content_json
            # F-43 + F-47 (2026-07-09): overwrite server-owned fields — see
            # _normalize_server_owned_fields() docstring for the full context.
            _normalize_server_owned_fields(raw_intent)
            session.add_assistant_message(json.dumps(raw_intent))
            qb_cost += qb_response.cost_usd or 0.0
            qb_tokens_in += qb_response.tokens_in
            qb_tokens_out += qb_response.tokens_out
            yield _cot("qb_intent", "done", body="Intent generated",
                        action=raw_intent.get("action", ""),
                        target=raw_intent.get("target", ""),
                        risk_level=raw_intent.get("risk_level", ""))

            # Step 2: Schema validation
            yield _progress("schema_validation")
            yield _cot("schema_validation", "active",
                        body="Validating intent against JSON Schema (INV-2)")
            try:
                validated = validate(raw_intent)
            except IntentValidationError as exc:
                yield _cot("schema_validation", "failed", body=str(exc))
                yield ResultEvent(
                    result=self._schema_rejected(session, str(exc), qb_response, t0)
                )
                return
            yield _cot("schema_validation", "done", body="Schema valid")

            intent = validated.intent
            raw_target = intent.get("target", "")
            if raw_target:
                intent["target_realpath"] = os.path.realpath(raw_target)
            else:
                intent["target_realpath"] = ""

            # F-32: QB sometimes emits target="" or target="/" for queries too
            # ambiguous to resolve via context, which then fails downstream at
            # mcpd with a cryptic "not under any whitelisted root" error.
            # Short-circuit here with a friendly, actionable message before
            # spending PB tokens or hitting the sandbox.
            if intent["action"] in _PATH_REQUIRING_ACTIONS and raw_target in ("", "/"):
                friendly = (
                    "Query too ambiguous to route safely. Try naming a "
                    "specific directory (e.g. 'here', 'my Documents folder', "
                    "'/tmp'). Received target: "
                    + (repr(raw_target) if raw_target else "(empty)")
                )
                yield _cot("schema_validation", "failed", body=friendly)
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(self._make_error_fields(
                    session, friendly, duration))
                yield ResultEvent(result=TurnResult(
                    success=False,
                    output=friendly,
                    outcome=Outcome.SCHEMA_REJECTED,
                    tier=0,
                    backend=session.backend, duration_ms=duration,
                    cost_usd=qb_cost if qb_cost > 0 else None,
                    tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
                ))
                return

            # F-35: catalogue landing pad. If QB emitted an action not in the
            # supported set (either the explicit system.unsupported, or an
            # unlisted action that slipped past syntax-only schema validation),
            # short-circuit here with a friendly UNSUPPORTED card. Never invoke
            # PB or mcpd — the point is to tell the user "I can't do that"
            # instead of silently substituting a lookalike.
            if (intent["action"] == "system.unsupported"
                    or intent["action"] not in _SUPPORTED_ACTIONS):
                for ev in self._emit_unsupported(
                    session, intent, user_input, qb_response,
                    qb_cost, qb_tokens_in, qb_tokens_out, t0,
                ):
                    yield ev
                return

            try:
                self._system_logger.log("controller", "intent_generated", {"intent": intent})
            except Exception as e:
                yield _cot("system_logger", "failed", body=f"Failed to log intent: {e}")

            # Step 3: Risk classification
            yield _progress("risk_classification")
            yield _cot("risk_classification", "active",
                        body="Classifying risk tier")
            cls_result = classify(intent)
            yield _cot("risk_classification", "done",
                        body=f"Tier {int(cls_result.tier)} — {cls_result.reason}",
                        tier=int(cls_result.tier),
                        reason=cls_result.reason,
                        reversible=cls_result.reversible)

            # Step 3b: Tier-2 review
            original_tier = cls_result.tier
            if self._tier2 and cls_result.tier == Tier.MEDIUM:
                yield _progress("tier2_review")
                yield _cot("tier2_review", "active",
                            body="Escalate-only second review")
                t2_decision = self._tier2.review(intent, cls_result)
                if t2_decision.escalate:
                    cls_result = ClassificationResult(
                        tier=Tier.HIGH, reason=t2_decision.reason,
                        reversible=cls_result.reversible,
                    )
                assert cls_result.tier >= original_tier
                yield _cot("tier2_review", "done",
                            body="Escalated to Tier 3" if t2_decision.escalate
                            else "No escalation",
                            escalated=t2_decision.escalate,
                            final_tier=int(cls_result.tier))
            else:
                yield _cot("tier2_review", "done", body="Skipped",
                            skipped=True)

            # Step 3c: Trust consult
            trust_ttl = self._cfg.hitl.trust_ttl_seconds
            cls_result_requires_hitl = cls_result.requires_hitl
            if self._trust_store and trust_ttl > 0 and cls_result.requires_hitl:
                yield _progress("trust_consult")
                yield _cot("trust_consult", "active",
                            body="Checking trust grants")
                grant = self._trust_store.is_trusted(
                    intent["action"], intent["target"],
                    cls_result.tier, session.session_id,
                )
                if grant:
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id,
                        turn_index=session.turn_index,
                        intent_id="", action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"],
                        risk_level=intent["risk_level"],
                        outcome=Outcome.TRUST_APPLIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
                        cost_estimate_usd=qb_cost,
                        extra={"grant_id": grant.grant_id},
                    ))
                    cls_result_requires_hitl = False
                    yield _cot("trust_consult", "done",
                                body="Auto-approved via trust grant",
                                trusted=True,
                                grant_id=grant.grant_id)
                else:
                    yield _cot("trust_consult", "done",
                                body="No matching trust grant",
                                trusted=False)
            else:
                yield _cot("trust_consult", "done", body="Skipped",
                            skipped=True)

            # Step 4: HITL gate
            if cls_result_requires_hitl:
                yield _progress("hitl_gate")
                yield _cot("hitl_gate", "active",
                            body=f"Tier {int(cls_result.tier)} — awaiting human decision")
                modify_count = 0
                while True:
                    hitl_prompt = HitlPrompt(
                        intent, cls_result, backend=session.backend,
                        lockout_seconds=self._cfg.hitl.lockout_seconds,
                        timeout_seconds=self._cfg.hitl.timeout_seconds,
                        presenter=self._build_presenter(),
                    )
                    decision = hitl_prompt.ask()
                    hitl_extra = {
                        "decision_id": hitl_prompt.decision_id,
                        "key_pressed_class": hitl_prompt.key_pressed_class,
                        "latency_ms": hitl_prompt.decision_latency_ms,
                    }
                    if decision == Decision.EXPLAIN:
                        explanation = self._qb_explain(intent, cls_result)
                        yield InfoEvent(message=f"\n  {explanation}\n")
                        continue
                    if decision == Decision.MODIFY:
                        modify_count += 1
                        if modify_count >= _MAX_MODIFY_CYCLES:
                            yield _cot("hitl_gate", "failed",
                                        body="Modify limit reached — denied",
                                        decision="modify_limit")
                            yield InfoEvent(message="\n  Modify limit reached — operation denied.")
                            yield ResultEvent(result=self._denied(
                                session, intent, cls_result, Decision.DENIED,
                                qb_response, t0, extra=hitl_extra,
                            ))
                            return
                        yield InfoEvent(message="\n  [Modify] — not yet wired to intent revision (M5.1d)")
                        continue
                    if decision == Decision.TRUST:
                        if trust_ttl <= 0:
                            yield InfoEvent(message="\n  Trust is disabled in config (trust_ttl_seconds = 0).\n")
                            continue
                        if cls_result.tier >= Tier.HIGH:
                            yield InfoEvent(message="\n  Cannot trust Tier 3+ operations.\n")
                            continue
                        grant = self._trust_store.grant(
                            action=intent["action"],
                            target_prefix=intent["target"],
                            max_tier=cls_result.tier,
                            session_id=session.session_id,
                            ttl_seconds=trust_ttl,
                        )
                        self._audit.write_fields(AuditFields(
                            session_id=session.session_id,
                            turn_index=session.turn_index,
                            intent_id="", action=intent["action"],
                            target=intent["target"], tier=int(cls_result.tier),
                            reason=intent["reason"],
                            risk_level=intent["risk_level"],
                            outcome=Outcome.TRUST_GRANTED, duration_ms=0,
                            backend=session.backend, model=self._cfg.qb.model,
                            tokens_in=0, tokens_out=0,
                            cost_estimate_usd=0.0,
                            extra={"grant_id": grant.grant_id, "ttl_seconds": trust_ttl, **hitl_extra},
                        ))
                        yield _cot("hitl_gate", "done",
                                    body="Trust granted — approved",
                                    decision="trust")
                        break
                    if decision != Decision.APPROVED:
                        yield _cot("hitl_gate", "failed",
                                    body="Operation denied by user",
                                    decision=decision.name.lower())
                        yield ResultEvent(result=self._denied(
                            session, intent, cls_result, decision,
                            qb_response, t0, extra=hitl_extra,
                        ))
                        return
                    yield _cot("hitl_gate", "done",
                                body="Approved",
                                decision="approved")
                    break

            else:
                yield _cot("hitl_gate", "done", body="Skipped (no HITL required)",
                            skipped=True)

            # Step 5: Store intent
            yield _progress("intent_store")
            yield _cot("intent_store", "active", body="Persisting intent as opaque UUID")
            intent_id = self._store.put(intent)
            yield _cot("intent_store", "done", body="Stored",
                        intent_id=intent_id)

            # v6.8 M7.2 (2026-07-13): Tier-0 fast path. For read-only intents
            # whose intent → tool_call mapping is deterministic (fs.list,
            # system.status, etc.) skip the PB round-trip. INV-2 (schema
            # validation) still fires at Step 7 on the constructed tool_call.
            # Guarded by run.tier0_fast_path config (default True). Defensive
            # attribute walks match the pattern the other Scope B gates use.
            _rcfg = getattr(self._cfg, "run", None)
            _fast_path_enabled = getattr(_rcfg, "tier0_fast_path", True)
            _fast_path_tool_call = None
            if _fast_path_enabled:
                _fast_path_tool_call = _try_tier0_fast_path(
                    intent, int(cls_result.tier),
                )

            if _fast_path_tool_call is not None:
                yield _progress("pb_tool_call")
                yield _cot("pb_tool_call", "done",
                            body="Tier-0 fast path — skipped PB grammar-decode",
                            skipped=True, skip_reason="tier0_fast_path",
                            tool=_fast_path_tool_call.get("tool", ""))
                tool_call = _fast_path_tool_call
                pb_cost = 0.0
                total_cost = qb_cost
                total_tokens_in = qb_tokens_in
                total_tokens_out = qb_tokens_out
            else:
                # Step 6: PB → tool call (slow path)
                yield _progress("pb_tool_call")
                yield _cot("pb_tool_call", "active",
                            body="Privileged Brain generating MCP tool call")
                tool_schema = self._get_tool_schema(intent["action"])
                # F-27: pass the validated intent target so PB doesn't hallucinate
                # F-41: forward QB's `content` + `pb_hint` when present so PB has
                # the verbatim bytes + format coaching it needs (fixes the "column
                # of ones" CSV bug and similar content-fabrication regressions).
                pb_user = session.build_pb_user_turn(
                    intent_id, intent["action"], tool_schema,
                    target=intent.get("target", ""),
                    content=intent.get("content", ""),
                    pb_hint=intent.get("pb_hint", ""),
                )
                pb_system = self._prompts.get("pb")
                try:
                    pb_response = self._pb.complete(
                        system=pb_system, user=pb_user, schema=None, max_retries=1,
                    )
                    tool_call = pb_response.content_json
                except BrainSchemaError as exc:
                    raw = exc.last_payload_excerpt.strip()
                    if raw.upper().startswith("REFUSE:"):
                        reason = raw[len("REFUSE:"):].strip()
                        yield _cot("pb_tool_call", "failed",
                                    body=f"PB refused: {reason}")
                        duration = (time.monotonic() - t0) * 1000
                        self._audit.write_fields(AuditFields(
                            session_id=session.session_id,
                            turn_index=session.turn_index,
                            intent_id=intent_id, action=intent["action"],
                            target=intent["target"], tier=int(cls_result.tier),
                            reason=intent["reason"],
                            risk_level=intent["risk_level"],
                            outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                            backend=session.backend, model=self._cfg.qb.model,
                            tokens_in=total_tokens_in, tokens_out=0,
                            cost_estimate_usd=qb_cost,
                            extra={"pb_refused": True,
                                   "refusal_reason": reason},
                        ))
                        yield ResultEvent(result=TurnResult(
                            success=False,
                            output=f"Privileged Brain refused: {reason}",
                            outcome=Outcome.PB_SCHEMA_ERROR,
                            tier=int(cls_result.tier),
                            backend=session.backend, duration_ms=duration,
                            cost_usd=qb_cost if qb_cost > 0 else None,
                            tokens_in=total_tokens_in, tokens_out=0,
                        ))
                        return
                    raise
                pb_cost = pb_response.cost_usd or 0.0
                total_cost = qb_cost + pb_cost
                total_tokens_in = qb_tokens_in + pb_response.tokens_in
                total_tokens_out = qb_tokens_out + pb_response.tokens_out
                yield _cot("pb_tool_call", "done",
                            body=f"Tool call: {tool_call.get('tool', '?')}",
                            tool=tool_call.get("tool", ""))

            # Step 7: Validate tool call
            yield _progress("tool_validation")
            yield _cot("tool_validation", "active",
                        body="Validating PB output against tool schema")
            try:
                self._validate_tool_call(tool_call, intent["action"])
            except ValueError as exc:
                yield _cot("tool_validation", "failed", body=str(exc))
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                ))
                yield ResultEvent(result=TurnResult(
                    success=False, output=f"PB output validation failed: {exc}",
                    outcome=Outcome.PB_SCHEMA_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return
            yield _cot("tool_validation", "done", body="Tool call valid")

            # Step 8: QB verifier
            yield _progress("qb_verify")
            verifier_system = self._prompts.get("qb_verifier")
            # v6.8 M7.1 (2026-07-13): tier_floor is the first-class skip gate.
            # F-49 skip_tier_01 retry_mode is honored for backward compat via
            # verifier.should_skip_verifier(). Both call sites (this streaming
            # branch and the non-streaming branch at ~L1745) go through the
            # same helper so the semantics can't drift.
            _vcfg = getattr(self._cfg, "verifier", None)
            retry_mode = getattr(_vcfg, "retry_mode", "on_call_failed_only")
            _skip_verifier = _vcfg is not None and should_skip_verifier(_vcfg, int(cls_result.tier))
            if _skip_verifier:
                _tier_floor = getattr(_vcfg, "tier_floor", 2)
                yield _cot("qb_verify", "done",
                            body=f"Skipped (tier={int(cls_result.tier)} < tier_floor={_tier_floor})",
                            skipped=True, retry_mode=retry_mode,
                            tier_floor=_tier_floor,
                            skip_reason="tier_gate")
                # Fake a passing vresult so downstream flow continues.
                from .verifier import VerifierResult
                vresult = VerifierResult(
                    verified=True,
                    reason=f"skipped (tier < tier_floor)",
                    votes_cast=0,
                    verified_count=0,
                )
            else:
                yield _cot("qb_verify", "active",
                            body="QB verifying intent↔tool-call alignment",
                            retry_mode=retry_mode)
                vresult = self._verifier.verify(
                    intent, tool_call, self._qb, verifier_system,
                )
                if not vresult.verified and _should_retry_verifier(
                    retry_mode, vresult
                ):
                    yield _cot("qb_verify", "active",
                                body=f"Retry 1/1 ({retry_mode}): {vresult.reason}",
                                retry_mode=retry_mode)
                    vresult = self._verifier.verify(
                        intent, tool_call, self._qb, verifier_system,
                    )
            if not vresult.verified:
                yield _cot("qb_verify", "failed",
                            body=f"Rejected: {vresult.reason}",
                            reason=vresult.reason,
                            votes_cast=vresult.votes_cast,
                            verified_count=vresult.verified_count)
                duration = (time.monotonic() - t0) * 1000
                extra = {}
                if vresult.votes_cast > 1:
                    extra["verifier_votes"] = vresult.votes_cast
                    extra["verified_count"] = vresult.verified_count
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.QB_VERIFIER_REJECTED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra=extra if extra else None,
                ))
                yield ResultEvent(result=TurnResult(
                    success=False,
                    output=f"Verifier rejected: {vresult.reason}",
                    outcome=Outcome.QB_VERIFIER_REJECTED, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                ))
                return
            yield _cot("qb_verify", "done",
                        body="Verified",
                        votes_cast=vresult.votes_cast,
                        verified_count=vresult.verified_count)

            # Step 9: tool dispatch (mcpd, GUI Agent, or RPA Bridge)
            tool_name = tool_call.get("tool", "")
            is_gui = tool_name.startswith("gui.")
            is_rpa = tool_name.startswith("rpa.")

            yield _progress("mcpd_dispatch")
            if is_gui:
                yield _cot("mcpd_dispatch", "active",
                            body=f"Executing {tool_name} via GUI Agent",
                            tool=tool_name, execution_tier="gui")
                yield GuiEvent(
                    phase="preview",
                    action=tool_name,
                    window_title=tool_call.get("params", {}).get("window", ""),
                    element_role=tool_call.get("params", {}).get("role", ""),
                    element_name=tool_call.get("params", {}).get("name", ""),
                    predicted_outcome=f"{tool_name} on target element",
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )
                gui_cfg = getattr(self._cfg, "gui", None)
                if gui_cfg and not gui_cfg.enabled:
                    yield _cot("mcpd_dispatch", "failed",
                                body="GUI Agent disabled in config")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.GUI_DENIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "gui"},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False,
                        output="GUI automation is disabled. Enable with [gui] enabled = true.",
                        outcome=Outcome.GUI_DENIED, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                yield GuiEvent(
                    phase="executing",
                    action=tool_name,
                    window_title=tool_call.get("params", {}).get("window", ""),
                    element_role=tool_call.get("params", {}).get("role", ""),
                    element_name=tool_call.get("params", {}).get("name", ""),
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                try:
                    from gui_agent.agent import GuiAgent
                    # v6.10 P6 (F-73): pass the whole GuiConfig so
                    # retention + prefer_app_api + a11y_timeout_ms
                    # actually reach the Agent (they were dead knobs
                    # before). Backward compat: legacy scratch_dir path
                    # still works when gui_cfg is None.
                    gui = GuiAgent(config=gui_cfg) if gui_cfg else GuiAgent()
                    gui_result = gui.handle_request(
                        tool_name, tool_call.get("params", {}),
                    )
                except Exception as exc:  # noqa: BLE001
                    # F-53 Scope A.P3: `str(exc)` is surfaced twice —
                    # once via `_cot(...body=str(exc))` for CoT display
                    # and once via `GuiEvent(error=str(exc))` for the
                    # companion panel. Nothing swallowed.
                    yield _cot("mcpd_dispatch", "failed", body=str(exc))
                    yield GuiEvent(
                        phase="complete",
                        action=tool_name,
                        window_title=tool_call.get("params", {}).get("window", ""),
                        error=str(exc),
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.GUI_ERROR, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "gui", "gui_error": str(exc)},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False, output=f"GUI error: {exc}",
                        outcome=Outcome.GUI_ERROR, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                gui_error = gui_result.get("error")
                gui_success = gui_result.get("success", True) if gui_error is None else False
                gui_reason = gui_result.get("reason", "")

                # ── GUI→RPA escalation check ──
                _ESCALATABLE_REASONS = frozenset({
                    "no_a11y_tree", "atspi_unavailable", "element_too_small",
                })
                rpa_cfg = getattr(self._cfg, "rpa", None)
                if (
                    not gui_success
                    and gui_reason in _ESCALATABLE_REASONS
                    and rpa_cfg
                    and rpa_cfg.enabled
                ):
                    yield _cot("mcpd_dispatch", "active",
                                body=f"GUI failed ({gui_reason}) — escalating to RPA Bridge",
                                execution_tier="rpa", gui_reason=gui_reason)

                    rpa_keywords = self._gui_action_to_rpa_keywords(
                        tool_name, tool_call.get("params", {}),
                    )
                    rpa_workflow_name = f"escalation_{tool_name}"
                    rpa_timeout = rpa_cfg.timeout_seconds

                    keyword_preview = tuple(
                        f"{kw.get('name', '?')}  {' '.join(kw.get('args', []))}"
                        for kw in rpa_keywords
                    )

                    yield RpaEvent(
                        phase="preview",
                        workflow_name=rpa_workflow_name,
                        keyword_total=len(rpa_keywords),
                        timeout_remaining_ms=rpa_timeout * 1000,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    rpa_result = self._execute_rpa_workflow(
                        rpa_workflow_name, rpa_keywords, rpa_timeout,
                        session, intent, t0,
                    )

                    # Drain per-keyword step events collected during execution
                    for step_event in self._rpa_step_events:
                        yield step_event
                    self._rpa_step_events.clear()

                    rpa_success = rpa_result.get("success", False)
                    rpa_error = rpa_result.get("error", "")
                    rpa_timed_out = rpa_result.get("timed_out", False)

                    if rpa_timed_out:
                        rpa_outcome = Outcome.RPA_TIMEOUT
                    elif rpa_success:
                        rpa_outcome = Outcome.RPA_EXECUTED
                    elif rpa_result.get("qb_paused_at_keyword") is not None:
                        rpa_outcome = Outcome.RPA_QB_PAUSED
                    else:
                        rpa_outcome = Outcome.RPA_ERROR

                    from .audit import sanitize_gui_field
                    rpa_extra: dict[str, Any] = {
                        "execution_tier": "rpa_fallback",
                        "rpa_fallback_reason": gui_reason,
                        "rpa_keywords_executed": rpa_result.get("keywords_executed", 0),
                        "rpa_timeout_ms": rpa_timeout * 1000,
                        "rpa_elapsed_ms": rpa_result.get("elapsed_ms", 0),
                        "rpa_screenshot_hashes": rpa_result.get("screenshot_hashes", []),
                    }

                    yield RpaEvent(
                        phase="complete",
                        workflow_name=rpa_workflow_name,
                        keyword_index=rpa_result.get("keywords_executed", 0),
                        keyword_total=len(rpa_keywords),
                        error=rpa_error,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=rpa_outcome, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra=rpa_extra,
                    ))

                    from .mcpd_client import ToolResult
                    tool_result = ToolResult(result=rpa_result, request_id=0)
                    yield _cot("mcpd_dispatch", "done",
                                body=f"RPA fallback {'complete' if rpa_success else 'failed'}",
                                execution_tier="rpa_fallback",
                                requires_cow=False)
                else:
                    # Normal GUI result (success or non-escalatable error)
                    gui_outcome = Outcome.GUI_EXECUTED if gui_success else Outcome.GUI_ERROR

                    from .audit import sanitize_gui_field
                    extra_fields: dict[str, Any] = {
                        "execution_tier": "gui",
                        "element_role": sanitize_gui_field(
                            tool_call.get("params", {}).get("role", ""),
                        ),
                        "element_name": sanitize_gui_field(
                            tool_call.get("params", {}).get("name", ""),
                        ),
                        "window_title": sanitize_gui_field(
                            tool_call.get("params", {}).get("window", ""),
                        ),
                    }
                    sha = gui_result.get("sha256", "")
                    if sha:
                        extra_fields["screenshot_before_hash"] = sha

                    yield GuiEvent(
                        phase="complete",
                        action=tool_name,
                        window_title=tool_call.get("params", {}).get("window", ""),
                        element_role=tool_call.get("params", {}).get("role", ""),
                        element_name=tool_call.get("params", {}).get("name", ""),
                        screenshot_before_hash=sha,
                        error=gui_error or "",
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    )

                    from .mcpd_client import ToolResult
                    tool_result = ToolResult(
                        result=gui_result, request_id=0,
                    )
                    yield _cot("mcpd_dispatch", "done",
                                body=f"GUI execution {'complete' if gui_success else 'failed'}",
                                execution_tier="gui",
                                requires_cow=False)
            elif is_rpa:
                yield _cot("mcpd_dispatch", "active",
                            body=f"Executing {tool_name} via RPA Bridge",
                            tool=tool_name, execution_tier="rpa")

                rpa_cfg = getattr(self._cfg, "rpa", None)
                if rpa_cfg and not rpa_cfg.enabled:
                    yield _cot("mcpd_dispatch", "failed",
                                body="RPA Bridge disabled in config")
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.RPA_DENIED, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                        extra={"execution_tier": "rpa"},
                    ))
                    yield ResultEvent(result=TurnResult(
                        success=False,
                        output="RPA automation is disabled. Enable with [rpa] enabled = true.",
                        outcome=Outcome.RPA_DENIED, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    ))
                    return

                rpa_params = tool_call.get("params", {})
                rpa_keywords = rpa_params.get("keywords", [])
                rpa_workflow_name = rpa_params.get("workflow_name", "workflow")
                rpa_timeout = rpa_cfg.timeout_seconds if rpa_cfg else 30

                keyword_preview = tuple(
                    f"{kw.get('name', '?')}  {' '.join(kw.get('args', []))}"
                    for kw in rpa_keywords
                )

                yield RpaEvent(
                    phase="preview",
                    workflow_name=rpa_workflow_name,
                    keyword_total=len(rpa_keywords),
                    timeout_remaining_ms=rpa_timeout * 1000,
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                rpa_result = self._execute_rpa_workflow(
                    rpa_workflow_name, rpa_keywords, rpa_timeout,
                    session, intent, t0,
                )

                for step_event in self._rpa_step_events:
                    yield step_event
                self._rpa_step_events.clear()

                rpa_success = rpa_result.get("success", False)
                rpa_error = rpa_result.get("error", "")
                rpa_timed_out = rpa_result.get("timed_out", False)

                if rpa_timed_out:
                    rpa_outcome = Outcome.RPA_TIMEOUT
                elif rpa_success:
                    rpa_outcome = Outcome.RPA_EXECUTED
                elif rpa_result.get("qb_paused_at_keyword") is not None:
                    rpa_outcome = Outcome.RPA_QB_PAUSED
                else:
                    rpa_outcome = Outcome.RPA_ERROR

                from .audit import sanitize_gui_field
                rpa_extra: dict[str, Any] = {
                    "execution_tier": "rpa",
                    "rpa_keywords_executed": rpa_result.get("keywords_executed", 0),
                    "rpa_timeout_ms": rpa_timeout * 1000,
                    "rpa_elapsed_ms": rpa_result.get("elapsed_ms", 0),
                    "rpa_screenshot_hashes": rpa_result.get("screenshot_hashes", []),
                    "rpa_fallback_reason": sanitize_gui_field(
                        rpa_params.get("fallback_reason", "")
                    ),
                }

                yield RpaEvent(
                    phase="complete",
                    workflow_name=rpa_workflow_name,
                    keyword_index=rpa_result.get("keywords_executed", 0),
                    keyword_total=len(rpa_keywords),
                    error=rpa_error,
                    timestamp_ms=(time.monotonic() - t0) * 1000,
                )

                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=rpa_outcome, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra=rpa_extra,
                ))

                from .mcpd_client import ToolResult
                tool_result = ToolResult(result=rpa_result, request_id=0)
                yield _cot("mcpd_dispatch", "done",
                            body=f"RPA execution {'complete' if rpa_success else 'failed'}",
                            execution_tier="rpa",
                            requires_cow=False)
            else:
                # v6.9 Scope O Layer 2 Part A: consult manifest registry
                # BEFORE mcpd. Manifest-served tools (nav.cd today) never
                # touch mcpd — pure controller-side session state. If no
                # manifest matches, fall through to the mcpd path.
                _manifest_result = self._try_manifest_dispatch(tool_call, session)
                if _manifest_result is not None:
                    yield _cot("mcpd_dispatch", "active",
                                body=f"Executing {tool_name} via manifest",
                                tool=tool_name, dispatch="manifest")
                    tool_result = _manifest_result
                    yield _cot("mcpd_dispatch", "done",
                                body=f"{tool_name} manifest dispatch complete",
                                tool=tool_name, dispatch="manifest")
                else:
                    yield _cot("mcpd_dispatch", "active",
                                body=f"Executing {tool_name} via mcpd",
                                tool=tool_name)
                    try:
                        tool_result = self._mcpd.call(
                            tool_call["tool"], tool_call.get("params"),
                            timeout=self._cfg.run.mcpd_timeout_seconds,
                        )
                    except McpdTimeoutError:
                        yield _cot("mcpd_dispatch", "failed", body="Timeout")
                        duration = (time.monotonic() - t0) * 1000
                        self._audit.write_fields(AuditFields(
                            session_id=session.session_id, turn_index=session.turn_index,
                            intent_id=intent_id, action=intent["action"],
                            target=intent["target"], tier=int(cls_result.tier),
                            reason=intent["reason"], risk_level=intent["risk_level"],
                            outcome=Outcome.TOOL_TIMEOUT, duration_ms=duration,
                            backend=session.backend, model=self._cfg.qb.model,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                            cost_estimate_usd=total_cost,
                        ))
                        yield ResultEvent(result=TurnResult(
                            success=False, output="mcpd timed out — operation did not complete.",
                            outcome=Outcome.TOOL_TIMEOUT, tier=int(cls_result.tier),
                            backend=session.backend, duration_ms=duration,
                            cost_usd=total_cost if total_cost > 0 else None,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        ))
                        return
                    except (McpdProcessError, JsonRpcError) as exc:
                        yield _cot("mcpd_dispatch", "failed", body=str(exc))
                        duration = (time.monotonic() - t0) * 1000
                        self._audit.write_fields(AuditFields(
                            session_id=session.session_id, turn_index=session.turn_index,
                            intent_id=intent_id, action=intent["action"],
                            target=intent["target"], tier=int(cls_result.tier),
                            reason=intent["reason"], risk_level=intent["risk_level"],
                            outcome=Outcome.TOOL_ERROR, duration_ms=duration,
                            backend=session.backend, model=self._cfg.qb.model,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                            cost_estimate_usd=total_cost,
                        ))
                        # v6.10 F-64: prefer the friendly, hint-carrying
                        # translation of common mcpd rejections over the
                        # raw anyhow string. Falls through to the raw
                        # form if no mapping matches, so a mcpd wording
                        # change never silently swallows a diagnostic.
                        _friendly = self._friendly_tool_error(exc)
                        _output = _friendly if _friendly else f"Tool error: {exc}"
                        yield ResultEvent(result=TurnResult(
                            success=False, output=_output,
                            outcome=Outcome.TOOL_ERROR, tier=int(cls_result.tier),
                            backend=session.backend, duration_ms=duration,
                            cost_usd=total_cost if total_cost > 0 else None,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        ))
                        return
                    yield _cot("mcpd_dispatch", "done", body="Execution complete",
                                requires_cow=tool_result.requires_cow_approval)

            # Step 10: COW approval
            if tool_result.requires_cow_approval:
                yield _progress("cow_approval")
                yield _cot("cow_approval", "active",
                            body="Destructive op — awaiting COW preview approval")
                cow_preview_text = ""
                if tool_result.cow_preview:
                    cow_preview_text = json.dumps(tool_result.cow_preview, indent=2)
                cow_prompt = HitlPrompt(
                    intent, cls_result, backend=session.backend,
                    cow_summary=cow_preview_text,
                    lockout_seconds=self._cfg.hitl.lockout_seconds,
                    timeout_seconds=self._cfg.hitl.timeout_seconds,
                    presenter=self._build_presenter(),
                )
                decision = cow_prompt.ask()
                if decision != Decision.APPROVED:
                    cow_extra = {
                        "decision_id": cow_prompt.decision_id,
                        "key_pressed_class": cow_prompt.key_pressed_class,
                        "latency_ms": cow_prompt.decision_latency_ms,
                    }
                    yield _cot("cow_approval", "failed",
                                body="COW preview denied",
                                decision=decision.name.lower())
                    yield ResultEvent(result=self._denied(
                        session, intent, cls_result, decision,
                        qb_response, t0, intent_id=intent_id,
                        extra_cost=pb_cost,
                        extra_tokens_in=pb_response.tokens_in,
                        extra_tokens_out=pb_response.tokens_out,
                        extra=cow_extra,
                    ))
                    return
                yield _cot("cow_approval", "done", body="COW approved")
            else:
                yield _cot("cow_approval", "done", body="Skipped (no COW required)",
                            skipped=True)

            # Step 11: QB summarisation (with optional streaming)
            yield _progress("qb_summarize")
            yield _cot("qb_summarize", "active",
                        body="QB summarizing tool output for user")
            raw_output = json.dumps(tool_result.result)
            truncated = "\n".join(
                raw_output.splitlines()[:self._cfg.session.max_tool_output_lines]
            )

            if self._cfg.session.stream_output:
                summarise_system = (
                    "Summarise the tool output in 1-3 plain sentences for the "
                    "user. Be concise and factual — confirm what happened, "
                    "not what could happen. "
                    "For fs.write results, name the file and the byte count; "
                    "if the executed content differs from what was expected, "
                    "say so. For fs.read, name the file and the first line "
                    "(≤ 80 chars, quoted). For fs.list, give the count of "
                    "entries and split into directories vs files. For read-only "
                    "system queries, quote the key figure. "
                    'Output only JSON: {"summary": "<text>"}'
                )
                user_msg = json.dumps(
                    {"tool_output": truncated, "action": intent.get("action"),
                     "target": intent.get("target")},
                    separators=(",", ":"),
                )
                accumulated = ""
                try:
                    for chunk, acc, is_final in self._qb.stream_complete(
                        system=summarise_system, user=user_msg,
                        schema=_SUMMARISE_SCHEMA, max_retries=1,
                    ):
                        accumulated = acc
                        if not is_final and chunk:
                            yield TokenEvent(
                                token=chunk, accumulated=acc, final=False,
                            )
                    try:
                        parsed = json.loads(accumulated)
                        summary = parsed.get("summary", accumulated[:200])
                    except (json.JSONDecodeError, AttributeError):
                        summary = accumulated[:200]
                    yield TokenEvent(token="", accumulated=summary, final=True)
                except Exception as exc:
                    # F-53 v6.65: surface streaming failure so users know a
                    # bare fallback got shown instead of a real summary.
                    _log_exception(self._system_logger,
                                   "controller.qb_summarize.stream", exc)
                    summary = f"[summary generation failed: {type(exc).__name__}] " + truncated[:200]
            else:
                summary = self._qb_summarise(truncated, intent)

            session.add_tool_result_summary(summary)
            yield _cot("qb_summarize", "done", body="Summary ready")

            # Step 12: Audit
            yield _progress("audit")
            yield _cot("audit", "active", body="Writing audit record (INV-8)")
            duration = (time.monotonic() - t0) * 1000
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.EXECUTED, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
            ))
            yield _cot("audit", "done", body="Audit recorded",
                        outcome="executed", intent_id=intent_id)

            session.touch()

            yield ResultEvent(result=TurnResult(
                success=True, output=summary, outcome=Outcome.EXECUTED,
                tier=int(cls_result.tier), backend=session.backend,
                duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            ))

        except GeneratorExit:
            duration = (time.monotonic() - t0) * 1000
            try:
                # v6.10 F-67 (2026-07-18): every Outcome.CANCELLED audit
                # row MUST carry a cancel_reason so operators diagnosing
                # "why did this turn silently disappear?" get a clear
                # answer instead of a generic "cancelled". Today the only
                # emission path is GeneratorExit — a client disconnected
                # mid-stream (common in test runners that rapid-fire
                # close(); also fires when a user Ctrl+C's the AI Terminal
                # or the socket drops). Future paths (cost limit, rate
                # limit, admin abort) must populate their own reason
                # string via _audit_cancel() below — anti-hide guard in
                # test_cancelled_reason.py pins the discipline.
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id="", action="", target="", tier=0,
                    reason="cancelled", risk_level="unknown",
                    outcome=Outcome.CANCELLED, duration_ms=duration,
                    backend=session.backend,
                    model=getattr(self._cfg.qb, "model", ""),
                    tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
                    extra={
                        "cancelled_at_step": current_step,
                        "cancel_reason": "client_disconnect_during_streaming",
                    },
                ))
            except Exception as audit_exc:
                # F-53 v6.65: audit failure during cancellation isn't user-
                # visible but IS security-relevant (missed audit row = INV-8
                # regression). Surface it to SystemLogger so operators tuning
                # audit-log permissions see failures they'd otherwise miss.
                _log_exception(self._system_logger,
                               "controller.cancel_audit_write", audit_exc)
            return
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            _log_exception(self._system_logger,
                           "controller.run_turn_streaming", exc)
            try:
                self._audit.write_fields(self._make_error_fields(session, str(exc), duration))
            except Exception as audit_exc:
                # F-53: audit failure is worth surfacing separately.
                _log_exception(self._system_logger,
                               "controller.audit_write_streaming", audit_exc)
            yield ErrorEvent(
                error_type=type(exc).__name__,
                message=str(exc),
                cancelled_at_step=current_step,
            )

    def _run_turn_inner(self, user_input: str, session: Any, t0: float) -> TurnResult:
        qb_system = self._prompts.get(f"qb_{session.backend}")
        qb_cost = 0.0
        qb_tokens_in = qb_tokens_out = 0

        # ── Step 1: QB → Intent Object ────────────────────────────────────────
        session.add_user_message(user_input)
        # V6B Stage 2: prepend <context> block (see run_turn_streaming twin).
        qb_input = _build_qb_input(session, user_input)
        qb_response = self._qb.complete(
            system=qb_system,
            user=qb_input,
            schema=self._backend_intent_schema,
            max_retries=self._cfg.run.qb_max_retries,
        )
        raw_intent = qb_response.content_json
        # F-47b: same server-owned-field override the streaming path uses.
        # Without this the non-streaming RPC path lets Gemini's placeholder
        # UUIDs / non-conformant schema_version leak into validation.
        _normalize_server_owned_fields(raw_intent)
        session.add_assistant_message(json.dumps(raw_intent))
        qb_cost += qb_response.cost_usd or 0.0
        qb_tokens_in += qb_response.tokens_in
        qb_tokens_out += qb_response.tokens_out

        # ── Step 2: Schema validation ─────────────────────────────────────────
        try:
            validated = validate(raw_intent)
        except IntentValidationError as exc:
            return self._schema_rejected(session, str(exc), qb_response, t0)

        intent = validated.intent

        # ── Step 2b: Resolve target realpath (TOCTOU mitigation, SF-10) ──────
        raw_target = intent.get("target", "")
        if raw_target:
            intent["target_realpath"] = os.path.realpath(raw_target)
        else:
            intent["target_realpath"] = ""

        # F-32: short-circuit ambiguous targets before spending PB tokens.
        if intent["action"] in _PATH_REQUIRING_ACTIONS and raw_target in ("", "/"):
            friendly = (
                "Query too ambiguous to route safely. Try naming a specific "
                "directory (e.g. 'here', 'my Documents folder', '/tmp'). "
                "Received target: "
                + (repr(raw_target) if raw_target else "(empty)")
            )
            return self._schema_rejected(session, friendly, qb_response, t0)

        # F-35: catalogue landing pad — see run_turn_streaming twin comment.
        # Non-streaming path returns a TurnResult directly (no CoT events).
        if (intent["action"] == "system.unsupported"
                or intent["action"] not in _SUPPORTED_ACTIONS):
            return self._unsupported_result(
                session, intent, user_input, qb_response,
                qb_cost, qb_tokens_in, qb_tokens_out, t0,
            )

        # ── Step 3: Risk classification ───────────────────────────────────────
        cls_result = classify(intent)

        # ── Step 3b: Tier-2 review (escalate-only) ───────────────────────────
        original_tier = cls_result.tier
        if self._tier2 and cls_result.tier == Tier.MEDIUM:
            t2_decision = self._tier2.review(intent, cls_result)
            if t2_decision.escalate:
                cls_result = ClassificationResult(
                    tier=Tier.HIGH, reason=t2_decision.reason,
                    reversible=cls_result.reversible,
                )
            assert cls_result.tier >= original_tier, "Tier-2 review must never downgrade"

        # ── Step 3c: Trust consult (skip HITL if trusted) ────────────────────
        trust_ttl = self._cfg.hitl.trust_ttl_seconds
        if self._trust_store and trust_ttl > 0 and cls_result.requires_hitl:
            grant = self._trust_store.is_trusted(
                intent["action"], intent["target"],
                cls_result.tier, session.session_id,
            )
            if grant:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id="", action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.TRUST_APPLIED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
                    cost_estimate_usd=qb_cost,
                    extra={"grant_id": grant.grant_id},
                ))
                remaining = max(0, int(grant.expires_at - time.monotonic()))
                print(
                    f"\n  ✓ Auto-approved via trust grant "
                    f"{grant.action} → {grant.target_prefix} "
                    f"(expires in {remaining}s, /trust revoke {grant.grant_id})\n",
                    flush=True,
                )
                # Skip HITL, fall through to Step 5
                cls_result_requires_hitl = False
            else:
                cls_result_requires_hitl = True
        else:
            cls_result_requires_hitl = cls_result.requires_hitl

        # ── Step 4: HITL gate (Tier 3 only) ──────────────────────────────────
        if cls_result_requires_hitl:
            modify_count = 0
            while True:
                hitl_prompt = HitlPrompt(
                    intent, cls_result,
                    backend=session.backend,
                    lockout_seconds=self._cfg.hitl.lockout_seconds,
                    timeout_seconds=self._cfg.hitl.timeout_seconds,
                    presenter=self._build_presenter(),
                )
                decision = hitl_prompt.ask()

                hitl_extra = {
                    "decision_id": hitl_prompt.decision_id,
                    "key_pressed_class": hitl_prompt.key_pressed_class,
                    "latency_ms": hitl_prompt.decision_latency_ms,
                }

                if decision == Decision.EXPLAIN:
                    explanation = self._qb_explain(intent, cls_result)
                    print(f"\n  {explanation}\n", flush=True)
                    continue

                if decision == Decision.MODIFY:
                    modify_count += 1
                    if modify_count >= _MAX_MODIFY_CYCLES:
                        print("\n  Modify limit reached — operation denied.", flush=True)
                        return self._denied(
                            session, intent, cls_result, Decision.DENIED,
                            qb_response, t0, extra=hitl_extra,
                        )
                    print("\n  [Modify] — not yet wired to intent revision (M5.1d)", flush=True)
                    continue

                if decision == Decision.TRUST:
                    if trust_ttl <= 0:
                        print("\n  Trust is disabled in config (trust_ttl_seconds = 0).\n", flush=True)
                        continue
                    if cls_result.tier >= Tier.HIGH:
                        print("\n  Cannot trust Tier 3+ operations.\n", flush=True)
                        continue
                    grant = self._trust_store.grant(
                        action=intent["action"],
                        target_prefix=intent["target"],
                        max_tier=cls_result.tier,
                        session_id=session.session_id,
                        ttl_seconds=trust_ttl,
                    )
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id,
                        turn_index=session.turn_index,
                        intent_id="", action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"],
                        risk_level=intent["risk_level"],
                        outcome=Outcome.TRUST_GRANTED, duration_ms=0,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=0, tokens_out=0,
                        cost_estimate_usd=0.0,
                        extra={
                            "grant_id": grant.grant_id,
                            "ttl_seconds": trust_ttl,
                            **hitl_extra,
                        },
                    ))
                    print(
                        f"\n  ✓ Trust granted for {grant.action} "
                        f"→ {grant.target_prefix} ({trust_ttl}s)\n",
                        flush=True,
                    )
                    break  # treat as approved

                if decision != Decision.APPROVED:
                    return self._denied(
                        session, intent, cls_result, decision,
                        qb_response, t0, extra=hitl_extra,
                    )
                break

        # ── Step 5: Store intent → opaque UUID ───────────────────────────────
        intent_id = self._store.put(intent)

        # ── Step 6: PB → tool call ────────────────────────────────────────────
        tool_schema = self._get_tool_schema(intent["action"])
        # F-27 + F-41: pass validated intent target so PB doesn't hallucinate;
        # forward QB's content + pb_hint when present so PB uses verbatim bytes.
        pb_user = session.build_pb_user_turn(
            intent_id, intent["action"], tool_schema,
            target=intent.get("target", ""),
            content=intent.get("content", ""),
            pb_hint=intent.get("pb_hint", ""),
        )
        pb_system = self._prompts.get("pb")
        try:
            pb_response = self._pb.complete(
                system=pb_system,
                user=pb_user,
                schema=None,
                max_retries=1,
            )
            tool_call = pb_response.content_json
        except BrainSchemaError as exc:
            raw = exc.last_payload_excerpt.strip()
            if raw.upper().startswith("REFUSE:"):
                reason = raw[len("REFUSE:"):].strip()
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id,
                    turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"],
                    risk_level=intent["risk_level"],
                    outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=0,
                    cost_estimate_usd=qb_cost,
                    extra={"pb_refused": True,
                           "refusal_reason": reason},
                ))
                return TurnResult(
                    success=False,
                    output=f"Privileged Brain refused: {reason}",
                    outcome=Outcome.PB_SCHEMA_ERROR,
                    tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=qb_cost if qb_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=0,
                )
            raise
        pb_cost = pb_response.cost_usd or 0.0
        total_cost = qb_cost + pb_cost
        total_tokens_in = qb_tokens_in + pb_response.tokens_in
        total_tokens_out = qb_tokens_out + pb_response.tokens_out

        # ── Step 7: Post-validate tool call ───────────────────────────────────
        try:
            self._validate_tool_call(tool_call, intent["action"])
        except ValueError as exc:
            duration = (time.monotonic() - t0) * 1000
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.PB_SCHEMA_ERROR, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
            ))
            return TurnResult(
                success=False, output=f"PB output validation failed: {exc}",
                outcome=Outcome.PB_SCHEMA_ERROR, tier=int(cls_result.tier),
                backend=session.backend, duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            )

        # ── Step 8: QB verifier round-trip ────────────────────────────────────
        verifier_system = self._prompts.get("qb_verifier")
        # v6.8 M7.1 (2026-07-13): honour tier_floor + skip_tier_01 here too
        # (this branch was missing the gate that the streaming branch had —
        # a real coverage hole, non-streaming clients were paying the vote
        # latency on Tier 0/1 intents).
        _vcfg_ns = getattr(self._cfg, "verifier", None)
        if _vcfg_ns is not None and should_skip_verifier(_vcfg_ns, int(cls_result.tier)):
            from .verifier import VerifierResult
            vresult = VerifierResult(
                verified=True,
                reason="skipped (tier < tier_floor)",
                votes_cast=0,
                verified_count=0,
            )
        else:
            vresult = self._verifier.verify(intent, tool_call, self._qb, verifier_system)
        if not vresult.verified:
            duration = (time.monotonic() - t0) * 1000
            extra = {}
            if vresult.votes_cast > 1:
                extra["verifier_votes"] = vresult.votes_cast
                extra["verified_count"] = vresult.verified_count
            self._audit.write_fields(AuditFields(
                session_id=session.session_id, turn_index=session.turn_index,
                intent_id=intent_id, action=intent["action"],
                target=intent["target"], tier=int(cls_result.tier),
                reason=intent["reason"], risk_level=intent["risk_level"],
                outcome=Outcome.QB_VERIFIER_REJECTED, duration_ms=duration,
                backend=session.backend, model=self._cfg.qb.model,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                cost_estimate_usd=total_cost,
                extra=extra if extra else None,
            ))
            return TurnResult(
                success=False,
                output=f"Verifier rejected: {vresult.reason}",
                outcome=Outcome.QB_VERIFIER_REJECTED, tier=int(cls_result.tier),
                backend=session.backend, duration_ms=duration,
                cost_usd=total_cost if total_cost > 0 else None,
                tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            )

        # ── Step 9: Dispatch via McpdClient or GUI Agent ───────────────────
        tool_name = tool_call.get("tool", "")
        is_gui = tool_name.startswith("gui.")

        if is_gui:
            gui_cfg = getattr(self._cfg, "gui", None)
            if gui_cfg and not gui_cfg.enabled:
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.GUI_DENIED, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra={"execution_tier": "gui"},
                ))
                return TurnResult(
                    success=False,
                    output="GUI automation is disabled. Enable with [gui] enabled = true.",
                    outcome=Outcome.GUI_DENIED, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )
            try:
                from gui_agent.agent import GuiAgent
                # v6.10 P6 (F-73): full-config path — non-streaming call
                # site mirror of the streaming path above.
                gui = GuiAgent(config=gui_cfg) if gui_cfg else GuiAgent()
                gui_result = gui.handle_request(
                    tool_name, tool_call.get("params", {}),
                )
            except Exception as exc:  # noqa: BLE001
                # F-53 Scope A.P3: GUI Agent dispatch failure — recorded
                # to audit (Outcome.GUI_ERROR) with duration, tokens,
                # and the raw exception surfaces via the caller path.
                duration = (time.monotonic() - t0) * 1000
                self._audit.write_fields(AuditFields(
                    session_id=session.session_id, turn_index=session.turn_index,
                    intent_id=intent_id, action=intent["action"],
                    target=intent["target"], tier=int(cls_result.tier),
                    reason=intent["reason"], risk_level=intent["risk_level"],
                    outcome=Outcome.GUI_ERROR, duration_ms=duration,
                    backend=session.backend, model=self._cfg.qb.model,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    cost_estimate_usd=total_cost,
                    extra={"execution_tier": "gui", "gui_error": str(exc)},
                ))
                return TurnResult(
                    success=False, output=f"GUI error: {exc}",
                    outcome=Outcome.GUI_ERROR, tier=int(cls_result.tier),
                    backend=session.backend, duration_ms=duration,
                    cost_usd=total_cost if total_cost > 0 else None,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                )
            tool_result = ToolResult(result=gui_result, request_id=0)
        else:
            # v6.9 Scope O Layer 2 Part A: manifest-first dispatch. If
            # the tool is registered via a controller-side manifest
            # (nav.cd today), route there and skip mcpd. Falls through
            # cleanly for any tool the registry doesn't know.
            _manifest_result = self._try_manifest_dispatch(tool_call, session)
            if _manifest_result is not None:
                tool_result = _manifest_result
            else:
                try:
                    tool_result = self._mcpd.call(
                        tool_call["tool"],
                        tool_call.get("params"),
                        timeout=self._cfg.run.mcpd_timeout_seconds,
                    )
                except McpdTimeoutError:
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.TOOL_TIMEOUT, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                    ))
                    return TurnResult(
                        success=False, output="mcpd timed out — operation did not complete.",
                        outcome=Outcome.TOOL_TIMEOUT, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    )
                except (McpdProcessError, JsonRpcError) as exc:
                    duration = (time.monotonic() - t0) * 1000
                    self._audit.write_fields(AuditFields(
                        session_id=session.session_id, turn_index=session.turn_index,
                        intent_id=intent_id, action=intent["action"],
                        target=intent["target"], tier=int(cls_result.tier),
                        reason=intent["reason"], risk_level=intent["risk_level"],
                        outcome=Outcome.TOOL_ERROR, duration_ms=duration,
                        backend=session.backend, model=self._cfg.qb.model,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                        cost_estimate_usd=total_cost,
                    ))
                    # v6.10 F-64: friendly translation of common mcpd
                    # rejections. Falls through to raw form if unmapped.
                    _friendly = self._friendly_tool_error(exc)
                    _output = _friendly if _friendly else f"Tool error: {exc}"
                    return TurnResult(
                        success=False, output=_output,
                        outcome=Outcome.TOOL_ERROR, tier=int(cls_result.tier),
                        backend=session.backend, duration_ms=duration,
                        cost_usd=total_cost if total_cost > 0 else None,
                        tokens_in=total_tokens_in, tokens_out=total_tokens_out,
                    )

        # ── Step 10: COW approval (if mcpd requires it) ───────────────────────
        if tool_result.requires_cow_approval:
            cow_preview_text = ""
            if tool_result.cow_preview:
                cow_preview_text = json.dumps(tool_result.cow_preview, indent=2)
            cow_prompt = HitlPrompt(
                intent, cls_result,
                backend=session.backend,
                cow_summary=cow_preview_text,
                lockout_seconds=self._cfg.hitl.lockout_seconds,
                timeout_seconds=self._cfg.hitl.timeout_seconds,
                presenter=self._build_presenter(),
            )
            decision = cow_prompt.ask()
            if decision != Decision.APPROVED:
                cow_extra = {
                    "decision_id": cow_prompt.decision_id,
                    "key_pressed_class": cow_prompt.key_pressed_class,
                    "latency_ms": cow_prompt.decision_latency_ms,
                }
                return self._denied(session, intent, cls_result, decision,
                                    qb_response, t0, intent_id=intent_id,
                                    extra_cost=pb_cost,
                                    extra_tokens_in=pb_response.tokens_in,
                                    extra_tokens_out=pb_response.tokens_out,
                                    extra=cow_extra)

        # ── Step 11: QB summarisation (INV-2-extended) ───────────────────────
        raw_output = json.dumps(tool_result.result)
        truncated = "\n".join(raw_output.splitlines()[:self._cfg.session.max_tool_output_lines])
        summary = self._qb_summarise(truncated, intent)
        session.add_tool_result_summary(summary)

        # ── Step 12: Audit ────────────────────────────────────────────────────
        duration = (time.monotonic() - t0) * 1000
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent_id, action=intent["action"],
            target=intent["target"], tier=int(cls_result.tier),
            reason=intent["reason"], risk_level=intent["risk_level"],
            outcome=Outcome.EXECUTED, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
            cost_estimate_usd=total_cost,
        ))

        # ── Step 13: touch session ────────────────────────────────────────────
        session.touch()

        return TurnResult(
            success=True, output=summary, outcome=Outcome.EXECUTED,
            tier=int(cls_result.tier), backend=session.backend,
            duration_ms=duration,
            cost_usd=total_cost if total_cost > 0 else None,
            tokens_in=total_tokens_in, tokens_out=total_tokens_out,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _friendly_tool_error(self, exc: Exception) -> str:
        """v6.10 F-64 (2026-07-18): translate mcpd's raw anyhow-flavored
        tool errors into user-facing messages that name the cause + a
        remediation hint.

        Pre-Track-N users saw output like
            ``Tool error: Internal error: '/etc/os-release' is not under any whitelisted root``
        and had no idea whether the daemon was broken, the path was
        wrong, or the sandbox was denying legitimately. This helper
        pattern-matches the stable substrings mcpd emits and replaces
        the prefix with a friendly explanation.

        The mapping is intentionally conservative — anything not
        matched falls through to the raw ``Tool error: {exc}`` shape
        the caller already emits, so a mcpd wording change never
        silently swallows a diagnostic. Anti-hide test in
        test_friendly_tool_error.py pins the mapping table."""
        text = str(exc)

        # Path-outside-root — most common F-64 case (fs.list/read/stat
        # on /etc, /var, /usr, etc.). anyhow emits:
        #   "'{}' is not under any whitelisted root"
        # sometimes wrapped by JSON-RPC as "Internal error: ..." prefix.
        if "is not under any whitelisted root" in text:
            # Extract the offending path — the first quoted string is
            # the user-supplied path.
            import re
            m = re.search(r"'([^']+)' is not under any whitelisted root", text)
            offender = m.group(1) if m else "(unknown path)"
            return (
                f"Cannot access '{offender}' — the daemon's filesystem "
                "sandbox restricts reads to configured roots (default: "
                "your home directory). Ask an admin to widen "
                "[mcpd.fs] read_roots in /etc/icebreaker/controller.toml "
                "if you need broader access. Widening this weakens "
                "INV-4 sandbox scoping, so use sparingly."
            )

        # Absolute-path requirement — user asked for a relative path.
        if "path must be absolute" in text:
            return (
                "Please use an absolute path (starting with '/'). "
                "Relative paths cannot be resolved reliably from the "
                "daemon's context."
            )

        # Percent-encoded / backslash chars — mcpd rejects these to
        # thwart path-traversal attempts.
        if "percent-encoded characters not allowed" in text:
            return (
                "Path contains percent-encoded characters (like '%20') "
                "which the daemon's sandbox rejects for path-traversal "
                "safety. Rewrite the request without URL encoding."
            )
        if "backslash characters not allowed" in text:
            return (
                "Path contains backslash characters which the daemon's "
                "sandbox rejects for path-traversal safety. Use forward "
                "slashes only."
            )

        # File-size / directory guardrails.
        if "file too large" in text:
            return (
                "That file is too large for a single read. The daemon "
                "caps reads at ~1 MB to prevent memory blowups; use "
                "'read the first 100 lines of ...' or split the file."
            )
        if "path is a directory; use fs.list instead" in text:
            return (
                "That path is a directory. Try 'list files in <path>' "
                "instead of 'read <path>' — the daemon uses fs.list for "
                "directories and fs.read for files."
            )

        # UTF-8 failure on binary reads — shipped as Phase 5 limitation.
        if "not valid UTF-8" in text:
            return (
                "That file isn't UTF-8 text (binary content). The current "
                "sandbox only handles text; binary reads land in Phase 5."
            )

        # Unmatched — return None so caller falls back to raw form.
        return ""

    def _try_manifest_dispatch(self, tool_call: dict, session: Any) -> Any:
        """v6.9 Scope O Layer 2 Part A — dispatch to a controller-side
        manifest if one is registered for tool_call["tool"]; else return
        None so the caller falls through to mcpd.

        Returns a ToolResult-shaped object so downstream code
        (raw_output serialization, requires_cow_approval checks, audit
        write) is agnostic to whether the tool was manifest-served or
        mcpd-served.

        Raises the impl's exception verbatim on failure — the caller's
        existing McpdProcessError/JsonRpcError handler catches and
        surfaces it consistently with mcpd-side failures.
        """
        tool_name = tool_call.get("tool", "")
        if not tool_name or not self._manifests.has(tool_name):
            return None
        from .impl_kinds import DispatchContext
        from .mcpd_client import ToolResult

        ctx = DispatchContext(session=session, audit_log=self._audit)
        impl_result = self._manifests.dispatch(
            tool_name, tool_call.get("params") or {}, ctx,
        )
        # Wrap the ImplResult so downstream code sees a mcpd-shaped
        # result. `stdout` becomes $STEP_N_STDOUT for plan mode; extra
        # metadata rides in the same dict for audit visibility.
        payload = {"stdout": impl_result.stdout, "ok": True}
        payload.update(impl_result.metadata)
        return ToolResult(result=payload, request_id=0)

    def _qb_summarise(self, raw_output: str, intent: dict) -> str:
        summarise_system = (
            "Summarise the following tool output in 1-3 plain sentences for the user. "
            "Be concise and factual. Output only JSON: {\"summary\": \"<text>\"}"
        )
        user_msg = json.dumps(
            {"tool_output": raw_output, "action": intent.get("action"), "target": intent.get("target")},
            separators=(",", ":"),
        )
        try:
            resp = self._qb.complete(
                system=summarise_system,
                user=user_msg,
                schema=_SUMMARISE_SCHEMA,
                max_retries=1,
            )
            return resp.content_json.get("summary", raw_output[:200])
        except Exception as exc:
            # F-53 v6.65: previously swallowed silently — the user would see
            # raw tool bytes instead of a summary and have no idea the QB
            # summarize call had failed. This is exactly the class of bug
            # v6.65's exception surfacing was meant to kill. Log the swallow
            # for the audit trail; prefix the raw output so the user sees
            # WHY they're looking at unformatted bytes.
            _log_exception(self._system_logger, "controller.qb_summarize", exc)
            return f"[summary generation failed: {type(exc).__name__}] " + raw_output[:200]

    def _qb_explain(self, intent: dict, cls_result: Any) -> str:
        explain_system = (
            "Explain in 2-3 plain sentences what this system action will do and "
            "any risks. Be concise and factual. Output only JSON: "
            '{"explanation": "<text>"}'
        )
        user_msg = json.dumps({
            "action": intent.get("action"),
            "target": intent.get("target"),
            "risk_level": intent.get("risk_level"),
            "tier": int(cls_result.tier),
            "reversible": cls_result.reversible,
        }, separators=(",", ":"))
        try:
            resp = self._qb.complete(
                system=explain_system,
                user=user_msg,
                schema=_EXPLAIN_SCHEMA,
                max_retries=1,
            )
            return resp.content_json.get("explanation", "No explanation available.")
        except Exception as exc:
            # F-53 v6.65: same reasoning as _qb_summarise above. Surface the
            # failure type so users hitting the HITL "explain" button see
            # what went wrong instead of a generic apology.
            _log_exception(self._system_logger, "controller.qb_explain", exc)
            return f"Could not generate explanation ({type(exc).__name__})."

    def _validate_tool_call(self, tool_call: dict, expected_action: str) -> None:
        if not isinstance(tool_call, dict):
            raise ValueError("PB output is not a JSON object")
        tool = tool_call.get("tool")
        if tool != expected_action:
            raise ValueError(
                f"PB returned tool {tool!r} but intent action is {expected_action!r}"
            )
        params = tool_call.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("PB params must be a JSON object or absent")
        if params is not None:
            tool_schema = self._get_tool_schema(expected_action)
            if tool_schema.get("properties"):
                try:
                    jsonschema.validate(params, tool_schema)
                except jsonschema.ValidationError as exc:
                    path = " -> ".join(
                        str(p) for p in exc.absolute_path
                    ) or "root"
                    raise ValueError(
                        f"PB params failed schema validation at "
                        f"{path}: {exc.message}"
                    ) from None

    def _get_tool_schema(self, action: str) -> dict:
        if self._schemas_dir:
            schema_path = Path(self._schemas_dir) / f"{action}.json"
            if schema_path.exists():
                try:
                    return json.loads(schema_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        return {"type": "object"}

    def _resolve_schemas_dir(self) -> str:
        if self._cfg.run.mcpd_schemas_dir:
            return str(Path(self._cfg.run.mcpd_schemas_dir).expanduser())
        # Auto-detect: mcpd_binary is src/mcpd/target/release/mcpd → src/mcpd/schemas/
        binary = Path(self._cfg.run.mcpd_binary)
        candidate = binary.parent.parent.parent / "schemas"
        if candidate.exists():
            return str(candidate)
        distro_candidate = Path("/usr/share/icebreaker/schemas")
        if distro_candidate.exists():
            return str(distro_candidate)
        return ""

    def _build_presenter(self) -> HitlPresenter:
        if self._presenter_factory is not None:
            return self._presenter_factory()
        keymap = getattr(self._cfg, "keymap", None)
        presenter_name = getattr(self._cfg.hitl, "presenter", "terminal")
        try:
            return make_presenter(presenter_name, keymap=keymap)
        except ValueError:
            return TerminalPresenter(keymap=keymap)

    def _execute_rpa_workflow(
        self,
        workflow_name: str,
        keywords: list[dict],
        timeout_seconds: int,
        session: Any,
        intent: dict,
        t0: float,
    ) -> dict:
        """Spawn RPA Bridge subprocess, stream progress, and QB-monitor each step.

        Reads stdout line-by-line to capture ``rpa.keyword_progress``
        notifications as they arrive. After each keyword, builds a text
        state summary and sends it to QB for on-track/off-track assessment.
        If QB flags off-track, kills the subprocess and returns partial
        results with ``qb_paused_at_keyword``.
        """
        import json as _json
        import select as _select
        import subprocess
        import sys
        import threading

        from .turn_events import RpaEvent

        rpa_params: dict = {
            "workflow_name": workflow_name,
            "keywords": keywords,
        }
        rpa_cfg = getattr(self._cfg, "rpa", None)
        if rpa_cfg is not None:
            # PR #34 M2 (F-69 chain): read fields directly instead of
            # getattr(..., "<hard-coded-default>"). The hard-coded
            # fallback string was a silent F-69-shape drift risk — if
            # RpaConfig ever changed screenshot_policy's default without
            # updating this line, the fallback would ship stale under
            # `getattr` while `rpa_cfg.screenshot_policy` would honor
            # the dataclass default. The generalized
            # test_rpa_builder_all_defaults_match_dataclass guard now
            # keeps the dataclass ↔ _build_rpa_config in lockstep;
            # direct access here removes the third silent drift point.
            if rpa_cfg.screenshot_policy != "all":
                rpa_params["screenshot_policy"] = rpa_cfg.screenshot_policy
            if rpa_cfg.auto_wait_seconds > 0:
                rpa_params["auto_wait_seconds"] = float(rpa_cfg.auto_wait_seconds)

        rpa_request = {
            "jsonrpc": "2.0",
            "method": "rpa.execute_workflow",
            "id": 1,
            "params": rpa_params,
        }

        effective_timeout = timeout_seconds + 5

        _EMPTY_RESULT: dict = {
            "success": False,
            "timed_out": False,
            "keywords_executed": 0,
            "keywords_total": len(keywords),
            "elapsed_ms": 0,
            "keyword_results": [],
            "error": "",
            "screenshot_hashes": [],
        }

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "rpa_bridge"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._scrubbed_rpa_env(),
            )
        except (FileNotFoundError, OSError) as exc:
            return {**_EMPTY_RESULT, "error": f"Failed to start RPA Bridge: {exc}"}

        # Send request and close stdin so the Bridge knows input is complete
        try:
            proc.stdin.write((_json.dumps(rpa_request) + "\n").encode())
            proc.stdin.flush()
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            proc.wait()
            return {**_EMPTY_RESULT, "error": f"Failed to write to RPA Bridge: {exc}"}

        # SIGKILL watchdog in a background thread (defense-in-depth)
        killed_by_watchdog = threading.Event()

        def _watchdog() -> None:
            if not killed_by_watchdog.wait(effective_timeout):
                try:
                    proc.kill()
                except OSError:
                    pass
                killed_by_watchdog.set()

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        rpa_result: dict = dict(_EMPTY_RESULT)
        screenshot_hashes: list[str] = []
        keyword_results: list[dict] = []
        qb_paused_at: int | None = None

        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue

                # JSON-RPC notification: per-keyword progress
                if msg.get("method") == "rpa.keyword_progress":
                    p = msg.get("params", {})
                    kw_name = p.get("keyword_name", "")
                    kw_idx = p.get("keyword_index", 0)
                    kw_total = p.get("keyword_total", len(keywords))
                    kw_status = p.get("status", "")
                    elapsed = p.get("elapsed_ms", 0)
                    remaining = p.get("timeout_remaining_ms", 0)
                    sh = p.get("screenshot_hash", "")
                    if sh:
                        screenshot_hashes.append(sh)

                    keyword_results.append({
                        "index": kw_idx, "name": kw_name,
                        "status": kw_status, "elapsed_ms": elapsed,
                        "screenshot_hash": sh,
                    })

                    # QB progress monitor
                    on_track, concern = self._check_rpa_progress(
                        session, kw_name, kw_status, kw_idx, kw_total, intent,
                    )

                    # Emit step event (consumed by daemon→client→companion)
                    self._rpa_step_events.append(RpaEvent(
                        phase="step",
                        workflow_name=workflow_name,
                        keyword_index=kw_idx + 1,
                        keyword_total=kw_total,
                        current_keyword=kw_name,
                        keyword_status=kw_status,
                        timeout_remaining_ms=remaining,
                        screenshot_hash=sh,
                        qb_on_track=on_track,
                        qb_concern=concern,
                        timestamp_ms=(time.monotonic() - t0) * 1000,
                    ))

                    if not on_track:
                        qb_paused_at = kw_idx
                        self._rpa_step_events.append(RpaEvent(
                            phase="paused",
                            workflow_name=workflow_name,
                            keyword_index=kw_idx + 1,
                            keyword_total=kw_total,
                            current_keyword=kw_name,
                            qb_on_track=False,
                            qb_concern=concern,
                            timestamp_ms=(time.monotonic() - t0) * 1000,
                        ))
                        proc.kill()
                        break

                # JSON-RPC response: final result
                elif "result" in msg:
                    rpa_result.update(msg["result"])
                elif "error" in msg:
                    err = msg["error"]
                    rpa_result["error"] = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    rpa_result["success"] = False
        except Exception as exc:
            # F-53 v6.65: RPA stream parse failure means the RPA subprocess
            # sent garbage or the pipe closed unexpectedly. Log so operators
            # tuning RPA workflows see why an execution "silently succeeded"
            # with an incomplete rpa_result dict.
            _log_exception(self._system_logger, "controller.rpa_stream_parse", exc)

        # Wait for process to exit
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Cancel watchdog
        killed_by_watchdog.set()

        if killed_by_watchdog.is_set() and proc.returncode == -9:
            rpa_result["timed_out"] = True
            rpa_result["error"] = rpa_result.get("error") or "RPA Bridge process timed out (SIGKILL)"

        rpa_result["keyword_results"] = keyword_results
        rpa_result["screenshot_hashes"] = screenshot_hashes
        rpa_result["keywords_executed"] = len(keyword_results)
        if qb_paused_at is not None:
            rpa_result["qb_paused_at_keyword"] = qb_paused_at
            rpa_result["success"] = False
            rpa_result["error"] = rpa_result.get("error") or f"QB flagged off-track at keyword {qb_paused_at}"

        return rpa_result

    def _check_rpa_progress(
        self,
        session: Any,
        keyword_name: str,
        keyword_status: str,
        keyword_index: int,
        keyword_total: int,
        intent: dict,
    ) -> tuple[bool, str]:
        """Ask QB if RPA workflow is on track. Returns (on_track, concern).

        QB receives a sanitized text summary only (INV-1: no raw pixels).
        Fails safe: if QB is unreachable or returns garbage, assume on-track
        to avoid blocking the workflow on a monitoring failure.
        """
        state_summary = (
            f"Keyword {keyword_index + 1}/{keyword_total}: "
            f"{keyword_name} → {keyword_status}. "
            f"Original intent: {intent.get('action', '')} on {intent.get('target', '')}"
        )
        _PROGRESS_SCHEMA = {
            "type": "object",
            "properties": {
                "on_track": {"type": "boolean"},
                "concern": {"type": "string"},
            },
            "required": ["on_track"],
        }
        try:
            response = self._qb.complete(
                system=(
                    "You monitor RPA workflow progress. Given a keyword "
                    "execution summary, assess if the workflow is on track. "
                    'Output JSON: {"on_track": true/false, "concern": "text"}'
                ),
                user=state_summary,
                schema=_PROGRESS_SCHEMA,
                max_retries=1,
            )
            result = response.content_json
            return result.get("on_track", True), result.get("concern", "")
        except Exception as exc:
            # F-53 v6.65: on-track check failure defaults to on_track=True
            # (fail-open — better UX than blocking every RPA turn). But log
            # the exception so users see why the monitor is silently
            # passing everything.
            _log_exception(self._system_logger,
                           "controller.rpa_on_track_check", exc)
            return True, ""

    @staticmethod
    def _gui_action_to_rpa_keywords(tool_name: str, params: dict) -> list[dict]:
        """Translate a failed GUI action into RPA Bridge keywords.

        Maps gui.click/gui.type/gui.select to their Robot Framework
        equivalents. Uses the element name as a locator since AT-SPI
        identifiers often map to accessible names that Selenium/RPA can find.
        """
        window = params.get("window", "")
        role = params.get("role", "")
        name = params.get("name", "")

        # Build a CSS-like locator from the AT-SPI identity
        if name:
            locator = f"name={name}"
        elif role:
            locator = f"role={role}"
        else:
            locator = "xpath=//body"

        keywords: list[dict] = []

        # Navigate to window if specified
        if window:
            keywords.append({
                "name": "Wait Until Page Contains Element",
                "args": [locator, "10"],
            })

        if tool_name == "gui.click":
            keywords.append({"name": "Click Element", "args": [locator]})

        elif tool_name == "gui.type":
            text = params.get("text", "")
            keywords.append({"name": "Click Element", "args": [locator]})
            keywords.append({"name": "Input Text", "args": [locator, text]})

        elif tool_name == "gui.select":
            value = params.get("value", "")
            keywords.append({
                "name": "Select From List By Value",
                "args": [locator, value],
            })

        else:
            keywords.append({"name": "Click Element", "args": [locator]})

        return keywords

    @staticmethod
    def _scrubbed_rpa_env() -> dict[str, str]:
        """Build a minimal environment for the RPA Bridge subprocess (BP-8)."""
        safe = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "TERM", "TZ", "TMPDIR",
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
        }
        return {k: v for k, v in os.environ.items() if k in safe}

    def _denied(
        self, session: Any, intent: dict, cls_result: Any, decision: Decision,
        qb_response: Any, t0: float,
        intent_id: str = "",
        extra_cost: float = 0.0, extra_tokens_in: int = 0, extra_tokens_out: int = 0,
        extra: Optional[dict] = None,
    ) -> TurnResult:
        outcome = decision.to_outcome() or Outcome.HITL_DENIED
        duration = (time.monotonic() - t0) * 1000
        cost = (qb_response.cost_usd or 0.0) + extra_cost
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent_id, action=intent["action"],
            target=intent["target"], tier=int(cls_result.tier),
            reason=intent["reason"], risk_level=intent["risk_level"],
            outcome=outcome, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in + extra_tokens_in,
            tokens_out=qb_response.tokens_out + extra_tokens_out,
            cost_estimate_usd=cost,
            extra=extra,
        ))
        return TurnResult(
            success=False, output="Operation denied.",
            outcome=outcome, tier=int(cls_result.tier),
            backend=session.backend, duration_ms=duration,
            cost_usd=cost if cost > 0 else None,
            tokens_in=qb_response.tokens_in + extra_tokens_in,
            tokens_out=qb_response.tokens_out + extra_tokens_out,
        )

    def _schema_rejected(
        self, session: Any, error: str, qb_response: Any, t0: float,
    ) -> TurnResult:
        duration = (time.monotonic() - t0) * 1000
        cost = qb_response.cost_usd or 0.0
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id="", action="", target="", tier=0,
            reason="schema_rejected", risk_level="unknown",
            outcome=Outcome.SCHEMA_REJECTED, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in, tokens_out=qb_response.tokens_out,
            cost_estimate_usd=cost,
        ))
        return TurnResult(
            success=False, output=f"Could not parse intent: {error}",
            outcome=Outcome.SCHEMA_REJECTED, backend=session.backend,
            duration_ms=duration, cost_usd=cost if cost > 0 else None,
            tokens_in=qb_response.tokens_in, tokens_out=qb_response.tokens_out,
        )

    # ── F-35: system.unsupported short-circuit helpers ────────────────────────

    def _unsupported_payload(self, intent: dict, user_input: str) -> tuple[str, str, list]:
        """Extract UNSUPPORTED payload from an intent (rewriting if needed).

        If QB emitted `action=system.unsupported`, use its params verbatim.
        If QB emitted an unlisted action (`_SUPPORTED_ACTIONS` guard rewrite),
        synthesize a suggestion naming the phantom action so the user sees
        exactly what QB tried to do.

        Returns (requested_intent, suggestion, alternative_actions).
        """
        params = intent.get("params") or {}
        if intent.get("action") == "system.unsupported":
            requested = str(params.get("requested_intent") or user_input)[:512]
            suggestion = str(params.get("suggestion") or "")[:512]
            alt_raw = params.get("alternative_actions") or []
            alt = [str(a)[:32] for a in alt_raw if isinstance(a, str)][:5]
            return requested, suggestion, alt
        # Rewritten: QB emitted an unlisted action. Name it so the user sees why.
        phantom = intent.get("action", "<none>")
        requested = user_input[:512]
        suggestion = (
            f"Requested action '{phantom}' is not in the catalogue. "
            "The Quarantined Brain tried to substitute a tool that doesn't exist "
            "— refused before execution (F-35 lookalike guard)."
        )
        return requested, suggestion, []

    def _write_unsupported_audit(
        self, session: Any, intent: dict, requested: str, suggestion: str,
        qb_response: Any, duration: float,
    ) -> None:
        self._audit.write_fields(AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id=intent.get("intent_id", ""),
            action="system.unsupported",  # normalize even on rewrite
            target="",
            tier=0,
            reason=f"unsupported:{requested[:80]}",
            risk_level="low",
            outcome=Outcome.UNSUPPORTED, duration_ms=duration,
            backend=session.backend, model=self._cfg.qb.model,
            tokens_in=qb_response.tokens_in, tokens_out=qb_response.tokens_out,
            cost_estimate_usd=qb_response.cost_usd or 0.0,
            extra={"suggestion": suggestion[:256]},
        ))

    def _unsupported_result(
        self, session: Any, intent: dict, user_input: str, qb_response: Any,
        qb_cost: float, qb_tokens_in: int, qb_tokens_out: int, t0: float,
    ) -> TurnResult:
        """Non-streaming F-35 short-circuit."""
        requested, suggestion, _alt = self._unsupported_payload(intent, user_input)
        duration = (time.monotonic() - t0) * 1000
        friendly = suggestion or f"I can't do that: '{requested}'."
        self._write_unsupported_audit(session, intent, requested, suggestion, qb_response, duration)
        return TurnResult(
            success=False, output=friendly,
            outcome=Outcome.UNSUPPORTED, tier=0,
            backend=session.backend, duration_ms=duration,
            cost_usd=qb_cost if qb_cost > 0 else None,
            tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
        )

    def _emit_unsupported(
        self, session: Any, intent: dict, user_input: str, qb_response: Any,
        qb_cost: float, qb_tokens_in: int, qb_tokens_out: int, t0: float,
    ):
        """Streaming F-35 short-circuit — yields a friendly CoT card + ResultEvent."""
        # F-56 (2026-07-11): ResultEvent is imported at module top of
        # turn_events, which imports TurnResult from .main — a circular
        # dependency the codebase works around by deferring `.turn_events`
        # imports into each caller's local scope (see run_turn_streaming
        # line ~441). This method is invoked FROM run_turn_streaming's
        # scope but Python method scope does NOT inherit ResultEvent from
        # the caller's locals — every method needs its own local import.
        # G24 sweep on 2026-07-11 hit this on 2 broad-OS rows Gemini
        # routed to system.unsupported. Same shape as F-42 (`_cot`
        # closure vs method scope); same fix pattern.
        from .turn_events import ResultEvent
        requested, suggestion, alt = self._unsupported_payload(intent, user_input)
        # New CoT step: neither error nor success — informative UNSUPPORTED card.
        # 'done' state so TUI treats it as concluded; the outcome=UNSUPPORTED
        # is what drives yellow styling (Track A4).
        body = suggestion or f"I can't do that: '{requested}'."
        if alt:
            body += f"  (Adjacent supported actions: {', '.join(alt)})"
        # F-42: `_cot` is a nested closure inside run_turn_streaming — not in
        # scope here. Use _make_cot(t0, ...) instead.
        yield _make_cot(t0, "schema_validation", "done",
                        body="Intent accepted as UNSUPPORTED — no matching tool")
        # F-35 + F-48: yellow CoT card via styles.tcss keyed on step_NAME.
        # Terminal's CotCard.on_mount maps step_name → CSS class ("s-unsupported").
        # step_state must be one of {pending, active, done, failed} per
        # turn_events._COT_STEP_STATES; the F-42 fix used step_state="unsupported"
        # which crashed the whole turn. Use "done" for state (step is complete)
        # and preserve "unsupported" for name (drives yellow styling).
        yield _make_cot(t0, "unsupported", "done", body=body)
        duration = (time.monotonic() - t0) * 1000
        self._write_unsupported_audit(session, intent, requested, suggestion, qb_response, duration)
        yield ResultEvent(result=TurnResult(
            success=False, output=body,
            outcome=Outcome.UNSUPPORTED, tier=0,
            backend=session.backend, duration_ms=duration,
            cost_usd=qb_cost if qb_cost > 0 else None,
            tokens_in=qb_tokens_in, tokens_out=qb_tokens_out,
        ))

    def _make_error_fields(self, session: Any, error: str, duration: float) -> AuditFields:
        return AuditFields(
            session_id=session.session_id, turn_index=session.turn_index,
            intent_id="", action="", target="", tier=0,
            reason=f"internal_error:{error[:80]}", risk_level="unknown",
            outcome=Outcome.BRAIN_ERROR, duration_ms=duration,
            backend=session.backend, model=getattr(self._cfg.qb, "model", ""),
            tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
        )
