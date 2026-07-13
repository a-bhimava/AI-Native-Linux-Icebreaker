# Icebreaker v6.8 Implementation Plan

**Date:** 2026-07-13
**Author:** Session with Claude Opus 4.7
**Status at time of writing:** v6.7 dual-arch ISOs built and downloaded; live UTM sweep surfaced 4 P0 UX bugs (F-59, F-60, F-61, F-62); ship strategy pivoted from `v1.0-rc1` to `v6.7-known-issues`; Option 4 selected for v6.8 architecture (LiteLLM + LangGraph runtime).

**Purpose.** Preserve every decision from tonight's session in a form that survives context-window resets and future Claude conversations. Read this before any code change targeting v6.8.

---

## 1. Snapshot: what shipped in v6.7 (2026-07-13)

| Item | Path / Value |
|---|---|
| Repo state | branch `fix/scope-h-docs` on `origin/main`, tarball push flow |
| amd64 ISO | `~/Documents/Icebreaker/ISO/incremental/v6.7-amd64.iso` (3.3 GB) |
| amd64 SHA-256 | `a28851a159e37e0b4b2d4c5a1bd363bb6ae6ba036e95593a8a547701581e8ff4` |
| arm64 ISO | `~/Documents/Icebreaker/ISO/incremental/v6.7-arm64.iso` (3.3 GB) |
| arm64 SHA-256 | `bfb7b033c4b4cacd7052435c362df2172946bb836426c762ce6f297b3f984c0f` |
| Also on VM | `icebreaker-phase2-vm:/home/aditya/Icebreaker/incremental/.build/out/v6.7-*.iso` |
| amd64 QEMU gate | Not run to completion (TCG on GCP VM = no KVM; slow). Killed intentionally to focus on arm64. |
| arm64 QEMU gate | rc=1 — one FAIL was **stale**: `autostart .desktop missing`. Every substantive check PASSED: boot, gdm, controller, socket, RPC, restart, pbd + llama-server /health, INV-3 no TCP, INV-7 model checksum, in-guest seccomp harvest CLEAN (F-55 confirmed working on aarch64). |
| Tag decision | Do NOT tag `v1.0-rc1`. Ship v6.7 as **`v6.7-known-issues`**. v6.8 gets the tag consideration. |

### 1.1 Bugs fixed and shipped in v6.7 build cycle (relative to v6.65)

- F-55: `SYS_faccessat` + `SYS_faccessat2` added to seccomp allowlist (arm64 needs — arm64 has no `access(2)`). VERIFIED in-guest harvest CLEAN.
- F-56: `ResultEvent` NameError in `_emit_unsupported` (main.py:2470) — module-level import added.
- F-57: Autostart `.desktop` files deleted (first-boot UX regression).
- F-58: `SYS_access` added under `#[cfg(target_arch = "x86_64")]` (amd64 legacy syscall 21).
- v6.7 build-time bugs fixed: v2.manifest PKG-1 data-file copy order (schemas/prompts/grammars/catalogue.toml must be copied BEFORE F-51 marker check loop); v6.manifest checksums.sha256 needed filtering to `.gguf` lines only before embedding; smoke-gate faccessat-symbol strings check replaced with source-level assertion.

### 1.2 Known issues surfaced in v6.7 UTM sweep

These are documented in Section 3 and become v6.8's must-fix list.

- **F-59** (P0): Control Center silent crash on `Gtk.PasswordEntry.set_placeholder_text`
- **F-60** (P0): "take me to X" navigation queries return `system.unsupported`
- **F-61** (P0): Terminal HITL: no approval prompt renders in TUI; silent timeout → deny
- **F-62** (P0): No task decomposition, no follow-up context (compound + follow-up queries fail)

---

## 2. Scope K — Ship `v6.7-known-issues` (docs-only, no code)

**Do not tag v1.0-rc1. Do not rebuild ISO. Do these three things and stop.**

### K.1 — Invert stale smoke-gate autostart check

**File:** `incremental/build/smoke-gate.sh:224-232`
**Current:** Checks that autostart `.desktop` is absent (already correct).
**Bug:** arm64 qemu-gate.sh output shows `[FAIL] autostart .desktop missing` — so a DIFFERENT check (in `qemu-gate.sh` itself, not smoke-gate) is asserting presence. Find it and invert per F-57 semantic (files are intentionally absent).

**How to fix:** `grep -n "autostart" incremental/tests/qemu-gate.sh`. If it asserts a present `.desktop`, either remove or invert to assert absence.

### K.2 — Log F-59, F-60, F-61, F-62 in `incremental/GROUND_TRUTH.md § 7`

Format matches existing entries:

```
### F-59 — Control Center silent crash on Gtk.PasswordEntry.set_placeholder_text (2026-07-13)
Subsystem: gui.control.pages.keys_page
Symptom: Clicking "Icebreaker Control Center" menu icon does nothing on both amd64 and arm64.
Root cause: PyGI on Ubuntu 24.04 does not expose Gtk.Editable-interface's set_placeholder_text() as a bound method on Gtk.PasswordEntry. Icebreaker's KeyRow uses it directly.
Fix (source): dual-brain/gui/control/pages/keys_page.py:63 — replaced with self._entry.set_property("placeholder-text", ...).
Hot-patch on running guest: `sudo sed -i 's|self._entry.set_placeholder_text(|self._entry.set_property("placeholder-text", |' /opt/icebreaker/venv/lib/python3.12/site-packages/gui/control/pages/keys_page.py`
Regression lock (v6.8): dual-brain/gui/control/pages/tests/test_keys_page.py — headless GTK4/PyGI init that instantiates KeyRow on Linux CI.
Status: source-fixed on branch; NOT included in v6.7-known-issues ISO. Ships in v6.8.

### F-60 — "take me to X" navigation → system.unsupported (2026-07-13)
Subsystem: controller.main / prompts.qb_intent
Symptom: User types "take me to Downloads folder" → response "I can't do that: 'take me to Downloads folder'".
Root cause: _SUPPORTED_ACTIONS has no nav.cd / fs.cd / app.files.open action; QB has no rule to map nav-phrases to fs.list.
Fix (v6.8 M7.3): rewrite qb_intent.txt with rule: "take me to X" / "go to X" / "cd X" → fs.list <resolved_path>. Ships fs.list of the target folder + a friendly "Here's what's in <path>:" response.
Regression lock (v6.8): intent_corpus.json rows: nav.downloads.001, nav.home.001, nav.tmp.001.
Status: design decision → v6.8.

### F-61 — Terminal HITL: no approval prompt shown; silent DENY on timeout (2026-07-13)
Subsystem: controller.hitl / terminal.app
Symptom: For Tier-3 destructive ops, Interpretation panel shows "HITL Approval" step then "hitl_timeout" then "Operation denied" — user was never presented with a prompt.
Root cause: dual-brain/terminal/app.py has zero HITL handling. AiTerminalPresenter class exists (terminal/presenter.py) but nothing wires its set_callbacks() at TUI startup, no handle_hitl_prompt() dispatch. Daemon defaults to controller/hitl.py::TerminalPresenter which raw-reads stdin — Textual has already claimed stdin — read returns nothing → Decision.TIMEOUT → BP-4 default-to-deny.
Fix (v6.8 M7.7): wire AiTerminalPresenter.set_callbacks from terminal/app.py.on_mount; add handle_hitl_prompt(params) that renders inline card (per L6); add keypress y/n/Esc → presenter.submit_decision; teach daemon to accept `preferred_presenter=ai_terminal` in session-open handshake.
Regression lock (v6.8): dual-brain/terminal/tests/test_hitl_e2e.py — spins up mock daemon + Textual app, sends hitl.prompt notification, asserts prompt appears + keypress produces Decision.APPROVE/DENY.
Status: design decision → v6.8.

### F-62 — No task decomposition, no follow-up context (2026-07-13)
Subsystem: controller.main / controller.session
Symptom (compound): "get my IP and save it to a file" → returns IP OR saves an empty file, not both.
Symptom (follow-up): "what is my IP?" (returns IP) → "save that as a text file" → treated as fresh turn, no memory of previous IP.
Root cause: main.run_turn treats each turn independently; SessionConfig carries no history list; QB prompt has no plan mode; there is no Planner→Executor→Replanner architecture.
Fix (v6.8 M7.4 + M7.5): plan-and-execute via LangGraph adapter + session-persistent TurnMemory list (last N=10 turns, redacted per BP-8).
Regression lock (v6.8): dual-brain/controller/tests/test_plan_executor.py (compound intent) + test_session_memory.py (follow-up context).
Status: design decision → v6.8.
```

### K.3 — Create `docs/RELEASE_NOTES_v6.7.md`

Use the 13-section template from Scope H4 but with these changes:
- Header: `v6.7-known-issues` (NOT `v1.0-rc1`)
- Overview: single paragraph stating this ISO is functional but ships with 4 known P0s that will be fixed in v6.8
- **Known Issues** section: F-59/F-60/F-61/F-62 with exact user-impact + workaround:
  - F-59 workaround: `sudo sed -i` hot-patch listed above
  - F-60 workaround: use `list <path>` instead of `take me to <path>`
  - F-61 workaround: do NOT run Tier-2/3 (destructive) intents in the terminal until v6.8 — they will silently deny after timeout
  - F-62 workaround: compose queries as single-step per turn
- Component versions table filled from SHAs (amd64 `a28851a1…`, arm64 `bfb7b033…`)
- Explicit: **NO git tag**, **NO GPG signing** this cycle
- Appendix A: extend bug-fix table with F-55/F-56/F-57/F-58/F-59/F-60/F-61/F-62
- Placeholder block for user-appended F-63+ as they surface more bugs

### K.4 — Not touched this cycle

- No git tag
- No new ISO build
- No source-code changes ship in v6.7-known-issues (the F-59 source fix stays on the v6.8 branch)
- No smoke-gate changes beyond K.1

---

## 3. Scope L — Claude-Code-parity strategic axes (context)

Six independent axes to close the gap with Claude Code / Cursor. User selected L1 + L2 + L4 + L5 as v6.8 priorities.

| Axis | Description | Cost | Latency win | Security impact |
|---|---|---|---|---|
| L1 Tier-0 fast path | Skip PB + verifier for read-only intents (Tier 0/1); dispatch QB intent directly to mcpd | 1 day | −70% on read-only | none (INV-2 + INV-5 still fire) |
| L2 Streaming | Token-stream CoT, tool-call assembly, mcpd stdout live to companion panel | 3 days | Feels alive | none (UI-only) |
| L3 Local QB (Phi-4-mini) | 500MB local fast QB; escalate to Gemini for Tier 1+ | 3 wk | −500ms first token | ↑ (no cloud egress) |
| L4 Verifier tier-floor | Verifier voting only for Tier 2+ | 2h | −2s on Tier 0/1 | slight ↓ (still sandboxed) |
| L5 Prompt engineering pass | Rewrite QB/PB prompts with Claude Code rigor + 20 few-shot | 2 wk | Fewer round-trips | none |
| L6 Inline HITL with COW dry-run | Approval card shows dry-run diff + blast radius; matches Cursor tool-preview | 1 wk | Fewer clicks | ↑ (preview visible) |
| L7 Session memory + autocomplete | Bounded per-user session; suggest completions from recent intents | 3 wk | −40% on repeat | slight ↓ (audited) |

