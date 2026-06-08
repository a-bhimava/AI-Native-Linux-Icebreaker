# Phase 2 Roadmap — Dual-Brain Controller

## §1 — Context & Status

> **Status: planning complete after two scope rounds (2026-06-05). Implementation starts at M2.0.**

Phase 1 (mcpd Rust daemon) merged to main. The 22-tool MCP surface, kernel sandbox (Landlock + Seccomp-BPF + COW gate), and append-only audit log are live and verified on Linux.

Phase 2 builds **the Central Controller** — the security keystone that mediates between the Quarantined Brain (perception, no exec) and the Privileged Brain (exec, no raw input). The whitepaper's prompt-injection defense is *this code*. If the Controller is wrong, every other layer collapses.

**Scope refinements from planning round 2 (also 2026-06-05):**

1. **Pluggable QB backend** — Local Phi-4-mini (whitepaper-faithful, GBNF-guaranteed) is the default. **Anthropic** (Claude) and **Google** (Gemini) are first-class additional backends selectable at runtime. OpenAI deferred. The `BrainBackend` interface is designed so OAuth-based consumer subscriptions (Claude.ai, ChatGPT Plus) can drop in during Phase 5 without breaking existing code.
2. **PB stays local** — fine-tuned Qwen 2.5 Coder 1.5B (`models/run7_cot_q4km.gguf`), GBNF-constrained. Non-negotiable: PB has tool access, so giving it network egress to an API provider defeats the on-device safety guarantee.
3. **Dev + verification on the GCP VM only** — skip M4 iteration. All Phase 2 code runs against the real Linux mcpd binary throughout.
4. **Multi-turn REPL is V1** — `python -m controller --repl` is a hard requirement. Single-shot CLI (`python -m controller "<one>"`) is the special case of a one-intent session that auto-exits.

**Whitepaper invariants the Controller must enforce:**
- **INV-1** — Quarantined Brain has zero MCP / zero exec. Privileged Brain has zero raw user input.
- **INV-2** — Controller rejects any field outside the Intent schema. Only opaque UUIDs flow to PB.
- **INV-2-pluggable (new this phase)** — when QB is an API/subscription backend, GBNF is not available. Mandatory `jsonschema` validation + retry-on-malformed (≤3 attempts) is the safety floor. The Controller treats schema-rejected output identically across backends; the difference is the depth of guarantee (mathematical vs probabilistic).
- **INV-2-extended (V1-blocking now, was Phase 5)** — mcpd output strings flow back to QB for summary only, never re-enter the PB context. Critical for multi-turn since turn N's tool output can otherwise poison turn N+1's PB context.
- **INV-4** — Every MCP tool param validated against JSON Schema before dispatch.
- **INV-8** — Audit log opened with O_APPEND, records every intent including rejected. Now also records `backend`, `session_id`, `turn_index`, `tokens_in/out`, `cost_estimate_usd` for cost + privacy accountability.

**What already exists in `dual-brain/controller/` (skeleton):**

| File | LOC | Status |
|---|---|---|
| `schemas/intent.json` | 59 | Complete — JSON Schema v7, 7 fields (intent_id, action, target, params, reason, risk_level + optional schema_version, timestamp), strict metachar-forbidding patterns, `additionalProperties: false` |
| `intent_store.py` | 132 | Complete — thread-safe UUID→Intent dict, TTL=300s, methods `put/get/delete/revise/evict_expired`. Module-level singleton |
| `risk_classifier.py` | 216 | **Drifted** — `_TIER0_TOOLS` lists tools (`system.cpu`, `package.list`, `network.interfaces`, `service.status`) that mcpd does not ship. Must be regenerated from mcpd in M2.0 |

**What does not exist yet:**
- `backends/` — abstract `BrainBackend` interface + 3 implementations (local llama.cpp, Anthropic, Google Gemini)
- `session/` — `SessionState`, conversation memory, INV-2-extended enforcement
- `config/` — `controller.toml` loader, backend selection, API key resolution
- The orchestration loop (`main.py`) covering both `--repl` and single-shot
- mcpd subprocess wrapper (`mcpd_client.py`)
- the QB grammar (`grammars/qb_intent.gbnf`) — for local QB backend only
- audit log writer (`audit.py`) — with session_id / backend / cost fields
- HITL terminal gate (`hitl.py`)
- system prompts for QB (per-backend variants) and PB
- start scripts for the PB llama-server (mandatory) and the local QB llama-server (optional, only if `qb.backend = "local"`)
- a downloaded local QB model (`models/Phi-4-mini-instruct-Q4_K_M.gguf`) — only if local backend chosen

**Platform constraint:** All Phase 2 dev and verification on the GCP VM (Debian 12, kernel 6.1). Python 3.10+ from the system; brains on llama-server (local backend) or HTTPS to provider APIs (Anthropic / Google). mcpd integration tests use the real Linux mcpd binary built in Phase 1.

**What is NOT in scope for Phase 2 (preserved untouched):**
- `shell/pb_*` — the V1 shell trigger that pipes natural language → bash via PB directly. Separate code path. Different audit log (`~/.pb_audit.jsonl`). Coexists.

---

## §2 — Exit Gates

Phase 2 ships only when all eleven gates are green.