**v6.8 = L1 + L2 + L4 + L5 (user-selected).** L3 → v7.0. L6 → v6.9. L7 → v6.10.

---

## 4. Scope M — v6.8 architecture: plan-and-execute mapped to dual-brain

### 4.1 Blueprint

```
User query
    ↓
QB Planner (Gemini via LiteLLM) reads: (query, session_history, shell_context)
    → emits Plan JSON: [{step 1: intent}, {step 2: intent}, …]
    → validated against PlanSchema (controller/schemas/plan.json)
    ↓
Executor loop (per step, LangGraph state graph):
    → PB grammar-decodes step_i → mcp_tool_call
    → INV-2 schema validate
    → Risk classify (Tier 0/1/2/3)
    → Tier 0/1: dispatch to mcpd directly (L1 fast path)
    → Tier 2/3: COW dry-run → HITL prompt (L6 inline) → execute
    → Result accumulates into step_results[]
    ↓
QB Replanner reads: (original_plan, step_results, remaining_steps)
    → decides: continue / amend plan / declare done
    ↓
Final response streamed from Replanner
```

### 4.2 Invariants preserved

- **INV-1**: QB never dispatches tools directly. Planner emits Plan JSON (schema-validated); Executor (PB→mcpd) dispatches. Raw QB text NEVER reaches PB.
- **INV-2**: Every step_i goes through schema validation before mcpd.
- **INV-3**: mcpd network isolation unchanged.
- **INV-5**: Sandbox around each mcpd call unchanged.
- **INV-6**: COW + HITL still fire for Tier 2/3 per-step.
- **INV-7**: Model integrity checks unchanged.
- **INV-8**: Audit log records the whole plan + each step's outcome as a hash-chained sequence.

### 4.3 Milestones

| # | What | Cost | Notes |
|---|---|---|---|
| M7.1 | Verifier tier-floor (L4) | 2h | `verifier.tier_floor` config; skip vote for Tier 0/1 |
| M7.2 | Tier-0 fast path (L1) | 1 day | Skip PB when Tier=0; require QB emits mcpd-compatible intent |
| M7.3 | Prompt engineering pass (L5) — includes F-60 nav-phrase mapping | 3-4 days | Rewrite qb_intent.txt with 20 few-shot; extend intent_corpus.json |
| M7.4 | Plan mode (F-62 fix) — LangGraph plan-and-execute | 2 days (with LangGraph) vs 1 wk custom | New plan.json schema; new graph node config |
| M7.5 | Session memory (F-62 follow-up context) | 2 days (with LangGraph state) vs 3 days custom | TurnMemory list bounded to 10; BP-8 redaction |
| M7.6 | Streaming everything (L2) | 2 days (with LangGraph `.astream_events`) vs 3 days custom | plan.step_token, mcpd.stdout, mcpd.stderr events |
| M7.7 | Terminal HITL end-to-end (F-59 + F-61) | 3 days | LangGraph interrupt node + inline HITL card |

Weeks 1-3 = implementation. Week 4 = live UTM sweep + tag decision.

---

## 5. Scope N — Option 4: LiteLLM + LangGraph runtime + SQLite checkpointer + LangSmith dev-only

### 5.1 Decision rationale (final)

User answered three questions:
1. Identity: **vendor-neutral is the whole point** → rules out Anthropic/OpenAI/Google SDKs.
2. Deps footprint: **200 MB is fine** → removes the deps objection to LangGraph runtime.
3. Investment: **speed to close F-59/F-60/F-61/F-62 + parity work** → favors runtime over pattern-only.

→ **Option 4: LiteLLM as backend abstraction + LangGraph runtime as orchestration engine.**

### 5.2 LiteLLM adoption (~1 day)