| # | Gate | How it's verified |
|---|---|---|
| G1 | All Controller unit + integration tests pass | `pytest dual-brain/controller/tests/` exits 0 |
| G2 | Classifier ↔ mcpd catalogue parity | `python scripts/export_mcpd_catalogue.py --check` returns 0 (no drift) |
| G3 | Quarantined Brain has zero MCP attachment | Instrumented test: every QB backend's HTTP/SDK client has no `mcpd_client` reference; constructor rejects `mcpd_client=` kwarg |
| G4 | Privileged Brain receives only opaque UUIDs | Instrumented test wraps PB request body; asserts every user-supplied string is UUID format, never raw user text |
| G5 | Schema validator rejects all malformed intents | 100/100 hand-crafted corpus + 10 000 `hypothesis` iterations → 0 false accepts |
| G6 | 100+ adversarial prompt-injection payloads contained across **all backends** | 0/100+ cause unintended mcpd dispatch; HITL denials and schema rejections both count as "contained" |
| G7 | Audit log records every intent with full provenance | 100 mixed intents (accepted + rejected) → 100 lines in log; each line carries `intent_id`, `session_id`, `turn_index`, `backend`, `tokens_in/out`, `cost_estimate_usd`, `outcome` |
| G8 | HITL 3-second approve lockout enforced | Test reads stdin pre-3 s; approval rejected. Reads post-3 s → accepted. Timing measured via `time.monotonic()` |
| G9 | Tier 0/1 round-trip latency (local backend) | p95 < 500 ms end-to-end (QB inference + classify + PB inference + mcpd dispatch + audit log) with local QB; API backends documented separately (p95 < 1500 ms expected) |
| **G10** | **Backend parity** — all three backends (local, Anthropic, Gemini) emit schema-valid intents for the same 20-payload reference corpus | Same corpus run through each backend; resulting intents must (a) pass jsonschema, (b) classify to the same tier as the reference. Backend choice does not change safety classification |
| **G11** | **Multi-turn isolation** — INV-2-extended holds across REPL sessions | 10-turn session where turn N reads a file containing adversarial content with embedded instructions; mcpd's audit log shows zero unintended dispatches in turns N+1..10 (the QB summarises the content for the user but never lets it leak into the PB's prompt context) |

---

## §3 — Tool Catalogue & Tier Mapping (canonical)

The 22 tools mcpd ships, with their Phase 2 tier classification. **`risk_classifier.py` `_TIER0_TOOLS`/etc. sets MUST be regenerated from this in M2.0 and CI-asserted (G2).**

| Tool | Tier | Why |
|---|---|---|
| `system.status` | 0 | Read-only — uptime + load + mem + disk |
| `system.uptime` | 0 | Read-only |
| `system.cpu` | 0 | Read-only |
| `system.memory` | 0 | Read-only |
| `system.disk` | 0 | Read-only |
| `process.list` | 0 | Read-only |
| `process.inspect` | 0 | Read-only — single PID |
| `fs.read` | 0 | Read-only |
| `fs.list` | 0 | Read-only |
| `fs.stat` | 0 | Read-only |
| `fs.write` (in `$HOME`) | 1 | Low-risk write — auto + audit |
| `fs.write` (outside `$HOME`) | 3 | HITL — mcpd already returns `requires_cow_approval` |
| `fs.delete` | 3 | HITL — mcpd already returns `requires_cow_approval` |
| `service.logs` | 0 | Read-only |
| `service.start` / `stop` / `restart` | 2 | Medium-risk system change — auto + audit, summary back to user via QB |
| `network.status` | 0 | Read-only |
| `network.dns.read` | 0 | Read-only |
| `package.query` | 0 | Read-only |
| `package.install` / `remove` / `upgrade` | 2 | Medium — mcpd already returns `requires_cow_approval` |

`risk_level == "critical"` in the Intent Object maps to **Tier.HIGH** (Tier 3). No separate `CRITICAL=4` tier — the whitepaper defines exactly four. HITL prompt surfaces the "critical" label in its message text.

---

## §4 — Milestones M2.0 → M2.14

Ordered after Plan agent review and scope-round 2. Each milestone unlocks the next. All work happens on the GCP VM (`instance-20260528-030421`, us-central1-a) via SSH.

### M2.0 — VM pre-flight + skeleton fixes (1 day)
**Files:** `dual-brain/controller/risk_classifier.py`, `dual-brain/controller/intent_store.py`, `scripts/export_mcpd_catalogue.py` (new), `requirements.txt` (new)
**Actions:**
- Sanity-check VM Python (≥3.10), install `pip install jsonschema hypothesis pytest requests anthropic google-genai` into a project venv.
- Write `scripts/export_mcpd_catalogue.py` that reads `src/mcpd/src/schema.rs` (or runs `mcpd` with `tools/list`) and emits the canonical tool lists.
- Regenerate `_TIER0_TOOLS`, `_DESTRUCTIVE_TOOLS`, `_SYSTEM_WRITE_TOOLS` in `risk_classifier.py` from the exporter output. Drop the drifted entries.
- Widen `intent_store.revise()` lock — entire critical section under `self._lock`, not just the inner calls. Add concurrent test.
**Acceptance:** classifier unit tests cover every one of mcpd's 22 tools + drift assertion; concurrent revise test passes; venv builds clean on the VM.

### M2.1 — `McpdClient` (1 day)
**Files:** `dual-brain/controller/mcpd_client.py` (new), `dual-brain/controller/tests/test_mcpd_client.py` (new)
**Actions:** long-lived `subprocess.Popen` wrapper around `src/mcpd/target/release/mcpd`. JSON-RPC over stdin/stdout. Hard 10 s timeout per request; on timeout, kill subprocess and surface `tool_timeout` outcome. Parse `requires_cow_approval` responses into a typed `CowGated` result.
**Acceptance:** round-trips all 22 mcpd methods against the real binary; survives mcpd EOF/timeout.

### M2.2 — Intent schema validator (1 day)
**Files:** `dual-brain/controller/intent_schema.py` (new), `dual-brain/controller/tests/test_intent_schema.py` (new)
**Actions:** wrap the `jsonschema` library against `schemas/intent.json`. Surface structured `ValidationError` with field path. Normalise `timestamp`. Reject before any classification or store.
**Acceptance:** 100/100 malformed corpus rejected; 100/100 well-formed accepted; 10 k `hypothesis` iterations find zero crashes.

### M2.3 — Controller audit log with provenance (1 day)
**Files:** `dual-brain/controller/audit.py` (new), `dual-brain/controller/tests/test_audit.py` (new)
**Actions:** open `~/.local/state/icebreaker/controller-audit.log` with `O_APPEND | O_CREAT`, mode `0o640`. Line buffer + `os.fsync` after each write. Entry schema: `{ts, session_id, turn_index, intent_id, action, target, tier, reason, risk_level, outcome, duration_ms, user, backend, model, tokens_in, tokens_out, cost_estimate_usd}`. Redact param values matching secret patterns. Separate from `shell/pb_audit.py` and mcpd's audit log.
**Acceptance:** append-only verified by restart test; SIGKILL test does not lose the last line written; every test intent (accepted + rejected) lands in the log with full provenance.

### M2.4 — `BrainBackend` abstraction + config loader (2 days)
**Files:** `dual-brain/controller/backends/__init__.py` (new), `dual-brain/controller/backends/base.py` (new), `dual-brain/controller/config.py` (new), `~/.config/icebreaker/controller.toml.example` (new), `dual-brain/controller/tests/test_backends_base.py` (new)
**Actions:**
- Define the abstract `BrainBackend` class: `.complete(system: str, user: str, schema: dict | None, max_retries: int = 3) -> BrainResponse`. `BrainResponse` carries `{content_json, tokens_in, tokens_out, cost_usd, backend, model}`.
- All implementations validate output against `schema` and retry on schema failure up to `max_retries`. Local backend uses GBNF to make schema failures near-impossible; API backends rely on this retry loop as their primary safety floor (INV-2-pluggable).
- Config loader reads TOML: `[qb] backend = "local" | "anthropic" | "gemini"`, plus per-backend sections for model name, API key env var, max tokens.
- Constructor of every QB backend rejects a `mcpd_client=` kwarg (P2-F4 / G3 guard).
**Acceptance:** abstract interface tests (mock backend) pass; config loader handles each of the three backend choices; G3 guard test enforces `mcpd_client` kwarg rejection.

### M2.5 — `LlamaCppLocalBackend` (2 days)
**Files:** `dual-brain/controller/backends/llama_local.py` (new), `scripts/start_pb.sh` (new), `scripts/start_qb_local.sh` (new), `dual-brain/controller/tests/test_local_backend.py` (new)
**Actions:**
- HTTP client to llama-server's `/v1/chat/completions` (port 8080 for PB, port 8081 for local QB). Grammar-file pass-through. Hard timeout (30 s QB, 10 s PB). Re-parse with strict `json.loads` after grammar to catch truncation (P2-F14).
- `scripts/start_pb.sh` inlines `06_start_inference.sh` mode 2 (Qwen + 0.5B draft + `mcp_tool_call.gbnf`).
- `scripts/start_qb_local.sh` launches Phi-4-mini Q4_K_M on port 8081 with `qb_intent.gbnf`. Downloads model via `huggingface-cli` on first run if absent.
**Acceptance:** PB round-trips via local backend; local QB option round-trips when configured; truncation surfaces as `BrainTruncatedError`, never silent.

### M2.6 — `AnthropicBackend` (2 days)
**Files:** `dual-brain/controller/backends/anthropic.py` (new), `dual-brain/controller/tests/test_anthropic_backend.py` (new)
**Actions:** Anthropic Messages API via `anthropic` SDK. Uses **native JSON output mode** (`output_config.format = {"type": "json_schema", "schema": ...}`) — the provider's grammar-driven constrained decoding guarantees JSON shape server-side, without requiring `tool_use` (which would conflict with D17's outbound auditor blocklist). Our `transform_schema_for_provider` strips provider-unsupported keywords (`maxLength`, etc.) from the wire schema; the local `jsonschema` validator still enforces the full schema as the INV-2-pluggable safety floor. Model: `claude-haiku-4-5` (native JSON output requires Haiku 4.5+). API key from `ANTHROPIC_API_KEY` env var. Retry on schema failure up to 3 times. Cost estimate: track `input_tokens` + `output_tokens` * published rate.
**Acceptance:** identical 20-corpus reference run as local backend produces identical tier classifications (G10 parity test); 0 schema bypass after retries; cost tracked in audit log.