**Package:** [BerriAI/litellm](https://github.com/BerriAI/litellm) (Apache-2). Version pin `litellm~=1.x` (exact minor set at sprint start).

**What it replaces:**
- `dual-brain/controller/backends/gemini.py` (deprecated → delete after migration)
- `dual-brain/controller/backends/anthropic.py` (deprecated → delete)
- `dual-brain/controller/backends/openai.py` (deprecated → delete)

**What it becomes:**
- `dual-brain/controller/backends/litellm_backend.py` (~120 lines). Model ID string → provider (`"gemini/gemini-2.5-flash"`, `"anthropic/claude-sonnet-4-6"`, `"openai/gpt-5"`, `"ollama_chat/qwen-2.5-coder-1.5b"`).
- Wraps `litellm.completion(...)` and `litellm.acompletion(...)`.
- Fallback chain still lives in `fallback_backend.py` — LiteLLM handles per-provider, `fallback_backend.py` handles cross-provider.
- Streaming: uses LiteLLM's normalized ModelResponse chunk format.

**What it does NOT touch:**
- `dual-brain/controller/backends/local.py` (llama.cpp for PB) stays direct. INV-1: PB is local llama.cpp, never routed through cloud abstractions.

**Regression tests:**
- `test_litellm_backend.py` — mocked provider adapter; verifies OpenAI-format compatibility.
- `test_backend_fallback.py` (existing) still passes — fallback chain unchanged at Icebreaker level.

### 5.3 LangGraph adoption (~2 weeks integration + M7.4/M7.5/M7.6 features)

**Package:** [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) (MIT/Apache-2). Version pin `langgraph~=0.6.x` (exact set at sprint start).

**Companion package:** `langgraph-checkpoint-sqlite` for durable execution.

**The adapter file:** `dual-brain/controller/agent_graph.py` (~250 lines).

This file is the ONLY file that imports `langgraph` or `langchain_core`. Every other Icebreaker file talks to `AgentGraph` interface:

```python
class AgentGraph:
    def run(self, query: str, session: Session) -> Iterator[TurnEvent]: ...
    def resume(self, session_id: str) -> Iterator[TurnEvent]: ...
    def interrupt(self, session_id: str, decision: Decision) -> Iterator[TurnEvent]: ...
```

**Nodes in the graph:**
1. **Planner** — QB call via LiteLLM. Reads (query, session.history, shell_context). Emits PlanSchema JSON.
2. **StepExecutor** — For each step in the plan:
   - PB (local llama.cpp) grammar-decodes step_i → mcp_tool_call
   - Schema validate
   - Risk classify
   - Tier 2/3: interrupt node → HITL → resume
   - Tier 0/1: dispatch directly to mcpd
   - Accumulate result
3. **Replanner** — QB reviews accumulated results. Decides continue / amend / done.
4. **Responder** — Streams final response from Replanner output.

**Edges:**
- Planner → StepExecutor if plan valid, else FailSafe.
- StepExecutor → Replanner after all steps or after interrupt resume.
- Replanner → StepExecutor if amend, else Responder if done.
- Any node → FailSafe on schema/risk violation.

**Migration insurance:** ~150 lines of Icebreaker-owned wrapper. If LangGraph churns hard or we want to fork it out later, we replace one file.

### 5.4 LangSmith: dev builds ON, release ISOs OFF

**Two profiles:**

- `ICEBREAKER_PROFILE=dev` (dev builds, developer machines): LangSmith tracing enabled by default. `LANGSMITH_API_KEY` from local env / `.env`. Traces every graph turn. Enables agent-flow debugging, replay, eval dashboards.
- `ICEBREAKER_PROFILE=release` (shipped ISO, set by `cx-distro/build.sh` in the systemd unit env): LangSmith completely disabled. No API key wired. `controller/agent_graph.py` gates the LangSmith callback registration on the profile flag. Even if a user sets `LANGSMITH_API_KEY` in their env, the module does not initialize the tracer.

**Config surface:**
- `controller/config.py`: `observability.langsmith_enabled: bool` (default `False`; profile switch flips to `True` for dev).
- `controller/schemas/controller_config.json`: schema entry with `x-reload: restart`.

**CI gate G26 (new):**
- Release-profile ISO under live QEMU: assert no `smith.langchain.com` DNS lookup, no outbound TLS to LangSmith endpoints during a normal turn.
- Import-time assertion: `from controller.agent_graph import AgentGraph` in release profile with `LANGSMITH_API_KEY` set does NOT connect.

### 5.5 SQLite checkpointer for durable execution

**Path:** `~/.local/state/icebreaker/checkpoints.db` (respects `XDG_STATE_HOME` env if set).

**Config:**

```python
# controller/agent_graph.py
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string(
    os.path.expanduser("~/.local/state/icebreaker/checkpoints.db")
)
graph = graph_builder.compile(checkpointer=checkpointer)
```

**File permissions:** 0600 (user-only read/write). Directory 0700.

**Setup:** `v2.manifest` first-boot step creates the directory + sets perms. Subsequent daemon starts respect existing state.

**Encryption:** Rely on disk-level encryption if user has LUKS. No app-level encryption for v6.8 (avoid key-management complexity). If per-user encryption needed in v6.9, use `sqlcipher` with key derived from PAM login secret.

**Migration:** No auto-migration of checkpoints across daemon versions. Fresh DB for v6.8. v6.9 concerns.

### 5.6 What we do NOT touch

- mcpd (Rust, own sandbox) — unchanged.
- Audit hash-chain (INV-8) — unchanged.
- Landlock / Seccomp / COW — unchanged.
- Dual-brain isolation (INV-1) — LangGraph nodes = QB + PB + mcpd, NOT a single-agent loop.
- PB (local llama.cpp) — stays direct, never routed through cloud abstractions.

---

## 6. Sprint sequencing (v6.8, 4 weeks)

### Sprint 1 (week 1)

- **Day 1 (2h)** — M7.1 verifier tier-floor. Config + short-circuit + test.
- **Day 1-2 (~1 day)** — LiteLLM adapter. Migrate gemini/anthropic/openai backends. Local llama.cpp stays direct. Regression tests pass.
- **Day 3-5** — L1 Tier-0 fast path (1 day) + start LangGraph adapter design (`agent_graph.py`) (2 days).

### Sprint 2 (week 2)

- Land LangGraph adapter with QB Planner + StepExecutor + Replanner nodes.
- Migrate `main.py::run_turn_streaming` to route through `AgentGraph.run`.
- Full pytest suite passing (all existing 2333 + ~50 new tests).
- Days 4-5: M7.3 (prompt engineering pass). Includes F-60 fix (nav-phrase → fs.list mapping).

### Sprint 3 (week 3)

- M7.4 (Plan mode via LangGraph plan-and-execute node) — ~2 days for wrapper vs ~1 week custom
- M7.5 (session memory as LangGraph state) — ~2 days vs ~3 days custom
- M7.6 (L2 streaming via `.astream_events`) — ~2 days vs ~3 days custom
- Regression tests for compound intents + follow-up context

### Sprint 4 (week 4)

- M7.7 (terminal HITL wiring + F-59 source fix regression test + F-61 wire-up using LangGraph interrupt node) — ~3 days
- Live UTM sweep on running arm64 guest at `192.168.64.27`:
  - F-59: Control Center opens successfully
  - F-60: "take me to Downloads" resolves to fs.list ~/Downloads
  - F-61: Tier-3 delete shows inline HITL card + `y`/`n`/`Esc` all work
  - F-62 compound: "get my IP and save it to notes.txt" produces IP + file with IP
  - F-62 follow-up: turn 1 "what's my IP?" → turn 2 "save that to notes.txt" resolves to that IP
  - Tier-0 speed: `time list Downloads` returns < 500ms
  - Streaming: user watches CoT tokens stream, plan steps appear one-at-a-time, mcpd stdout live
- If all green: rebuild both arch ISOs. Run offline pytest + arm64 qemu-gate. Tag `v6.8` if clean.
- If any red: patch on same label (v6.8-rc2, etc). Do NOT rev to `v6.9`.

---

## 7. Files touched (v6.8 execution scope)

### New files

- `dual-brain/controller/agent_graph.py` — LangGraph adapter (~250 lines with LangSmith wiring + SQLite checkpointer)
- `dual-brain/controller/backends/litellm_backend.py` — LiteLLM adapter (~120 lines)
- `dual-brain/controller/schemas/plan.json` — PlanSchema
- `dual-brain/controller/prompts/qb_plan.txt` — Planner prompt
- `dual-brain/controller/tests/test_agent_graph_e2e.py`
- `dual-brain/controller/tests/test_langsmith_off_in_release.py`
- `dual-brain/controller/tests/test_sqlite_checkpointer.py`
- `dual-brain/controller/tests/test_litellm_backend.py`
- `dual-brain/controller/tests/test_plan_executor.py`
- `dual-brain/controller/tests/test_session_memory.py`
- `dual-brain/controller/tests/test_tier0_fast_path.py`
- `dual-brain/controller/tests/corpus/plan_corpus.json` (multi-step plan test fixtures)
- `dual-brain/terminal/tests/test_hitl_e2e.py`
- `dual-brain/gui/control/pages/tests/test_keys_page.py` (F-59 regression lock)

### Modified files

- `dual-brain/controller/config.py` — `verifier.tier_floor`, `plan.enabled`, `session.history_turns`, `observability.langsmith_enabled`, `checkpointer.path`
- `dual-brain/controller/schemas/controller_config.json` — schema for above
- `dual-brain/controller/main.py` — Tier-0 fast path branch; route to `AgentGraph.run`; session history read/write
- `dual-brain/controller/session.py` — add `history: list[TurnMemory]`; redaction hook (BP-8)
- `dual-brain/controller/verifier.py` — skip votes below `tier_floor`
- `dual-brain/controller/prompts/qb_intent.txt` — M7.3 rewrite (nav-phrase mapping, 20 few-shot examples)
- `dual-brain/terminal/app.py` — F-61 wiring, plan-event dispatch, streaming stdout
- `dual-brain/terminal/companion.py` — stepped-plan render
- `dual-brain/gui/control/pages/keys_page.py` — F-59 source fix (already applied on branch)
- `dual-brain/controller/tests/corpus/intent_corpus.json` — M7.3 expanded rows (nav-phrase, compound)
- `dual-brain/pyproject.toml` — add `litellm~=1.x`, `langgraph~=0.6.x`, `langgraph-checkpoint-sqlite`
- `cx-distro/build.sh` — set `ICEBREAKER_PROFILE=release` in shipped systemd unit env
- `incremental/versions/v2.manifest` — first-boot: `mkdir -p ~/.local/state/icebreaker` + `chmod 0700`; F-51 markers for `agent_graph.py:LiteLLMBackend` + `agent_graph.py:SqliteSaver`
- `incremental/build/smoke-gate.sh` — new check: LangSmith env not set + endpoint not reachable in release ISO
- `dual-brain/controller/ci.sh` — new gate G26 (release-profile-no-telemetry)
- `incremental/GROUND_TRUTH.md § 7` — F-59/F-60/F-61/F-62 entries with regression-lock annotations
- `docs/RELEASE_NOTES_v6.8.md` — new (drafted end of cycle)

### Deleted files (after LiteLLM migration lands)

- `dual-brain/controller/backends/gemini.py`
- `dual-brain/controller/backends/anthropic.py`
- `dual-brain/controller/backends/openai.py`

`local.py` stays (PB llama.cpp direct call).

---

## 8. Verification for v6.8 gate

### Offline

- `pytest dual-brain/ --timeout=90 -q` — new tests green, existing 2333 unchanged.
- `bash ci.sh` — all gates green including new G25 (plan-schema validation) + G26 (release-profile-no-telemetry).
- Scope B G23 (no-new-hardcoded-knobs) still green.
- Scope A test_no_silent_swallow.py still green.

### Live (guest UTM sweep — this is the discipline v6.7 skipped)

- F-59 live-verification: Control Center opens without error on fresh boot.
- F-60 live: "take me to Downloads" resolves to fs.list ~/Downloads. Similarly "go to /tmp", "cd ~/Documents".
- F-61 live: Tier-3 delete intent produces inline HITL card. Press `y` → executes; press `n` → deny; press `Esc` → cancel. Timeout still defaults to deny after 30s.
- F-62 compound live: `"get my IP and save it to notes.txt"` produces IP + notes.txt containing IP.
- F-62 follow-up live: turn 1 `"what is my IP?"` returns IP; turn 2 `"save that to notes.txt"` writes IP without asking again.
- Tier-0 speed: `time list Downloads` on the guest returns < 500ms.
- Streaming test: user watches CoT tokens stream, plan steps appear as they complete, mcpd stdout live.
- G24 sweep (from Scope F): all rows in intent_corpus.json pass on the guest.
- G26 release-telemetry gate: `tcpdump -i any host smith.langchain.com` shows zero packets during a full turn.

Every red = patch on same label. Do NOT rev.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Plan hallucination (planner emits 15-step plan for 1-step task) | Cap plan length at N=5 in config; replanner can extend; include few-shot examples of single-step plans |
| Context poisoning (attacker injects malicious `result_summary` into session memory via QB output) | History stores `(intent_action, redacted_result_bytes_hash)` not free text. Structured, not natural language. |
| Fast-path regression (L1 accidentally covers Tier 2 through misclassification) | Runtime assertion in `_dispatch_direct()`: `assert tier <= 1`. Panic on violation. |
| Streaming reveals unsafe info (mcpd stderr contains a key) | BP-8 redactor runs on streamed lines before UI display. |
| Session memory contradicts INV-1 (planner reads history that includes rich PB output) | History stores summary + hash, not raw PB output text. |
| LangGraph API churn on version bump | Version pin in pyproject.toml. Adapter layer isolates. Test suite catches breaking changes. |
| LiteLLM CVE (their proxy mode has had 5+ CVEs in 2024) | We use LiteLLM SDK mode, NOT proxy mode. Pin version. Audit CVE list before every ISO cycle. |
| LangSmith telemetry accidentally leaks in release | CI gate G26 asserts no `smith.langchain.com` traffic during full turn. Test in G26 imports agent_graph with `LANGSMITH_API_KEY` set + `PROFILE=release` and asserts no connection. |
| SQLite checkpoint corruption after crash | LangGraph's SqliteSaver uses transactions. On corruption: drop DB, restart. Loss is bounded to unfinished turn. |
| SQLite grows unbounded | Retention policy: prune checkpoints older than 30 days. Cronjob or startup task. |

---

## 10. Non-goals for v6.8

- No `v1.0-rc1` tag (still `v6.8`; v1.0 comes after v6.8 clears live UTM sweep clean).
- No local QB (L3 deferred to v7.0).
- No autocomplete UI (L7 deferred).
- No LangGraph external adapter for other agent frameworks (Phase 8).
- No agent-spawn / sub-agent primitives (Phase 8).
- No repo-map analog (Icebreaker operates on OS state, not source).
- No LangSmith telemetry in release ISO. Ever.
- No single-agent-loop refactor. Dual-brain isolation preserved via LangGraph nodes.
- No auto-migration of checkpoints across daemon versions (v6.9 concern).
- No SQLite over network.
- No `langchain-core` primitives leaking into Icebreaker's public API surface (adapter wraps all).

---

## 11. Sources (research 2026-07-13)

### Claude Agent SDK / Claude Code architecture

- [How the agent loop works — Claude Code Docs](https://code.claude.com/docs/en/agent-sdk/agent-loop)
- [Claude Agent SDK: Agent Loops, Tool Calls, and Multi-Step Workflows — Augment Code](https://www.augmentcode.com/guides/claude-agent-sdk-agent-loops-tool-calls)
- [Claude Code Architecture Explained: Agent Loop, Tool System, and Permission Model — DEV](https://dev.to/brooks_wilson_36fbefbbae4/claude-code-architecture-explained-agent-loop-tool-system-and-permission-model-rust-rewrite-41b2)
- [Inside the Agentic Loop: A Deep Technical Dive — DEV](https://dev.to/monuminu/inside-the-agentic-loop-a-deep-technical-dive-into-ai-coding-agents-claude-code-and-the-4pnf)
- [Making Claude Code more secure and autonomous with sandboxing — Anthropic](https://www.anthropic.com/engineering/claude-code-sandboxing)
- [Self-hosted sandboxes — Claude Platform Docs](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes)
- [Securely deploying AI agents — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/secure-deployment)

### Cursor 2.0

- [Cursor 2.0: Agent-First Architecture Complete Guide](https://www.digitalapplied.com/blog/cursor-2-0-agent-first-architecture-guide)
- [Cursor Agent Best Practices 2026: Multi-File Edits, Parallel Agents & Rules](https://baeseokjae.github.io/posts/cursor-agent-best-practices-2026/)
- [How to make Cursor an Agent that Never Forgets — DEV](https://dev.to/getcore/how-to-make-cursor-an-agent-that-never-forgets-and-10x-your-productivity-108a)
- [Context: Mastering Project Switching in Cursor's AI Memory System — egghead.io](https://egghead.io/context-mastering-project-switching-in-cursors-ai-memory-system~fiyzv)

### Aider

- [Repository map — Aider](https://aider.chat/docs/repomap.html)
- [Aider Deep Dive: The CLI Agentic Coding Tutorial 2026](https://www.digitalapplied.com/blog/aider-deep-dive-cli-agentic-coding-tutorial-2026)

### LangGraph & agent patterns

- [langchain-ai/langgraph — GitHub](https://github.com/langchain-ai/langgraph)
- [Plan-and-Execute Agents — LangChain blog](https://www.langchain.com/blog/planning-agents)
- [ReAct vs Plan-and-Execute vs ReWOO vs Reflexion](https://theaiengineer.substack.com/p/the-4-single-agent-patterns)
- [Architecting Resilient LLM Agents: A Guide to Secure Plan-then-Execute Implementations (arxiv 2509.08646)](https://arxiv.org/pdf/2509.08646)
- [AI Agents with LangGraph Agentic Workflow Pattern and Task Decomposition — Medium](https://gyliu513.medium.com/ai-agents-with-langgraph-agentic-workflow-pattern-36d867dc7b68)
- [Choosing an agent framework — Speakeasy](https://www.speakeasy.com/blog/ai-agent-framework-comparison)
- [The best open source frameworks for building AI agents in 2026 — Firecrawl](https://www.firecrawl.dev/blog/best-open-source-agent-frameworks)
- [AI Agent Frameworks Compared: LangGraph vs CrewAI vs AutoGen — pecollective](https://pecollective.com/blog/ai-agent-frameworks-compared/)

### LiteLLM

- [BerriAI/litellm — GitHub](https://github.com/BerriAI/litellm)
- [Streaming + Async — LiteLLM docs](https://docs.litellm.ai/docs/completion/stream)
- [LiteLLM Multi-Provider Support — DeepWiki](https://deepwiki.com/openai/openai-agents-python/7.4-litellm-multi-provider-support)

### MCP ecosystem

- [modelcontextprotocol/servers — GitHub](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)
- [MCP Server Ecosystem Reference 2026](https://hidekazu-konishi.com/entry/mcp_server_ecosystem_reference_2026.html)
- [Best MCP Servers 2026 — Shareuhack](https://www.shareuhack.com/en/posts/best-mcp-servers-guide-2026)
- [The MCP Ecosystem in 2026 — mcp-conference.com](https://www.mcp-conference.com/resources/mcp-ecosystem-2026)

### Memory & context

- [Enhancing Self-Made Agent with Memory — Pondhouse Data](https://www.pondhouse-data.com/blog/ai-agents-memory)
- [Look Back to Reason Forward: Revisitable Memory for Long-Context LLM Agents (arxiv 2509.23040)](https://arxiv.org/pdf/2509.23040)

---

## 12. Handoff notes for future sessions

- The v6.7 build story (all 17 build-time failures + fixes) lives in `cx-distro/BUILD_REQUIREMENTS.md`. Read that before any ISO rebuild.
- Preflight infrastructure: `cx-distro/preflight.sh` + `cx-distro/rebuild/rebuild-v67.sh`. Run preflight BEFORE any rebuild. Rebuild is idempotent, single-shot; never use `--skip-to=N` for N > 0.
- The GCP VM `icebreaker-phase2-vm` in project `project-12486d7e-4046-45bd-8b4`, zone `us-west4-b`, is the build host. STOPS after ~14 hours of idle (cost management). Wake with `gcloud compute instances start`.
- The UTM guest at `192.168.64.27` (icebreaker@icebreaker password) is the live-verification host. G24 corpus sweeps run against it via ssh.
- Every bug added to GROUND_TRUTH.md § 7 needs an R14 regression lock (named test or corpus row). This is the discipline that keeps Icebreaker from re-shipping the same bug.
- Every ISO cycle MUST include a live UTM sweep of every F-xx before tag. v6.7 shipped without this and F-59/F-60/F-61/F-62 escaped. Do NOT skip the sweep.
- The Phase 6 completion plan (Scopes A-J) lives at `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md` (ephemeral to Claude Code sessions but referenced here).

---

## 13. Best-practices findings (research 2026-07-13, MUST-read before coding)

### 13.1 LangGraph SqliteSaver — CRITICAL security finding

**[Check Point Research 2026: "From SQLi to RCE — Exploiting LangGraph's Checkpointer"](https://research.checkpoint.com/2026/from-sqli-to-rce-exploiting-langgraphs-checkpointer/)** describes a real CVE where a compromised checkpointer database could lead to Remote Code Execution through msgpack deserialization of arbitrary Python objects.

**Required mitigations** (Icebreaker MUST apply both):

1. **Set `LANGGRAPH_STRICT_MSGPACK=true` in the systemd unit env for the controller daemon**. This restricts checkpoint deserialization to known-safe primitive types (str, int, list, dict, bool, None, bytes). Any attempt to deserialize an arbitrary Python class is rejected.

2. **Pass an explicit `allowed_msgpack_modules` allowlist when creating the checkpointer**. Even with strict-msgpack, we enumerate the module names that ARE allowed (our own state dataclasses). Anything else raises.

```python
from langgraph.checkpoint.sqlite import SqliteSaver

# Icebreaker pattern: strict mode + explicit allowlist
checkpointer = SqliteSaver.from_conn_string(
    conn_string=os.path.expanduser("~/.local/state/icebreaker/checkpoints.db"),
    allowed_msgpack_modules=[
        "controller.agent_graph",  # our state module
        "controller.session",       # TurnMemory, ShellContext
        "controller.intent_schema",  # Intent
    ],
)
```

**Also required:**

- **File permissions 0600 (user-only)** — already planned in N.5.
- **Directory 0700** — already planned.
- **Sqlite pragma settings**: `PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;` at connection open. Reduces corruption risk.
- **Rotation policy**: prune checkpoint rows older than 30 days on daemon startup. Bounded DB growth.

### 13.2 LangGraph interrupt — HITL gotcha

**[Docs by LangChain — Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)**: "When execution resumes, the runtime restarts the entire node from the beginning — it does not resume from the exact line where interrupt was called. Any code that ran before the interrupt will execute again."

**Icebreaker implication**: We CANNOT put an `interrupt()` inside a node that already dispatched an mcpd tool call. The mcpd call would fire again on resume. Structure MUST be:

```
Node A: RiskAssessor — classifies tier, if Tier ≥ 2 → transition to Node B
Node B: HitlGate — calls interrupt() with COW dry-run + intent details
Node C: McpdDispatcher — actually executes (only reached if Node B's resume was APPROVE)
```

Never merge B and C. Non-negotiable.

**Also**: keep the interrupt() call last in Node B. Any state updates BEFORE the interrupt() will replay on resume. Any state updates that CANNOT be idempotently replayed (writing to disk, sending Slack, etc.) MUST be in Node C (post-resume).

### 13.3 LangGraph state schema — keep it small

**Best practice from research**: "Put in the minimum that conditional edges need to route correctly, plus the accumulating artifacts the LLM needs to reason. Use external references, not content."

**Icebreaker state schema (draft):**

```python
from typing import TypedDict

class GraphState(TypedDict):
    # Routing minimum
    tier: int                     # 0/1/2/3 — drives edges
    step_index: int
    total_steps: int
    intent_valid: bool

    # Accumulating artifacts — REFERENCES, not content
    plan_id: str                  # UUID; the plan lives in session, not state
    step_result_hashes: list[str]  # SHA-256 of each step's result
    session_id: str
    turn_id: str

    # HITL surface
    hitl_pending: bool
    hitl_prompt_key: str          # not the raw prompt; key to look up in session
    hitl_decision: Optional[str]  # None | "approve" | "deny" | "timeout"

    # Streaming surface (small)
    last_event_seq: int
```

**What NOT to store**: raw QB output, raw PB grammar tokens, raw mcpd stdout, raw user query. All those live in the session (audit-logged, redacted) and are referenced by hash / key.

**Rationale**: keeps checkpoints kilobytes not megabytes; prevents PB output from leaking through the graph state into audit trails uncontrolled; supports fast checkpointing.

### 13.4 LiteLLM — production hardening

**Findings from research:**

- **Pin `litellm >= 1.44`** — the v1.44+ "context-aware fallback" feature auto-strips incompatible params (e.g. `response_format` when falling back from an OpenAI model to a Gemini model). Without it, fallbacks silently fail.
- **Known bug**: streaming fallback for 429 (rate-limit) errors is broken in some versions (`GitHub issue #22296`). Icebreaker workaround: catch `RateLimitError` at the LiteLLM adapter layer and re-invoke via async retry loop.
- **Known bug**: sync streaming does not fallback. Icebreaker fix: use `litellm.acompletion(stream=True)` (async) inside the adapter, expose sync interface via `asyncio.run` boundary.
- **Retries + fallbacks stack**: retry same model 2x for transient errors (429, network), THEN move to fallback chain for hard errors. Configure both.
- **In-process only**: `litellm.completion` `success_callback` fires for in-process calls. Icebreaker never uses LiteLLM's proxy mode; SDK mode only.

**Icebreaker LiteLLM adapter shape:**

```python
# controller/backends/litellm_backend.py
import litellm
import asyncio
from litellm import RateLimitError

class LiteLLMBackend:
    def __init__(self, model_id: str, retry_count: int = 2, timeout_s: int = 30):
        self.model_id = model_id
        self.retry_count = retry_count
        self.timeout_s = timeout_s

    async def _complete_async(self, messages, stream=False, **kwargs):
        for attempt in range(self.retry_count + 1):
            try:
                return await litellm.acompletion(
                    model=self.model_id,
                    messages=messages,
                    stream=stream,
                    timeout=self.timeout_s,
                    **kwargs,
                )
            except RateLimitError as e:
                if attempt == self.retry_count:
                    raise
                await asyncio.sleep(0.5 * (2 ** attempt))  # exponential backoff

    def complete(self, messages, stream=False, **kwargs):
        return asyncio.run(self._complete_async(messages, stream=stream, **kwargs))
```

### 13.5 LangSmith opt-out — the ONLY safe pattern

**Finding**: No single documented environment variable definitively disables LangSmith across all versions. The safest pattern is: **never import the `langsmith` module in release profile**.

**Icebreaker pattern:**

```python
# controller/agent_graph.py — TOP OF FILE
import os
_PROFILE = os.environ.get("ICEBREAKER_PROFILE", "release")

# Import langsmith ONLY in dev profile. Release ISOs never resolve this module.
if _PROFILE == "dev":
    from langsmith import Client as LangSmithClient
    from langgraph.callbacks import LangSmithCallbackHandler
    _LANGSMITH_AVAILABLE = True
else:
    _LANGSMITH_AVAILABLE = False
    LangSmithClient = None
    LangSmithCallbackHandler = None
```

Belt-and-braces on release build: `cx-distro/build.sh` writes systemd unit env:
```
Environment=ICEBREAKER_PROFILE=release
Environment=LANGSMITH_TRACING=false
Environment=LANGCHAIN_TRACING_V2=false
```

**CI gate G26 asserts:**
1. Grep for `import langsmith` in the runtime path — must be conditional on `_PROFILE == "dev"`.
2. Live QEMU test: start daemon under `ICEBREAKER_PROFILE=release` with `LANGSMITH_API_KEY=fake-key` in env. Run a full turn. `tcpdump -i any host smith.langchain.com` returns zero packets.

### 13.6 Async / threading — the impedance mismatch

**Icebreaker controller is threading-based.** LangGraph is async-first.

**Pattern** (already noted in N.9): `AgentGraph.run()` is sync-wrapping-async. Internal graph execution uses `asyncio.run()` at the adapter boundary. The existing daemon-side threading model does NOT change.

```python
class AgentGraph:
    def run(self, query: str, session: Session) -> Iterator[TurnEvent]:
        """Sync interface used by daemon threads."""
        loop = asyncio.new_event_loop()
        try:
            async_gen = self._run_async(query, session)
            while True:
                try:
                    event = loop.run_until_complete(async_gen.__anext__())
                    yield event
                except StopAsyncIteration:
                    break
        finally:
            loop.close()

    async def _run_async(self, query, session):
        async for event in self._graph.astream_events({...}, config={...}):
            yield event
```

This isolates async to the adapter. The controller stays sync/threaded.

### 13.7 State-schema deserialization safety

Even with strict-msgpack + allowlist, **NEVER trust checkpoint data as if it were internal**. Every load path revalidates:

```python
def load_state(session_id: str) -> GraphState:
    raw = checkpointer.get(config={"configurable": {"thread_id": session_id}})
    if raw is None:
        return _default_state(session_id)
    # Re-validate every field. Reject on any anomaly.
    validated = _state_schema.validate(raw)  # jsonschema.validate
    return validated
```

This is INV-2 for checkpoint data.

### 13.8 pyproject.toml pin specifications

Exact pins for v6.8:

```toml
[project]
dependencies = [
    "litellm>=1.44,<2.0",
    "langgraph>=0.6,<0.7",
    "langgraph-checkpoint-sqlite>=1.0,<2.0",
    # Optional; dev-only:
    # "langsmith>=0.3,<1.0",  # NOT ship in release ISO
]

[project.optional-dependencies]
dev = [
    "langsmith>=0.3,<1.0",  # only for dev builds
]
```

`cx-distro/build.sh` installs without `[dev]` extra so LangSmith isn't in the venv on release.

### 13.9 Sources for Section 13

- [From SQLi to RCE — Exploiting LangGraph's Checkpointer (Check Point Research 2026)](https://research.checkpoint.com/2026/from-sqli-to-rce-exploiting-langgraphs-checkpointer/)
- [LangGraph SqliteSaver reference](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/SqliteSaver)
- [Persistence — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Interrupts — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Making it easier to build human-in-the-loop agents with interrupt (LangChain blog)](https://www.langchain.com/blog/making-it-easier-to-build-human-in-the-loop-agents-with-interrupt)
- [LiteLLM Fallback Configuration Guide (Markaicode)](https://markaicode.com/tutorial/litellm-fallback-configuration/)
- [LiteLLM Python SDK docs](https://docs.litellm.ai/docs/tutorials/python_sdk)
- [LiteLLM streaming fallback issue #22296](https://github.com/BerriAI/litellm/issues/22296)
- [LangGraph State Management: Checkpointing & Recovery — ActiveWizards](https://activewizards.com/blog/langgraph-state-management-checkpointing-recovery-and-the-persistence-layer-decision/)
- [How do I disable or toggle LangSmith tracing during execution](https://support.langchain.com/articles/2491399069-how-do-i-disable-or-toggle-langsmith-tracing-during-execution)

---

## 14. Control Center configurability — MANDATORY for every knob

**User requirement (2026-07-13 08:15 ET):** *"most of the code is configurable based on a tab in control center so that we can adjust the behavior later, hardcoding it might make the process very hard to revise / correct"*

**Rule.** Every field added under Section 7 (files touched) that governs runtime behavior gets:

1. **A field in `controller/config.py` dataclass** (typed, with default)
2. **A schema entry in `controller/schemas/controller_config.json`** (with `x-reload: "hot" | "restart"`, `minimum`, `maximum`, `description`)
3. **A GUI surface in Control Center** (page + widget)
4. **A test that the GUI round-trips the value through TOML**

No exceptions. Following the pattern Scope B established (schema → dataclass → GUI → test).

### 14.1 New Control Center page: **Agent** (new tab)

Rationale: LangGraph settings + Plan-mode settings + Session-memory settings + Verifier-tier-floor + Tier-0 fast path all shape the agent's behavior. They belong on their own page.

**Widgets on the Agent page:**

| Widget type | Config field | Description | Reload |
|---|---|---|---|
| `Adw.ExpanderRow "Plan & Execute"` | | | |
| ↳ `Adw.SwitchRow` | `plan.enabled` | Plan-and-execute for compound queries (default: on) | restart |
| ↳ `Adw.SpinRow` | `plan.max_steps` | Maximum plan length (default: 5, range 1-10) | hot |
| ↳ `Adw.SwitchRow` | `plan.replan_on_failure` | Retry with amended plan if a step fails (default: on) | hot |
| `Adw.ExpanderRow "Session Memory"` | | | |
| ↳ `Adw.SpinRow` | `session.history_turns` | Turns kept in memory (0-50, default 10) | hot |
| ↳ `Adw.SwitchRow` | `session.redact_secrets` | Auto-redact API keys / passwords in history (default: on) | hot |
| `Adw.ExpanderRow "Performance"` | | | |
| ↳ `Adw.SwitchRow` | `run.tier0_fast_path` | Skip PB for read-only intents (default: on) | restart |
| ↳ `Adw.SpinRow` | `verifier.tier_floor` | Only verify at Tier ≥ N (0-3, default 2) | restart |
| `Adw.ExpanderRow "Durable Execution"` | | | |
| ↳ `Adw.EntryRow` | `checkpointer.path` | SQLite checkpoint DB path (default `~/.local/state/icebreaker/checkpoints.db`) | restart |
| ↳ `Adw.SpinRow` | `checkpointer.retention_days` | Prune checkpoints older than N days (default 30, 1-365) | restart |
| `Adw.ExpanderRow "Observability (dev)"` | | | |
| ↳ `Adw.SwitchRow` | `observability.langsmith_enabled` | LangSmith tracing (dev builds only; disabled + hidden on release ISO) | restart |

Row visibility rule: `observability.*` expander is HIDDEN when `ICEBREAKER_PROFILE=release` (checked via env at page instantiation).

### 14.2 New Control Center page: **Providers** (new tab, extends "Models")

The existing Models page becomes the QB/PB selection UI. A new "Providers" page captures LiteLLM-specific settings so provider adds don't clutter Models.

**Widgets:**

| Widget type | Config field | Description | Reload |
|---|---|---|---|
| `Adw.EntryRow` (per provider) | `providers.<name>.api_key_env` | Env var name holding the key (e.g. `GEMINI_API_KEY`) — NOT the key itself (BP-8) | restart |
| `Adw.SpinRow` | `providers.<name>.timeout_s` | Per-provider request timeout (default 30, 5-300) | hot |
| `Adw.SpinRow` | `providers.<name>.retry_count` | Retries before falling back (default 2, 0-5) | hot |
| `Adw.EntryRow` | `providers.<name>.base_url` | Custom endpoint URL (empty = provider default). For self-hosted proxies. | restart |
| `Adw.ComboRow` | `providers.<name>.fallback_to` | Next provider in fallback chain (dropdown of registered providers) | hot |

Each provider row is created dynamically from `catalogue.toml`'s provider registry — Adding a provider is a `catalogue.toml` edit, GUI updates on next load (Scope B/C pattern).

### 14.3 Existing pages: extend

**Behavior page**: extend the "Verifier voting" group (Scope B) with:
- `Adw.SwitchRow` — `verifier.parallel_dispatch` — Run votes in parallel threads (default: on)
- Already-present votes/require/parallel/qb_max_retries stay.

**Limits page** (Scope B B4): extend with:
- `Adw.SpinRow` — `run.turn_timeout_seconds` (Scope B B1 field, already added; keep)
- `Adw.SpinRow` — `plan.max_step_timeout_seconds` — per-step timeout inside plan (default 60, 5-600) | restart

### 14.4 Rule reinforcement: no-new-knobs lint stays

Scope B's G23 (no-new-hardcoded-knobs) gate stays. Every new numeric constant in v6.8 code either:
- Goes into config (preferred), OR
- Is added to `dual-brain/tools/lint/knobs_allowlist.toml` with a `reason` line

CI blocks the PR otherwise.

### 14.5 Backward compatibility

Every new field ships with a default. Loading a v6.7 config into v6.8 controller MUST work (all new fields resolve to defaults, no crash, no warning). Test: `test_config_backward_compat.py` — extend the existing Scope B test with a v6.7-shaped fixture.

### 14.6 Schema-driven bounds

Every SpinRow reads its min/max from the JSON schema via `gui/control/schema_reader.py` (Scope B pattern). Never hardcode bounds in the widget constructor.

### 14.7 Test matrix

For each new field added in v6.8:

| Test | File | What it asserts |
|---|---|---|
| Backward-compat | `test_config_backward_compat.py` | Field resolves to documented default when absent from TOML |
| Round-trip | `test_pages_smoke.py::test_agent_page_roundtrip` | GUI edit → save → reload TOML → same value |
| Schema-widget alignment | `test_schema_reader.py::test_agent_bounds` | Widget bounds match schema bounds |
| No-new-knobs | Already covered by G23 | Custom AST checker fails on new module-level numeric constants |

Every one of these is required per field. Do not skip.

### 14.8 GUI implementation reuse

- All widgets use `Adw.*` (LibAdwaita 1.3+) per Scope B pattern.
- Cross-field validators (e.g. `plan.max_steps ≥ 1 AND plan.enabled=true` OR `plan.enabled=false`) surface via inline warning rows (per Scope B pattern).
- `Apply` button disabled while any warning is live.

### 14.9 Files touched for Section 14

New:
- `dual-brain/gui/control/pages/agent_page.py` (Agent tab)
- `dual-brain/gui/control/pages/providers_page.py` (Providers tab)
- `dual-brain/gui/control/pages/tests/test_agent_page.py`
- `dual-brain/gui/control/pages/tests/test_providers_page.py`

Modified:
- `dual-brain/gui/control/window.py` — register two new pages
- `dual-brain/gui/control/pages/behavior_page.py` — add `verifier.parallel_dispatch` row
- `dual-brain/gui/control/pages/limits_page.py` — add `plan.max_step_timeout_seconds` row

All backed by controller_config.json schema entries + config.py dataclasses.

---

## 15. Data-format contracts (exact type signatures — implement against these)

**Purpose.** Every input, output, and message format we depend on. Copy these into code as authoritative. If the API surface changes on a version bump, this section is the diff target.

### 15.1 GraphState TypedDict (LangGraph state schema)

**Location:** `dual-brain/controller/agent_graph.py`

```python
from typing import TypedDict, Annotated, Optional, Literal
from operator import add

# Reducer helpers
def _last_write_wins(a, b): return b
def _append(a: list, b: list) -> list: return a + b

class GraphState(TypedDict, total=False):
    # ── Routing minimum (drives conditional edges) ────────────────────────
    tier: Annotated[int, _last_write_wins]                    # 0/1/2/3
    step_index: Annotated[int, _last_write_wins]              # 0-based
    total_steps: Annotated[int, _last_write_wins]
    intent_valid: Annotated[bool, _last_write_wins]
    plan_valid: Annotated[bool, _last_write_wins]

    # ── Accumulating artifacts (references only, NOT content) ─────────────
    plan_id: Annotated[str, _last_write_wins]                 # UUID
    step_result_hashes: Annotated[list[str], _append]         # SHA-256 per step
    session_id: Annotated[str, _last_write_wins]
    turn_id: Annotated[str, _last_write_wins]

    # ── HITL surface ──────────────────────────────────────────────────────
    hitl_pending: Annotated[bool, _last_write_wins]
    hitl_prompt_key: Annotated[str, _last_write_wins]         # lookup key
    hitl_decision: Annotated[Optional[Literal["approve", "deny", "timeout", None]], _last_write_wins]

    # ── Streaming surface (kept small) ────────────────────────────────────
    last_event_seq: Annotated[int, _last_write_wins]

    # ── Terminal state markers ────────────────────────────────────────────
    completed: Annotated[bool, _last_write_wins]
    error_key: Annotated[Optional[str], _last_write_wins]      # lookup in session

# Sizes: this TypedDict serializes to ~500 bytes per checkpoint. Verified acceptable per §13.3.
```

**Rule**: fields NEVER contain raw QB text, raw PB grammar tokens, raw mcpd stdout, or user-facing strings. Content lives in `session` (audit-logged, redacted), referenced here by hash/key/UUID.

### 15.2 Node function contract

```python
# All nodes follow this signature
def planner_node(state: GraphState) -> dict:
    """Return partial state update. LangGraph reducer merges."""
    # ... call LiteLLM
    return {
        "plan_id": new_uuid,
        "total_steps": len(plan),
        "plan_valid": True,
    }

# Nodes with side effects (post-HITL resume) MUST be idempotent because
# LangGraph re-runs the node from the top on resume. See §13.2.
```

**Idempotency contract:**
- Planner node: idempotent (reads QB, no external write until Executor).
- Executor node: NOT idempotent — makes mcpd calls that mutate state. MUST come AFTER HitlGate on Tier 2/3 path.
- HitlGate node: idempotent — pure `interrupt()` call.
- Replanner node: idempotent (reads plan + results, returns amended plan).
- Responder node: idempotent (streams from session, no external write).

### 15.3 Conditional edge function contract

```python
def route_after_planner(state: GraphState) -> str:
    """Return the name of the next node."""
    if not state.get("plan_valid"):
        return "fail_safe"
    return "executor"

def route_after_executor(state: GraphState) -> str:
    if state.get("hitl_pending"):
        return "hitl_gate"
    if state.get("step_index") >= state.get("total_steps"):
        return "replanner"
    return "executor"
```

**Rule**: conditional edge functions return `str` (next node name). Never return a state update — that's what nodes do.

### 15.4 astream_events output shape

```python
# Each event yielded by graph.astream_events(...)
{
    "event": str,           # e.g. "on_chain_start", "on_chat_model_stream", "on_tool_end"
    "name": str,            # node name or model name
    "run_id": str,          # UUID for this event
    "tags": list[str],      # optional
    "metadata": {
        "langgraph_node": str,      # node that emitted this event
        "langgraph_step": int,      # step index within run
        "thread_id": str,           # session_id
        # ... plus other run metadata
    },
    "data": {
        # varies by event type
        # on_chat_model_stream: {"chunk": AIMessageChunk}
        # on_chain_end: {"input": ..., "output": ...}
        # on_tool_start: {"input": {...}}
        # on_tool_end: {"output": ...}
    },
}
```

**Icebreaker use**: adapter subscribes to `on_chain_start`, `on_chain_end`, `on_chat_model_stream`, `on_tool_start`, `on_tool_end` events and translates to `TurnEvent` (see 15.10).

### 15.5 Stream modes we use

- `astream_events(version="v2")` — token-level streaming (v2 is stable).
- `astream(stream_mode="updates")` — one dict per node completion, containing only that node's state delta.
- `astream(stream_mode="values")` — full state after each step.
- `stream_mode=["updates", "custom"]` — combined mode when a node emits custom events via `get_stream_writer()`.

Icebreaker default: `astream_events(version="v2")` for the fine-grained view. Fallback to `astream(stream_mode="updates")` for coarse events.

### 15.6 LiteLLM `completion()` input contract

```python
from litellm import acompletion, completion

response = await acompletion(
    model="gemini/gemini-2.5-flash",           # provider/model_id
    messages=[
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},  # for tool loops
        {"role": "tool", "content": "...", "tool_call_id": "abc"},
    ],
    tools=[
        {
            "type": "function",
            "function": {
                "name": "fs.list",
                "description": "List directory contents",
                "parameters": {         # JSON Schema
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"],
                }
            }
        }
    ],
    response_format={"type": "json_object"},   # optional; for JSON-only output
    stream=True,
    stream_options={"include_usage": True},
    temperature=0.0,
    timeout=30,
    max_tokens=2048,
    # api_key from LITELLM_MASTER_KEY or per-provider env
)
```

### 15.7 LiteLLM `ModelResponse` (non-streaming) shape

```python
response = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gemini/gemini-2.5-flash",
    "choices": [
        {
            "finish_reason": "tool_calls" | "stop" | "length" | "content_filter",
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "..." | None,       # None when tool_calls present
                "tool_calls": [
                    {
                        "id": "call_xyz",
                        "type": "function",
                        "function": {
                            "name": "fs.list",
                            "arguments": '{"path": "/home"}',   # JSON string
                        }
                    }
                ] | None,
            }
        }
    ],
    "usage": {
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
    },
    "system_fingerprint": "..." | None,
}

# Access pattern
content = response.choices[0].message.content
tool_calls = response.choices[0].message.tool_calls
finish_reason = response.choices[0].finish_reason
```

### 15.8 LiteLLM `ModelResponseStream` (streaming) chunk shape

```python
# Each chunk yielded by `async for chunk in await acompletion(..., stream=True)`
chunk = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion.chunk",
    "created": 1700000000,
    "model": "gemini/gemini-2.5-flash",
    "choices": [
        {
            "finish_reason": None | "stop" | "tool_calls",   # None until last chunk
            "index": 0,
            "delta": {
                "role": "assistant" | None,      # only present in FIRST chunk
                "content": "partial text..." | None,
                "tool_calls": [
                    {
                        "index": 0,               # which tool_call in the sequence
                        "id": "call_xyz" | None,  # only present in first chunk for this tool
                        "type": "function" | None,
                        "function": {
                            "name": "fs.list" | None,     # only in first chunk for this tool
                            "arguments": '{"pa' | ...     # PARTIAL — must accumulate
                        }
                    }
                ] | None,
            }
        }
    ],
    "system_fingerprint": None,
}

# If stream_options={"include_usage": True}, one final chunk BEFORE [DONE]:
usage_chunk = {
    ...,
    "choices": [],   # empty
    "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...},
}
```

**Icebreaker tool_calls accumulator pattern:**

```python
# Accumulate tool_call fragments across chunks
tool_calls_buffer: dict[int, dict] = {}    # index -> accumulated tool_call

async for chunk in stream:
    for choice in chunk.choices:
        delta = choice.delta
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_calls_buffer:
                    tool_calls_buffer[idx] = {
                        "id": tc_delta.id,
                        "type": "function",
                        "function": {"name": tc_delta.function.name, "arguments": ""},
                    }
                # ACCUMULATE arguments
                if tc_delta.function.arguments:
                    tool_calls_buffer[idx]["function"]["arguments"] += tc_delta.function.arguments

# After stream: parse arguments as JSON
for idx, tc in tool_calls_buffer.items():
    args = json.loads(tc["function"]["arguments"])
```

### 15.9 LiteLLM exceptions to catch

```python
from litellm import (
    RateLimitError,             # 429; retry with backoff
    ContextWindowExceededError, # input too long; do NOT retry
    APIError,                   # generic 5xx; retry once, then fallback
    Timeout,                    # request timed out; retry once
    APIConnectionError,         # network failure; retry with backoff
    ContentPolicyViolationError,# provider refused; do NOT retry, escalate to fallback
    AuthenticationError,        # bad API key; do NOT retry; surface to user
    BadRequestError,            # 400; validate input then re-emit intent (not a retry)
    ServiceUnavailableError,    # 503; retry with backoff
    InvalidRequestError,        # 422; do NOT retry; surface schema error
    PermissionDeniedError,      # 403; do NOT retry; surface to user
    NotFoundError,              # 404; wrong model_id; surface to user
    OpenAIError,                # base class
)

# Icebreaker error handling matrix
_RETRYABLE = (RateLimitError, APIError, Timeout, APIConnectionError, ServiceUnavailableError)
_FALLBACK = (RateLimitError, APIError, Timeout, ContentPolicyViolationError, ServiceUnavailableError)
_TERMINAL = (ContextWindowExceededError, AuthenticationError, InvalidRequestError,
             PermissionDeniedError, NotFoundError, BadRequestError)
```

### 15.10 TurnEvent hierarchy (Icebreaker's daemon-to-client wire format)

Existing types stay; new types added for plan/streaming:

```python
# controller/turn_events.py (existing + new)

@dataclass
class TokenEvent:
    """Existing — QB/PB token stream"""
    turn_id: str
    seq: int
    text: str
    source: Literal["qb", "pb", "verifier"]

@dataclass
class CotEvent:
    """Existing — chain-of-thought step"""
    turn_id: str
    seq: int
    step_name: str
    step_state: Literal["thinking", "done", "failed"]
    detail: str

@dataclass
class ResultEvent:
    """Existing — turn outcome"""
    turn_id: str
    intent_id: str
    outcome: Literal["executed", "denied", "unsupported", "error"]
    reason: str

# ── NEW for v6.8 ──

@dataclass
class PlanStartedEvent:
    turn_id: str
    plan_id: str
    total_steps: int
    summary: str                # one-line "Plan: 3 steps to save your IP to a file"

@dataclass
class PlanStepStartedEvent:
    turn_id: str
    plan_id: str
    step_index: int
    step_intent_action: str     # e.g. "network.status"
    step_summary: str           # "Fetch your IP address"

@dataclass
class PlanStepCompletedEvent:
    turn_id: str
    plan_id: str
    step_index: int
    outcome: Literal["executed", "skipped", "failed", "denied"]
    result_hash: str            # SHA-256; content lookup via session
    elapsed_ms: int

@dataclass
class PlanCompletedEvent:
    turn_id: str
    plan_id: str
    steps_executed: int
    steps_skipped: int
    steps_failed: int

@dataclass
class McpdStdoutEvent:
    turn_id: str
    step_index: int
    line: str                   # single line; BP-8 redacted

@dataclass
class McpdStderrEvent:
    turn_id: str
    step_index: int
    line: str                   # BP-8 redacted

@dataclass
class HitlPromptEvent:
    """Existing shape enriched with COW dry-run"""
    turn_id: str
    intent_action: str
    tier: int
    summary: str                # one-line QB explanation
    cow_diff: list[str]         # e.g. ["+/tmp/notes.txt (37 bytes)", "-/tmp/old.txt (12 bytes)"]
    blast_radius_bytes: int
    timeout_seconds: int        # server-side hitl countdown

@dataclass
class HitlLockoutEvent:
    """Existing"""
    turn_id: str
    lockout_seconds: int
```

All events serialized to JSON-RPC 2.0 notifications over the daemon socket. Client (terminal / GUI) subscribes via `client.on("event_name", callback)`.

### 15.11 LangGraph interrupt payload contract

```python
from langgraph.types import interrupt, Command

# Inside HitlGate node
def hitl_gate_node(state: GraphState) -> dict:
    # Build the payload (JSON-serializable, small)
    payload = {
        "type": "hitl_prompt",
        "turn_id": state["turn_id"],
        "intent_action": ...,           # from session lookup by turn_id
        "tier": state["tier"],
        "summary": ...,
        "cow_diff": ...,
        "blast_radius_bytes": ...,
        "timeout_seconds": 30,
    }
    # This raises to pause graph; caller receives it via astream_events
    user_response: Literal["approve", "deny", "timeout"] = interrupt(payload)
    return {
        "hitl_decision": user_response,
        "hitl_pending": False,
    }

# When client submits decision, daemon calls:
graph.invoke(Command(resume="approve"), config={"configurable": {"thread_id": session_id}})
# Node reruns from top; interrupt() now returns "approve" instead of raising.
```

### 15.12 SqliteSaver connection contract

```python
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3
import os

# The from_conn_string factory returns a context manager
def make_checkpointer(cfg: CheckpointerConfig) -> SqliteSaver:
    """Icebreaker pattern — direct connection with our pragmas + msgpack allowlist."""
    path = os.path.expanduser(cfg.path)
    os.makedirs(os.path.dirname(path), exist_ok=True, mode=0o700)

    conn = sqlite3.connect(path, check_same_thread=False)
    # Icebreaker pragmas
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")  # WAL-safe fast mode

    # File perms (defense in depth beyond directory perms)
    os.chmod(path, 0o600)

    return SqliteSaver(
        conn=conn,
        # Msgpack safety (see §13.1)
        allowed_msgpack_modules=[
            "controller.agent_graph",
            "controller.session",
            "controller.intent_schema",
        ],
    )

# Config for invocation
config = {
    "configurable": {
        "thread_id": session_id,           # groups checkpoints by session
        "checkpoint_ns": "",               # default namespace
    }
}

# Write happens automatically on graph.invoke / graph.astream. Read on resume.
```

### 15.13 Plan schema (`controller/schemas/plan.json`)

```json
{
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://icebreaker.local/schemas/plan.json",
    "type": "object",
    "additionalProperties": false,
    "required": ["plan_id", "steps", "created_at"],
    "properties": {
        "plan_id": {
            "type": "string",
            "format": "uuid",
            "description": "Server-generated UUID; NEVER 'DO_NOT_EMIT'"
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["step_index", "intent"],
                "properties": {
                    "step_index": {"type": "integer", "minimum": 0, "maximum": 9},
                    "intent": {
                        "$ref": "intent.json"
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0, "maximum": 9},
                        "description": "step_indices whose result_hashes this step consumes"
                    }
                }
            }
        },
        "created_at": {"type": "string", "format": "date-time"},
        "summary": {
            "type": "string",
            "maxLength": 200,
            "description": "One-line human summary for the terminal"
        }
    }
}
```

### 15.14 TurnMemory shape (session history)

```python
# controller/session.py

@dataclass(frozen=True)
class TurnMemory:
    turn_id: str
    session_id: str
    query_hash: str                       # SHA-256 of user's raw query
    intent_action: str                    # e.g. "fs.list", "system.status"
    result_hash: str                      # SHA-256 of result content
    result_summary: str                   # ≤ 200 chars, redacted (BP-8)
    outcome: Literal["executed", "denied", "unsupported", "error"]
    tier: int
    timestamp_utc: str                    # ISO 8601

# Redaction runs on result_summary before storage; result content itself
# NEVER stored in memory — only its hash. Content stays in audit log.
```

### 15.15 AgentGraph adapter interface (Icebreaker's façade)

```python
# controller/agent_graph.py

class AgentGraph:
    def __init__(self, cfg: ControllerConfig, session_store: SessionStore):
        """Initialize graph, checkpointer, backends. Called once at daemon start."""

    def run(self, query: str, session_id: str) -> Iterator[TurnEvent]:
        """Sync interface used by daemon threads.
        Yields TurnEvent instances. Blocks on HITL. Idempotent on retry."""

    def resume(self, session_id: str, decision: Literal["approve", "deny"]) -> Iterator[TurnEvent]:
        """Resume a paused (interrupted) run. Called when client submits HITL decision."""

    def cancel(self, session_id: str) -> None:
        """Cancel a run in progress. Checkpoint state marked 'cancelled'."""

    def status(self, session_id: str) -> Literal["idle", "running", "paused", "completed", "error"]:
        """Query current state of a session's run."""

    def prune_checkpoints(self, older_than_days: int = 30) -> int:
        """Housekeeping. Returns count of deleted rows. Called on daemon startup."""

    def close(self) -> None:
        """Close checkpointer connection cleanly."""
```

This is the ONLY LangGraph-aware surface Icebreaker exposes. Every daemon/GUI/terminal consumer talks to this interface. Migration insurance §5.3.

### 15.16 Sources for Section 15

- [StateGraph reference — LangChain Reference](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)
- [Graph API overview — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [Streaming — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/streaming)
- [astream_events — LangChain Reference](https://reference.langchain.com/python/langgraph/pregel/main/Pregel/astream_events)
- [Function Calling — LiteLLM Docs](https://docs.litellm.ai/docs/completion/function_call)
- [Streaming + Async — LiteLLM Docs](https://docs.litellm.ai/docs/completion/stream)
- [Response Handling and Streaming — DeepWiki](https://deepwiki.com/BerriAI/litellm/2.5-response-handling-and-streaming)
- [Exception Mapping — LiteLLM Docs](https://docs.litellm.ai/docs/exception_mapping)
- [Structured output — Docs by LangChain](https://docs.langchain.com/oss/python/langchain/structured-output)
- [SqliteSaver — LangChain Reference](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/SqliteSaver)
- [Interrupts reference — LangChain Reference](https://reference.langchain.com/python/langgraph/types/interrupt)
- [Interrupt Command resume — Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/interrupts)

---

## 16. QB/PB integration architecture — how LangGraph maps to Icebreaker's existing brains

**Purpose.** Answer the question: *"how does this system connect to our existing PB and QB model — does it replace or augment QB?"* This section drills into the technical mapping without writing code.

### 16.1 One-sentence summary

**LangGraph replaces the orchestration around QB (not QB itself), and does not touch PB.** QB stays as the cloud LLM that interprets natural language; PB stays as the local llama.cpp server that grammar-decodes validated intents into tool calls. What changes is the wiring between them: LangGraph becomes the graph that connects Planner→Executor→Replanner. LiteLLM is a provider-neutral adapter around QB. mcpd is unchanged.

### 16.2 Current architecture (as of v6.7)

```
User query
    ↓
main.py::run_turn_streaming
    ↓
QB backend (backends/gemini.py, anthropic.py, openai.py)  ← REPLACED
    → sends system prompt + user query, streams tokens back
    → returns Intent (JSON matching intent.json schema)
    ↓
Schema validate (intent.json)
    ↓
Risk classify (Tier 0/1/2/3)
    ↓
Verifier vote (Tier 2+ or all today; will be Tier 2+ after M7.1)
    ↓
COW dry-run + HITL (Tier 2+)
    ↓
PB backend (backends/local.py, calls llama-server via HTTP with GBNF grammar)  ← UNCHANGED
    → grammar-constrained decoding produces mcp_tool_call JSON
    → returns tool_call dict
    ↓
mcpd dispatch (JSON-RPC over stdio)  ← UNCHANGED
    → sandboxed tool execution
    → returns result
    ↓
Response streamed back to client
```

### 16.3 New architecture (Option 4, v6.8)

```
User query
    ↓
main.py::run_turn_streaming
    ↓
AgentGraph.run(query, session_id)   [our adapter, §15.15]
    ↓
LangGraph StateGraph
    ├─ Node: Planner
    │     LiteLLM.completion(model="gemini/gemini-2.5-flash", messages=[…])   ← QB call (multi-provider via LiteLLM)
    │     → emits Plan JSON (1 or more Intent-steps)
    │     → grammar/schema validated against plan.json
    │
    ├─ Loop: Executor (one iteration per plan step)
    │     ├─ Schema validate (intent.json)
    │     ├─ Risk classify → decides edge
    │     ├─ [Tier 2+] → Verifier node → Voter
    │     ├─ [Tier 2+] → HitlGate node → interrupt() → wait for user
    │     ├─ Executor calls PB via HTTP (llama-server /completion with GBNF grammar)   ← PB call (UNCHANGED)
    │     │     → grammar-constrained decoding produces mcp_tool_call JSON
    │     ├─ McpdDispatcher node — calls mcpd via JSON-RPC over stdio          ← mcpd (UNCHANGED)
    │     └─ Accumulate step result (hash) in state
    │
    ├─ Node: Replanner
    │     LiteLLM.completion again — reads plan + step results, decides continue/amend/done
    │
    └─ Node: Responder
          Streams final natural-language response from replanner output
    ↓
TurnEvent stream translated to client (§15.10)
```

### 16.4 QB integration in detail

**What QB is today**
- Gemini 2.5 Flash (or Anthropic/OpenAI/local Qwen) called via provider-specific backend adapter
- Prompt: `prompts/qb_intent.txt` (system prompt) + user query + optional shell context
- Output: JSON Intent matching `schemas/intent.json`
- Streaming: token-by-token via provider SDK's streaming interface
- INV-1 constraint: QB has ZERO MCP tool connections; QB never dispatches anything

**What QB becomes in v6.8**
- Same model. Same prompt. Same output shape (Intent → wrapped in a 1-step Plan).
- Called through LiteLLM (§15.6) — model_id string picks provider.
- Called from LangGraph's Planner node.
- Also called from Replanner node (a second QB call after execution to decide continuation).
- Streaming preserved: LangGraph's `astream_events` surfaces LiteLLM's chunks as `on_chat_model_stream` events, which the adapter translates to `TokenEvent(source="qb", ...)`.

**INV-1 preservation with LangGraph**
The concern: LangGraph state is shared across nodes. If Planner puts raw QB output in state, downstream nodes see it, and PB might get exposed to raw QB text.

**Mitigation** (per §15.1):
- Planner's raw text output is stored in `session.py`'s external store, keyed by `plan_id`.
- State only holds `plan_id` + validated step summaries (e.g. `intent_action`, `tier`).
- Executor fetches the specific Intent it needs by `(plan_id, step_index)`. Never sees the raw QB text.
- PB receives ONLY the validated Intent (schema-clean, no free-text QB output).
- INV-1 preserved: PB gets structured Intent, not QB's raw stream.

**Prompt/model choice via LiteLLM**
- The Planner node's `model_id` is read from config (`run.qb_backend`, `run.qb_model`).
- LiteLLM's model_id string picks the provider: `"gemini/gemini-2.5-flash"`, `"anthropic/claude-sonnet-4-6"`, `"openai/gpt-5"`, or `"ollama_chat/qwen-…"` for a local QB (future L3).
- Fallback chain (`fallback_backend.py`) still lives in Icebreaker; wraps LiteLLM calls.
- Prompt file (`qb_intent.txt`) unchanged in structure. M7.3 rewrites the CONTENT for accuracy + nav-phrase mapping.

### 16.5 PB integration in detail

**What PB is today**
- Local llama.cpp server (started by `pbd.service`) serving `run7_cot_q4km.gguf` (Qwen 2.5 Coder 1.5B fine-tuned).
- Called via HTTP POST to `http://localhost:8080/completion` (or unix socket).
- Request body includes `prompt` (the validated Intent injected into a template) + `grammar` (the GBNF file content from `grammars/qb_intent.gbnf`).
- llama.cpp applies **grammar-constrained decoding**: every candidate token is checked against the GBNF grammar; invalid tokens masked out. Output is guaranteed schema-conforming JSON.
- Output: `mcp_tool_call` JSON — a validated tool call ready for mcpd.
- Streaming: yes, llama-server supports streaming; PB currently decodes fully before returning.
- INV-1 constraint: PB has ZERO access to raw user input or external document content. Only sees the schema-validated Intent (which itself is opaque — the raw text has been stripped).

**What PB becomes in v6.8**
- **PB is completely unchanged.** Same model, same GBNF grammar, same llama-server, same HTTP endpoint.
- **LangGraph doesn't know about PB.** LangGraph knows about a node called `Executor` which internally makes an HTTP call.
- The Executor node's implementation (function body) is:
  1. Read Intent from session by `(plan_id, step_index)`.
  2. POST to local llama-server `/completion` with `{prompt: <intent_wrapped>, grammar: <gbnf_content>, stream: true}`.
  3. Consume the streaming response.
  4. Return validated `mcp_tool_call` dict.
  5. Emit tokens as custom stream events → translated to `TokenEvent(source="pb")`.
- **GBNF grammar stays.** LangGraph has no native GBNF support ([research confirms](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)); GBNF is a llama.cpp-level feature. Icebreaker keeps using it directly through the HTTP call.
- **Verifier voting on PB output** stays — but only for Tier 2+ after M7.1. Verifier node in the graph is between Executor's PB call and McpdDispatcher.

**Why PB is not "a tool" from LangGraph's perspective**
- LangGraph's tool-binding is designed for QB (the reasoning model) to invoke tools that produce results. `Gemini.bind_tools([fs.list, fs.read, ...])`.
- If we bound tools to QB directly, QB's LiteLLM call would emit `tool_calls` deltas → we'd dispatch to mcpd directly from QB. **This breaks INV-1.**
- PB is not a tool; PB is a **mandatory intermediary**. Its job is to take a validated Intent (from QB, schema-clean) and rewrite it into a mcpd-compatible tool_call with grammar constraints. This is a security transformation, not a semantic tool.
- Therefore: **QB is NOT bound with any tools in v6.8.** QB's LiteLLM call emits a plain JSON Plan matching `plan.json` schema — NO tool_calls in the response. The Plan contains Intents which are LATER given to PB.
- We use `response_format={"type": "json_object"}` on QB calls to force JSON output. Plus the QB prompt has explicit schema instructions. Both together give reliable schema-conforming JSON without tool-calls mechanism.

### 16.6 Risk classifier position

**Today:** runs after PB's tool_call is produced (main.py risk_classifier).
**v6.8:** runs on the Intent BEFORE it reaches PB. Two reasons:
1. Tier-0 fast path (L1/M7.2) — if Intent is Tier 0, we can skip PB entirely and dispatch a QB-emitted intent directly to mcpd (still with schema validation). Requires knowing tier before deciding to call PB.
2. Verifier + HITL gating — need to know tier before deciding whether Verifier and HITL nodes are involved.

Placement in graph: **RiskClassifier node between "Intent validated" and "Executor invokes PB"**. Conditional edges branch on `state["tier"]`.

### 16.7 Verifier position

**Today:** verifier voting runs on the Intent (or tool_call, ambiguous in current code) synchronously between validation and dispatch. Blocks the turn.

**v6.8:** verifier is a LangGraph node. Edges gate it on `tier >= tier_floor` (default 2 per M7.1). For Tier 0/1 it's SKIPPED.

Placement: after RiskClassifier, before HitlGate.

Verifier internally uses LiteLLM to call the verifier model (currently a second QB pass; could be a different model per config). Result: `approve` / `reject`. On reject, graph routes back to Planner with a `verifier_rejected` reason for replanning.

### 16.8 HitlGate + mcpd dispatch positions

**HitlGate node** (Tier 2+): calls `interrupt()` with the payload (§15.11). Graph pauses. Client sees `HitlPromptEvent` (§15.10). User's decision resumes the graph. Node returns `hitl_decision` in state update.

**McpdDispatcher node** (all tiers): after HitlGate (if applicable) OR straight after Executor for Tier 0/1. Makes JSON-RPC call to mcpd, streams stdout/stderr via custom events. Result → session store; hash → state.

**Critical**: McpdDispatcher is SEPARATE from Executor node (per §13.2 idempotency). Executor calls PB. Dispatcher calls mcpd. This way HITL interrupt inside Executor won't re-dispatch mcpd on resume.

Actually re-reading §13.2: the concern is that if Executor calls PB AND then Dispatcher on the same graph turn, and HITL interrupt fires between them (which it does for Tier 2+), then on resume Executor re-runs from top and re-calls PB. That's fine — PB's grammar-decode is idempotent for a fixed Intent input.

But: we should NOT call PB before HITL. Better graph:

```
RiskClassifier → (Tier 0/1) → Executor (PB) → McpdDispatcher → next step
              → (Tier 2+)  → Verifier → HitlGate → Executor (PB) → McpdDispatcher → next step
```

For Tier 2+, HITL fires BEFORE PB grammar-decode. This way the user approves the Intent (which they can understand), not the tool_call (which is more technical). It also avoids the "PB was called but the user then denied" waste.

### 16.9 Streaming event translation

LangGraph `astream_events` (§15.4) emits events with types like `on_chat_model_stream`, `on_chain_start`, `on_tool_start`. Icebreaker's translation layer maps these to `TurnEvent` (§15.10):

| LangGraph event | Icebreaker event | Notes |
|---|---|---|
| `on_chain_start` (Planner) | `PlanStartedEvent` | Fires when planner begins |
| `on_chat_model_stream` (in Planner) | `TokenEvent(source="qb")` | Every QB token |
| `on_chain_end` (Planner) | `PlanStartedEvent` (updated with total_steps) OR fold into first PlanStep | Plan fully assembled |
| `on_chain_start` (Executor step N) | `PlanStepStartedEvent(step_index=N)` | Step begins |
| `on_chat_model_stream` (in Executor, PB call) | `TokenEvent(source="pb")` | Every PB token |
| Custom event `mcpd_stdout` (in McpdDispatcher) | `McpdStdoutEvent` | Live mcpd output |
| Custom event `mcpd_stderr` (in McpdDispatcher) | `McpdStderrEvent` | Live mcpd errors |
| `on_chain_end` (Executor step N) | `PlanStepCompletedEvent(step_index=N)` | Step done |
| interrupt() raised (HitlGate) | `HitlPromptEvent` | Pause, show prompt |
| `on_chain_end` (last node) | `ResultEvent`, `PlanCompletedEvent` | Turn done |

Terminal client (Textual TUI) already handles CoTEvent, TokenEvent, ResultEvent, RpaEvent. Extending to handle PlanStarted/PlanStepStarted/etc. is one method per event type in `terminal/app.py`.

### 16.10 Session state flow

**Today:** `SessionConfig` and `ShellContext` carry per-turn state. `Session` object is passed into `run_turn`.

**v6.8:** `Session` gains `history: list[TurnMemory]` (M7.5). Session lives in an external store (in-memory dict for now, potentially SQLite for persistence in v6.9). LangGraph state carries `session_id`. Nodes fetch the session by ID when they need it.

Flow:
1. User types query in terminal.
2. Client sends `turn.run(query, session_id)` to daemon.
3. Daemon calls `AgentGraph.run(query, session_id)`.
4. Agent graph's Planner node fetches session via `session_id`, reads `history[-10:]` for context, passes to LiteLLM call as system messages.
5. Executor node fetches Intent by `(plan_id, step_index)`.
6. On turn completion, Responder writes a new `TurnMemory` into `session.history`.
7. Client receives the ResultEvent.

Session store is Icebreaker-owned (`controller/session_store.py`); LangGraph doesn't know about it beyond the `session_id` key.

### 16.11 Backward compatibility with existing controller code

- `main.py::run_turn_streaming` becomes a thin wrapper that calls `AgentGraph.run(...)` and forwards events.
- Existing per-provider backends (`backends/gemini.py`, `anthropic.py`, `openai.py`) are DELETED after LiteLLM migration lands.
- `backends/local.py` (PB via llama-server) stays — Executor node uses it directly.
- `verifier.py` stays; called from a LangGraph node.
- `risk_classifier.py` stays; called from a LangGraph node.
- `hitl.py`'s `HitlPresenter` and `TerminalPresenter` stay for GUI dialog rendering, but the wait-for-decision mechanism moves to LangGraph interrupt.
- `session.py` gets `history` field.
- `main.py` orchestration code shrinks by ~500 lines (LangGraph does the work).
- Audit hash-chain (`logger.py`, INV-8) unchanged; called from a LangGraph node.
- mcpd unchanged.

### 16.12 What replaces vs augments

**LangGraph REPLACES:**
- `main.py::run_turn_streaming` orchestration logic
- The per-step control flow (validate → classify → verify → HITL → dispatch)
- Custom streaming code (LangGraph's `astream_events` does it)
- Custom checkpoint code (LangGraph's SqliteSaver does it)

**LangGraph AUGMENTS (does not touch, but wraps):**
- QB call — same model, same prompt, but called through LiteLLM inside a Planner node
- PB call — same model, same GBNF grammar, but called through an Executor node
- mcpd dispatch — same JSON-RPC, but called through a McpdDispatcher node
- Session store — same shape, but nodes fetch via session_id from state
- Verifier + risk classifier + HITL — same logic, but now as graph nodes with edges

**LangGraph DOES NOT TOUCH:**
- mcpd (Rust binary)
- Sandbox stack (Landlock/Seccomp/COW)
- Audit hash-chain
- Model integrity checks
- Systemd services (`pbd`, `qbd`, `mcpd`, `controller`)
- Terminal TUI (Textual) — beyond adding new event handlers

### 16.13 The migration steps in dependency order

1. **LiteLLM backend adapter** (2-3 days). Wrap existing gemini/anthropic/openai backends in a unified LiteLLM interface. Verify streaming still works. Test suite passes.
2. **Session store extraction** (~1 day). Move session lookup from `run_turn` into a `SessionStore` class. Prepares for nodes fetching sessions by ID.
3. **AgentGraph adapter** (~3 days). Build `agent_graph.py` with the StateGraph, node functions calling out to existing backends + verifier + risk_classifier + mcpd. NO plan-mode yet — a 1-step plan for backward compat.
4. **Migrate `run_turn_streaming`** to call `AgentGraph.run` (~1 day). Existing tests pass (existing behavior is a 1-step plan).
5. **Add plan-mode** (~2 days). QB prompt updated to emit multi-step plans; Planner node parses; Executor loops.
6. **Add session history + replanner** (~2 days). Session gets `history` field; Replanner node uses it.
7. **Add HITL via interrupt** (~2 days). Migrate HITL from synchronous stdin-read to LangGraph interrupt.
8. **Terminal HITL wiring (F-61)** (~2 days). Terminal's app.py subscribes to `HitlPromptEvent`, renders inline card, key handler calls `AgentGraph.resume(session_id, decision)`.

Cumulative: ~15 days of dev work, roughly the 4-week sprint plan.

### 16.14 What we deliberately do NOT do

- **Do NOT bind mcpd tools to QB via LangGraph tool_binding.** This would let QB emit `tool_calls` that dispatch directly. INV-1 breach.
- **Do NOT let LangGraph state carry raw QB or PB output.** State carries hashes/keys; content in session.
- **Do NOT let LangGraph choose which PB to call.** PB is fixed (local llama-server per config). Only QB is provider-selectable via LiteLLM.
- **Do NOT use LangGraph's `create_react_agent` prebuilt.** ReAct pattern lets model iterate freely; we need explicit Planner→Executor separation for INV-1.
- **Do NOT put mcpd behind LangChain's `Tool` abstraction.** mcpd is a stdio JSON-RPC service; wrap it as a function call inside a node, not as a LangChain Tool.
- **Do NOT let LangGraph resume execution WITHOUT going through Icebreaker's audit chain.** Every graph resume writes an audit line.

### 16.15 Sources for Section 16

- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Grammar and Structured Output — DeepWiki](https://deepwiki.com/ggml-org/llama.cpp/8.1-grammar-and-structured-output)
- [Grammars (GBNF) — llama.cpp Advanced](https://mintlify.com/ggml-org/llama.cpp/advanced/grammars)
- [LangGraph starter template for llama-cpp-python (irandysousa/langgraph-llama-cpp-starter)](https://github.com/irandysousa/langgraph-llama-cpp-starter)
- [Building AI Agents with llama.cpp — KDnuggets](https://www.kdnuggets.com/building-ai-agent-with-llama-cpp)