### M2.7 — `GeminiBackend` (2 days)
**Files:** `dual-brain/controller/backends/gemini.py` (new), `dual-brain/controller/tests/test_gemini_backend.py` (new)
**Actions:** Google AI Studio Gemini API via `google-generativeai` SDK. Uses **structured output via `response_schema`** (Gemini's native JSON mode with schema constraint). Model: `gemini-2.0-flash` (very cheap, generous free tier). API key from `GEMINI_API_KEY` env var. Same retry-on-schema-failure shape as Anthropic backend.
**Acceptance:** G10 parity holds across local + Anthropic + Gemini for the 20-corpus reference; 0 schema bypass after retries.

### M2.8 — `qb_intent.gbnf` + parity testing (2 days)
**Files:** `dual-brain/controller/grammars/qb_intent.gbnf` (new), `dual-brain/controller/tests/test_grammar_parity.py` (new)
**Actions:** seed from `llama.cpp/examples/json-schema-to-grammar.py` against `schemas/intent.json`. Hand-tighten the `pattern` constraints (action regex, shell-metachar denylist on target/params strings). Differential test: 200+ corpus of valid + malformed intents → GBNF-accepts ⊆ jsonschema-accepts. 10 k random-byte fuzz finds zero divergences. (Only relevant when local backend is active; API backends bypass GBNF.)
**Acceptance:** every valid corpus intent accepted by both; every malformed corpus intent rejected by both; zero fuzz divergences.

### M2.9 — HITL terminal gate (1 day)
**Files:** `dual-brain/controller/hitl.py` (new), `dual-brain/controller/tests/test_hitl.py` (new)
**Actions:** `HitlPrompt(intent, classification).ask() -> Decision` shows intent + target + risk + tier label + backend used + (if available) mcpd `requires_cow_approval` dry-run summary. **3-second `time.sleep(3)` before any input is read.** 30-second decision timeout via `select`/`signal` = deny. `[A]` and `[D]` only; `[E]` and `[M]` are stubs that print "not implemented in V1 — Phase 5". `isatty()` fallback: in non-TTY contexts, decision defaults to deny.
**Acceptance:** lockout verified by mocking `input()` and measuring elapsed time (G8); timeout test asserts deny; non-TTY test defaults to deny.

### M2.10 — System prompts (per-backend variants) (1 day)
**Files:** `dual-brain/controller/prompts/qb_local.txt` (new), `dual-brain/controller/prompts/qb_anthropic.txt` (new), `dual-brain/controller/prompts/qb_gemini.txt` (new), `dual-brain/controller/prompts/pb.txt` (new)
**Actions:** craft system prompts per backend (each provider responds best to slightly different instruction styles).
- QB common content: "translate to Intent Object, output JSON only, never include code/paths/secrets the user did not explicitly name."
- PB single prompt: "receive intent UUID + tool catalogue, emit one MCP tool call, never explain."
**Acceptance:** all three QB variants pass adversarial red-team smoke test; PB prompt produces valid `mcp_tool_call.gbnf`-compliant output.

### M2.11 — Session state + multi-turn REPL (3 days)
**Files:** `dual-brain/controller/session.py` (new), `dual-brain/controller/repl.py` (new), `dual-brain/controller/tests/test_session.py` (new), `dual-brain/controller/tests/test_repl_isolation.py` (new)
**Actions:**
- `SessionState`: holds the QB's conversation memory (system prompt + alternating user/assistant messages with intent + tool-result-summary turns), session_id (UUID), turn_index, last-activity timestamp.
- **INV-2-extended enforcement**: when mcpd returns content (file content, logs, package descriptions), the orchestrator passes it to QB-summarisation-only path, then strips it before any subsequent PB call. The PB's prompt template per turn is rebuilt from scratch using ONLY the new intent UUID + tool catalogue — never inherits PB-history.
- Session TTL = 30 minutes since last activity. `/exit`, `/quit`, Ctrl+D exit. `/reset` clears memory but keeps session.
- REPL is `prompt_toolkit`-based for command history + readline editing.
**Acceptance:** 10-turn session where turn N has the QB read a file with embedded injection → turns N+1..10 show zero unintended dispatch (G11); session_id stamps every audit row.

### M2.12 — Main orchestration loop (2 days)
**Files:** `dual-brain/controller/__main__.py` (new), `dual-brain/controller/main.py` (new), `dual-brain/controller/tests/test_e2e.py` (new)
**Actions:** the end-to-end pipeline that both `--repl` and single-shot share:
1. Load config; instantiate the configured QB backend + the local PB backend; health-probe both.
2. Spawn `McpdClient` (one per session).
3. Per turn:
   a. Read user input (REPL) or arg (single-shot).
   b. Call configured QB backend → Intent Object candidate.
   c. Validate against `intent_schema.py` → reject on failure (audit + skip turn).
   d. Classify via `risk_classifier.classify(intent)` with `os.path.realpath` resolution (P2-F12).
   e. If Tier 3: HITL gate → on deny, audit + skip turn.
   f. `intent_store.put(intent)` → get `intent_id`.
   g. Call PB with `intent_id` only (no raw text) → tool call `{"tool":..., "params":...}`.
   h. Post-validate tool call against mcpd's per-tool schema (reject = audit + skip).
   i. QB verifier round-trip — ask the **same QB backend** "does this tool call match the original intent?" using only structured fields. On no, abort turn.
   j. Wrap into JSON-RPC, dispatch via `McpdClient`. Handle `requires_cow_approval` → re-engage HITL with dry-run.
   k. Pass mcpd output strings to QB ONLY for summarisation. Tool-output never re-enters PB context (INV-2-extended).
   l. Write audit entry with full provenance. Display summary. Loop.
4. Single-shot exits after first turn; REPL loops until exit signal.
**Acceptance:** Tier 0 happy path (`"what's my disk usage?"`) returns parsed info on each backend; Tier 3 `delete /etc/hosts` fires HITL; deny default = no dispatch; REPL maintains session across turns; backend switch via `--backend anthropic` works.

### M2.13 — Adversarial + fuzz test suite across all backends (3 days)
**Files:** `dual-brain/controller/tests/test_adversarial.py` (new), `dual-brain/controller/tests/test_injection_corpus.py` (new), `dual-brain/controller/tests/corpus/*.json` (new)
**Actions:** assemble 100+ prompt injection payloads (Simon Willison's set + PromptBench + tool-output reflection cases + multi-turn poisoning sequences). 100+ malformed intents. `hypothesis` fuzz strategies for schema validator (10 k iter) and `intent_store.revise` (race + correction-fields). Each payload runs end-to-end **on each backend** (3× cost but it's the gate). Assertion: mcpd's audit log shows only HITL-denied or schema-rejected outcomes, never destructive dispatch.
**Acceptance:** 0/100+ payloads cause unintended dispatch on any backend (G6 across backends); 0 schema bypass; 0 fuzz crashes in 60 s.

### M2.14 — Exit gates + closeout (1 day)
**Files:** `dual-brain/controller/ci.sh` (new), `docs/phase2/phase2_roadmap.md` (§11 Closeout added)
**Actions:** ci.sh runs G1–G11 sequentially. Final docs update with per-gate evidence. PR opened with security-critical sign-off ask.
**Acceptance:** all G1–G11 green; PR awaits two-reviewer approval.

---

## §5 — Cross-Cutting Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python | Skeleton is Python; latency budget allows it (brain inference dominates); fastest path to working orchestration |
| Dev + test target | **Linux VM only** (`instance-20260528-030421`, us-central1-a) | User-specified scope round 2. Matches production exactly; mcpd integration tests against real Linux Landlock + seccomp; avoids macOS-vs-Linux drift |
| Process model | Per-session: spawn mcpd subprocess at REPL start, kill at REPL exit. Long-lived llama-server (PB always; local QB if configured). Single-shot is a one-turn session. | Daemon mode (systemd / socket perms) is Phase 5; cold-mcpd ~50 ms amortises across all session turns; blast radius bounded per session |
| **QB backend** | **Pluggable**: `local` (Phi-4-mini via llama.cpp + GBNF), `anthropic` (Claude Haiku 4.5 via Messages API + native JSON output `output_config.format`), `gemini` (Gemini 2.0 Flash via `response_schema`). Runtime-selectable via `~/.config/icebreaker/controller.toml` + `--backend` CLI flag. | User-specified scope round 2. Local default preserves whitepaper guarantees; API backends enable better quality / lower setup friction. `BrainBackend` interface is designed so OAuth backends (Claude.ai, ChatGPT Plus) drop in for Phase 5 |
| QB safety floor per backend | Local → GBNF makes invalid output near-impossible. API → mandatory `jsonschema` validation + retry-on-malformed up to 3 attempts (INV-2-pluggable) | GBNF is local-only. APIs guarantee output-shape via their native JSON output features (Anthropic `output_config.format`, Gemini `response_schema`) but their providers can still emit drift; retry loop is the safety floor |
| PB backend | **Local fine-tuned Qwen 2.5 Coder 1.5B** (`models/run7_cot_q4km.gguf`), `mcp_tool_call.gbnf` constrained, llama-server port 8080. **Non-negotiable.** | PB has tool access. Sending an API provider every mcpd call + result violates the on-device safety guarantee — the whole reason for two brains disappears. Locked. |
| Brain transport (local) | llama-server HTTP `/v1/chat/completions`, grammar-file pass-through | OpenAI-compatible client lib (`requests` or `openai` Python SDK) |
| Brain transport (Anthropic) | `anthropic` SDK, Messages API with native JSON output (`output_config.format = "json_schema"`) | Server-side grammar-constrained decoding per Anthropic structured-output docs; no `tools` / `tool_use` / prefill needed, so D17 auditor stays clean |
| Brain transport (Gemini) | `google-generativeai` SDK with `response_schema` config | Native JSON shape guarantee per Gemini docs |
| Brain lifecycle | Controller does NOT start llama-servers. `scripts/start_pb.sh` + `scripts/start_qb_local.sh` for ops. API backends require no startup. Health probe on Controller init | llama-server load is 5–20 s; unacceptable in REPL boot; crash supervision is Phase 5 |
| PB → mcpd wire | PB emits `{"tool":..., "params":...}`; Controller wraps into full JSON-RPC | Clean separation; PB stays small and tool-agnostic |
| QB grammar | New `qb_intent.gbnf` (local backend only), seeded from `json-schema-to-grammar.py`, hand-tightened for `pattern` | Generators skip `pattern`; metachar regex is the security gate. API backends do not use this; they get retry-on-schema-failure instead |
| GBNF / schema parity | Differential testing — corpora through both validators; CI asserts `GBNF ⊆ schema` | Only sound check without a verified generator |
| Tier mapping | `risk_level == "critical"` → `Tier.HIGH`; no new tier | Whitepaper defines 4 tiers; "critical" is a user-facing *label*, HITL surfaces it in text |
| **Interaction model** | **Multi-turn REPL** (`python -m controller --repl`) is the default. Single-shot (`python -m controller "<one>"`) is a one-turn auto-exit session. `prompt_toolkit` for line editing + history | User-specified scope round 2 |
| Session lifecycle | TTL 30 minutes since last activity. `/exit`, `/quit`, Ctrl+D close. `/reset` clears memory but keeps session. session_id (UUIDv4) stamps every audit row | Bounds memory; matches OS session feel; correlatable in audit log |
| **Tool-output reflection** | mcpd output strings → QB only for summarisation. NEVER inherits into PB context. The PB prompt template is rebuilt from scratch each turn using intent UUID + tool catalogue. INV-2-extended | V1-blocking because we have multi-turn. Closes second-order injection vector (file with embedded instructions) |
| QB verifier round-trip | After PB emits tool call, ask the **same QB backend** "does this tool call match the original intent?" with structured fields only. Abort on no | Whitepaper's second-pass defense; same backend so consistency holds even mid-session |
| Audit log | `~/.local/state/icebreaker/controller-audit.log`, O_APPEND, 0o640, line-buffered + per-line `fsync`. Includes `session_id`, `turn_index`, `backend`, `model`, `tokens_in/out`, `cost_estimate_usd` | Provenance + cost audit per backend |
| API key storage | Env vars (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`). Config references env var *names*, never key values | Keys must not appear in config files / logs / audit |
| HITL V1 | `[A]` + `[D]` only. 3 s lockout, 30 s timeout → deny | Two-button gate is the safety contract; `[E]`/`[M]` are Phase 5 UX |
| Tier 2 LLM pass | Not in Phase 2; rule-based only | Whitepaper roadmaps it for Phase 5 |
| Adversarial corpus | 100+ payloads × 3 backends. `hypothesis` fuzz on schema and revise | 20/20 is anecdote; threshold is "0 unintended dispatch on ANY backend" |

---

## §6 — Dependencies & Prerequisites

**Python packages** (new to project):
- `jsonschema` ≥ 4.0 — schema validation
- `hypothesis` ≥ 6.0 — fuzz testing
- `requests` ≥ 2.30 — HTTP to llama-server
- `pytest` ≥ 7.0 — test runner
- `anthropic` ≥ 0.40 — Anthropic Messages API client (for `AnthropicBackend`)
- `google-generativeai` ≥ 0.8 — Gemini API client (for `GeminiBackend`)
- `prompt-toolkit` ≥ 3.0 — REPL line editing + history (M2.11)
- `tomli` (Python 3.10) — TOML config parsing

**System tools** already in use:
- `llama.cpp` / `llama-server` (Phase 4)
- `huggingface-cli` (Phase 4)
- `gbnf-validator` (from llama.cpp build) — for M2.8 differential tests
- system Python 3.10+ on the VM

**Model files:**
- `models/run7_cot_q4km.gguf` (986 MB, present on VM) — PB, mandatory
- `models/Phi-4-mini-instruct-Q4_K_M.gguf` (~3 GB, M2.5 downloads to VM via `huggingface-cli`) — local QB backend, optional (only if `qb.backend = "local"`)
- `privileged-brain/inference/draft_models/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf` (300 MB, downloaded by `start_pb.sh` if absent)

**API access (optional, only for the corresponding backend):**
- Anthropic API key in `ANTHROPIC_API_KEY` env var. Default model `claude-haiku-4-5` (required for native JSON output mode). ~$1 / 1M input tokens, $5 / 1M output tokens (confirm against published rates at deploy time)
- Google Gemini API key in `GEMINI_API_KEY` env var. Default model `gemini-2.0-flash`. Free tier covers ~1,500 requests/day for dev

**mcpd binary:** `src/mcpd/target/release/mcpd` — built and tested in Phase 1, present on VM.

---

## §7 — Files Added or Modified

| Path | Action | Milestone |
|---|---|---|
| `dual-brain/controller/risk_classifier.py` | Modify (regenerate tool sets) | M2.0 |
| `dual-brain/controller/intent_store.py` | Modify (widen revise lock) | M2.0 |
| `dual-brain/controller/schemas/intent.json` | Reuse as-is | — |
| `dual-brain/controller/mcpd_client.py` | New | M2.1 |
| `dual-brain/controller/intent_schema.py` | New | M2.2 |
| `dual-brain/controller/audit.py` | New | M2.3 |
| `dual-brain/controller/config.py` | New | M2.4 |
| `dual-brain/controller/backends/__init__.py` | New | M2.4 |
| `dual-brain/controller/backends/base.py` | New | M2.4 |
| `dual-brain/controller/backends/llama_local.py` | New | M2.5 |
| `dual-brain/controller/backends/anthropic.py` | New | M2.6 |
| `dual-brain/controller/backends/gemini.py` | New | M2.7 |
| `dual-brain/controller/grammars/qb_intent.gbnf` | New | M2.8 |
| `dual-brain/controller/hitl.py` | New | M2.9 |
| `dual-brain/controller/prompts/qb_local.txt` | New | M2.10 |
| `dual-brain/controller/prompts/qb_anthropic.txt` | New | M2.10 |
| `dual-brain/controller/prompts/qb_gemini.txt` | New | M2.10 |
| `dual-brain/controller/prompts/pb.txt` | New | M2.10 |
| `dual-brain/controller/session.py` | New | M2.11 |
| `dual-brain/controller/repl.py` | New | M2.11 |
| `dual-brain/controller/main.py` | New | M2.12 |
| `dual-brain/controller/__main__.py` | New | M2.12 |
| `dual-brain/controller/tests/*.py` | New (test_*) | M2.1 onward |
| `dual-brain/controller/tests/corpus/*.json` | New | M2.13 |
| `dual-brain/controller/ci.sh` | New | M2.14 |
| `scripts/export_mcpd_catalogue.py` | New | M2.0 |
| `scripts/start_pb.sh` | New | M2.5 |
| `scripts/start_qb_local.sh` | New | M2.5 |
| `requirements.txt` | New | M2.0 |
| `~/.config/icebreaker/controller.toml.example` | New | M2.4 |
| `models/Phi-4-mini-instruct-Q4_K_M.gguf` | New (downloaded by start_qb_local.sh if absent) | M2.5 |
| `docs/phase2/phase2_roadmap.md` | This file | planning |
| `docs/phase2/2026-06-05_plan_summary.md` | New (layman summary) | planning |
| `docs/phase2/findings.md` | New (research surface) | planning |
| `shell/pb_*` | **Do not modify** (V1 preserved) | — |

---

## §8 — Verification

After M2.14, on the GCP VM (`instance-20260528-030421`):

```bash
# 1. Bring up PB llama-server (one-time, leave running)
./scripts/start_pb.sh                       # port 8080

# 2. (Optional, only if backend=local) Bring up local QB llama-server
./scripts/start_qb_local.sh                 # port 8081

# 3. (Only for backend=anthropic / gemini) export API keys
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=...

# 4. Install Controller deps
cd dual-brain/controller
pip install -r ../../requirements.txt && pip install -e .

# 5. Run all gates
./ci.sh                                      # G1-G11 green

# 6. Backend smoke (each backend, single-shot)
python -m controller --backend local     "what's my disk usage?"
python -m controller --backend anthropic "what's my disk usage?"
python -m controller --backend gemini    "what's my disk usage?"

# 7. Multi-turn REPL smoke
python -m controller --repl --backend local
> what's my disk usage?           # Tier 0 — auto-returns
> install htop                    # Tier 2 — auto + audit
> delete /etc/hosts                # Tier 3 — HITL fires, default deny
> /exit

# 8. Confirm audit log carries full provenance
tail -4 ~/.local/state/icebreaker/controller-audit.log | jq '.session_id, .backend, .turn_index, .cost_estimate_usd'
```

**Pass criteria:**
- `ci.sh` exits 0 (G1–G11 all pass).
- Each backend returns identical tier classification on the 20-corpus reference set (G10).
- REPL maintains a single `session_id` across all turns until exit.
- Multi-turn isolation (G11) holds: a turn that reads adversarial file content does not cause subsequent turns to dispatch unintended tools.
- Audit log lines for each turn carry `session_id`, `turn_index`, `backend`, `model`, `tokens_in/out`, `cost_estimate_usd`.
- Tier 0 query returns parsed disk info within p95 < 500 ms on local backend (API backends documented separately, p95 < 1500 ms expected).
- Instrumented capture of PB request body shows zero occurrences of raw user input text on any backend.
- `python -m controller --check-isolation` confirms every QB backend has zero MCP attachment.

---

## §9 — Failure Modes Register

| # | Failure | Mitigation |
|---|---|---|
| P2-F1 | Raw user text leaks into PB context | PB input is the UUID string only; instrumented G4 test; type assertion in `PrivilegedBrain.call(intent_id: str)` |
| P2-F2 | Schema validation bypassed | `jsonschema` strict + GBNF as belt-and-suspenders; G5 fuzz |
| P2-F3 | Audit log not append-only / loses last lines | O_APPEND + explicit `fsync` per line; SIGKILL test |
| P2-F4 | Both brains cross-wired to MCP | `QuarantinedBrain.__init__` rejects `mcpd_client=` kwarg; G3 assertion |
| P2-F5 | Classifier drift vs mcpd catalogue | G2 ci.sh guard; M2.0 export script regenerates from `schema.rs` |
| P2-F6 | HITL 3 s lockout bypassed | G8 timing test; `input()` only called *after* `time.sleep(3)`; no shortcut path |
| P2-F7 | QB grammar accepts intents schema rejects | M2.5 differential test in CI |
| P2-F8 | mcpd subprocess hangs | 10 s timeout in `McpdClient`; kill + audit `tool_timeout` |
| P2-F9 | PB emits call for non-existent tool / wrong shape | `BrainClient.PrivilegedBrain` post-validates against mcpd tool schema; reject → audit + abort |
| P2-F10 | HITL approval on null/expired intent | Defensive null check; expired intents fail at `intent_store.get` |
| **P2-F11** | **Tool-output reflection injection** (file content → PB → exec) | **INV-2-extended**: mcpd output strings flow back to QB only. Never re-enter PB. Enforced in M2.8 orchestration |
| **P2-F12** | **TOCTOU between classify and execute** (symlink swap) | Classifier resolves symlinks via `os.path.realpath` before tier decision; audit logs post-resolution path |
| **P2-F13** | **`intent_store.revise()` race** under concurrent QB calls | M2.0 widens lock — entire revise critical section under `self._lock` |
| **P2-F14** | **llama-server returns truncated JSON** (token limit) | `BrainClient` re-parses with strict `json.loads`; truncation surfaces as `BrainTruncatedError`, not silent success |
| **P2-F15** | **Brain crash mid-request** hangs Controller | Hard HTTP timeout (30 s QB, 10 s PB); explicit health-check before reuse |
| **P2-F16** | **Audit log buffered in stdlib I/O**, lost on SIGKILL | Line buffering + `os.fsync(fd)` after every write |
| **P2-F17** | **API backend returns valid-JSON but semantically wrong intent** (e.g. classifies own action wrong, swaps target) | Retry-on-malformed up to 3 attempts; QB verifier round-trip catches semantic drift; G10 backend parity test on 20-corpus catches systematic bias |
| **P2-F18** | **API rate limit / network outage during a session** | Per-backend exponential backoff; on persistent failure, abort intent + audit + suggest backend switch; never silently drop |
| **P2-F19** | **API key leaks into logs / audit / error messages** | API key access via `os.environ` at backend init only; never serialised; audit log records `backend` name + `model` but not key. Error messages from SDK redacted before log |
| **P2-F20** | **Session memory grows unbounded** | Session TTL 30 min; max 50 turns; on overflow, oldest turns elided with explicit marker in audit log |
| **P2-F21** | **Tool-output reflection across turns** (file content in turn N poisons PB context in turn N+1) | INV-2-extended enforcement in M2.11: PB prompt template rebuilt per turn from intent UUID + tool catalogue only; never inherits prior turns' content; G11 verifies |
| **P2-F22** | **Backend hot-swap mid-session** changes intent classification | Backend switch (`/backend anthropic`) starts a fresh session with new session_id; old session_id closed in audit log; no carryover |

---

## §10 — What's Deferred to Later Phases

| Item | Phase | Why |
|---|---|---|
| Tier 2 LLM classifier pass | Phase 5 | Whitepaper places it there; rule-based is sound for V1 |
| `[E]xplain` and `[M]odify` HITL actions | Phase 5 | UX scope; two-button safety contract is what V1 needs |
| Daemon mode (long-running Controller) | Phase 5 | Needs systemd / supervision / log rotation |
| OpenAI backend (GPT-4o-mini) | Phase 5 | Anthropic + Gemini cover the API option for V1; OpenAI integration cost is meaningful and parallel |
| OAuth subscription backends (Claude.ai, ChatGPT Plus) | Phase 5 | Requires hosted OAuth callback infra + token refresh + per-provider browser auth. `BrainBackend` interface is designed to absorb them without breaking changes |
| `schema_version` negotiation logic | Phase 7 | Optional field; no live drift problem yet |
| Audit log rotation / compression | Phase 5 | logrotate config |
| `intent_store.revise()` wired into orchestration | Phase 5 | Only QB verifier path uses it; full revise flow is UX work |
| COW dry-run integration (overlay mount) | Phase 3 | Phase 2 surfaces mcpd's `requires_cow_approval` verbatim |
| QB-verifier majority voting (k=3) | Phase 5 | V1 single-pass + HITL is sufficient |
| Per-session cost ceiling + alerts | Phase 5 | V1 logs cost; alerting / hard caps are UX |
| GUI / TUI beyond `prompt_toolkit` REPL | Phase 5 | `prompt_toolkit` is the right surface for V1 |

---

## §11 — Closeout

*Reserved. Will be filled at the end of Phase 2 with per-gate evidence, the three side-discoveries from canonical Linux verification (mirroring Phase 1 closeout), and the five commits that closed the phase.*

---

## §12 — References

- `AI_Native_OS_Whitepaper.md` § Dual-Brain Architecture, § Controller, § Graduated Determinism (Tier 0–3)
- `docs/implementation_plan.md` lines 198–243 — Phase 2 spec
- `docs/ARCHITECTURE.md` lines 75–91, 320–350, 454–460 — Controller responsibilities, latency budgets
- `docs/V1_ROADMAP.md` lines 137–168, 186–195 — HITL UX, exit criteria
- `docs/phase1_roadmap.md` — template structure; mcpd interface contract
- `CLAUDE.md` § INV-1 through INV-8, § Test-Only Knobs (the `fs-test-roots` pattern)
- `dual-brain/controller/schemas/intent.json` — Intent Object canonical schema
- `src/mcpd/src/schema.rs` — 22-tool catalogue source of truth
- `privileged-brain/inference/grammar/mcp_tool_call.gbnf` — PB output grammar
- `privileged-brain/06_start_inference.sh` — llama-server launcher reference
- Simon Willison, "Prompt Injection" tag — <https://simonwillison.net/tags/prompt-injection/>
- llama.cpp `json-schema-to-grammar.py` — GBNF generator seed for M2.5
