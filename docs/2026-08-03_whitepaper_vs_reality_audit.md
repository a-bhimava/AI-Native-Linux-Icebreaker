# Icebreaker — Whitepaper vs Reality Audit

**Date**: 2026-08-03
**Branch at audit time**: `feat/v6.15-vision-grounding`
**Reference document**: `AI_Native_OS_Whitepaper.md` v1.1 (revised 2026-07-09)
**Shipping ISO at audit time**: v6.15 arm64, both editions (built earlier the same day)

---

## Status update — 2026-08-11 (v6.17 pre-build)

**13 of 26 departures closed** across v6.16 + v6.17 milestones. All three Tier A flagship claims are now real. This ledger's original body below is preserved verbatim as the audit-time snapshot; the closures show up as `→ F-109`, `→ F-110`, or `→ F-111` in the Status column of individual rows.

| Row | Original state | Now | Closed by |
|---|---|---|---|
| **C-1** (Tier A #1) — COW imagination layer | DEPARTED — cow.rs doesn't exist | **SHIPPED (simulation, not literal overlayfs — user-approved design 2026-08-03)** | v6.16 M7.0.1a-h · commits `c3ac412..630aa54` · F-109 |
| **C-4** (Tier A #2) — GBNF-constrained PB | DEPARTED — PB launched without --grammar-file | **SHIPPED (wire-up + bounded retry loop with QB-consult rescue for edge cases)** | v6.16 M7.0.2a-g · commits `977bdea..39a991d` · F-110 |
| **A-1..A-8** (Tier A #3) — OC edition INV-1 gap | DEPARTED — opencode/Gemini invoked mcpd + iceui directly; no Intent Object, no PB, no schema/risk/HITL, no verifier, sentinel audit rows | **SHIPPED (submit_intent flow — opencode locked to ONE tool; Controller.run_turn_from_intent drives full 11-step pipeline; real Gemini QB in OC mode; audit deconfliction via IntentStore UUID ring)** | v6.17 M7.6a-1a-i · commits `79185ff..<pending>` · F-111 |
| **B-2** — HITL Tier-3 shows dry-run diff | Tier-3 gate carries `cow_summary=None` | **SHIPPED — streaming Step 3.5 pre-pass populates diff at INITIAL gate** | v6.16 M7.0.1e · F-109 |
| **B-3** — AgentGraph has COW branch | AgentGraph mcpd_dispatcher_node had no COW path | **SHIPPED — new `cow_preview_node` between verifier + hitl_gate** | v6.16 M7.0.1f · F-109 |
| **B-4** — GBNF constraint | Controller-side `_build_pb` omitted grammar_path; server-side silently degraded | **SHIPPED — both sides wired + build-time smoke-gate + first-boot warn-not-crash** | v6.16 M7.0.2a-g · F-110 |

**Whitepaper §5 text update** (acknowledging simulation approach over literal overlayfs) still deferred to M7.6b per the M7.0.1 design decision. **Per-tool GBNF tightening** (enumerate 25 tool names + per-tool params shape in the grammar) deferred to v6.18+ — the M7.0.2 generic `{"tool": <str>, "params": <obj>}` grammar closes §6's shape guarantee; per-tool schema violations that survive it are caught by the bounded retry loop.

**A-6/A-7 partial closure**: audit rows for non-submit_intent iceui direct calls (legacy_direct rollback path only) still rely on `oc_audit_bridge` sentinel enrichment; full parity is M7.0.3 v6.18 scope.

**13 departures remain**. Next tracked in `docs/2026-08-03_phase7_convergence_tracker.md` progress ledger.

---

## What this document is

An honest ledger of every place the shipping code has departed from the whitepaper's architectural claims. It was triggered by a user question during v6.15 OC-edition verification: *"we are using gemini via opencode for everything — this isn't a dual-brain architecture at all. why?"* That question turned out to be the tip of a much bigger drift.

**What this document is NOT**: a fix proposal. No implementation direction is chosen here. The audit exists so a human reader can pick which departures matter, in what order, before any code moves.

## How to use this document

Each departure has a citation to the whitepaper claim it violates and a `file:line` citation to the code that departs. Rows are grouped by scope:

- **Section A** — OC edition (`icebreaker-ai-terminal` menu tile, opencode + Gemini)
- **Section B** — Current edition (Textual TUI / GTK chatbot + Python daemon + local PB)
- **Section C** — Cross-cutting (mcpd Rust sandbox, KPIs, Phase 7/8 roadmap, BP invariants)
- **Consolidated tiers** — the same rows re-sorted by severity (flatly false → aspirational → deliberate)
- **Honest one-paragraph summary** at the end

Skim the tiers first if you have five minutes; drop into A/B/C only for the specific row you want to verify.

## Method

1. Full read of `AI_Native_OS_Whitepaper.md` (991 lines) — every architectural claim, invariant, KPI, and roadmap milestone extracted.
2. Three parallel Explore agents each with a distinct scope, each producing citations to `file:line`. No inference — only cited evidence made it into the tables below.
3. Aggregation into this doc. Zero fixes proposed.

---

## Section A — OC edition (opencode + Gemini)

### Turn trace ("delete ~/foo.txt" in opencode TUI)

1. `icebreaker-oc` launches opencode 1.18.4 with `OPENCODE_CONFIG=/etc/icebreaker/qb_oc.json` (`cx-distro/distro/icebreaker-oc:98,102`).
2. Gemini call goes out with `GOOGLE_GENERATIVE_AI_API_KEY` alias (`cx-distro/distro/icebreaker-oc:47-49`); Gemini returns `tools/call name=icebreaker_fs.delete`.
3. `fs.delete` is `ask` per `incremental/build/gen-oc-config.sh:85-89` → opencode fires its own native ask modal.
4. On approve, opencode → stdio → `cx-distro/distro/mcpd-for-oc.sh:45` → mcpd Rust binary → executes + writes `/var/log/mcpd/audit.log`.
5. If the Python controller daemon happens to be running, `OCAuditBridge` tails and enriches (`dual-brain/controller/oc_audit_bridge.py:283-305`).
6. **`icebreaker-pbd.service` / `llama-server` / `run7_cot_q4km.gguf` receive NOTHING** — no OC path routes to the PB socket.

### Departures

| # | Whitepaper claim | Reality | Evidence |
|---|---|---|---|
| A-1 | INV-1 §5 line 107: "QB has zero direct access [to] system execution tools" | QB (Gemini via opencode) invokes mcpd + iceui tools directly | `cx-distro/distro/qb_oc.json.template:6-18`; `cx-distro/distro/mcpd-for-oc.sh:45` |
| A-2 | §5 line 152: "Controller passes only an opaque reference ID to the PB" | No Intent Object. No reference ID. No PB. | `dual-brain/controller/main.py:544-545` short-circuits OC turn as `UNSUPPORTED` |
| A-3 | §5 line 117: "PB is completely blind to the outside world" | Moot — PB is not on the call path; systemd unit boots but nothing routes there | `cx-distro/config/hooks/live/0100-icebreaker-setup.hook.chroot:7` enables unit; no OC code opens `/run/icebreaker/pbd.sock` |
| A-4 | §5 lines 172-176: "Controller — schema validation, risk classification, HITL gate, audit logging" | Only audit partially preserved. Schema/risk/HITL all bypassed on OC tool path | `dual-brain/controller/mcp_gui_server.py:257-263` calls `_dispatch_in_subprocess` directly, no Controller gates |
| A-5 | §8 lines 421-443: Tier 2 LLM classifier + Tier 3 blocking modal with dry-run + risk badge | Flattened to opencode's `allow`/`ask`/`deny`. No dry-run consequence, no risk line, no tier label | `incremental/build/gen-oc-config.sh:107-109,181`; `cx-distro/distro/qb_oc.json.template:26-29` |
| A-6 | INV-8: audit captures every intent including rejected | `iceui_*` tool calls have ZERO audit write path | `dual-brain/controller/mcp_gui_server.py` has 0 `AuditLog` references; `dual-brain/controller/oc_audit_bridge.py:71-74` only tails mcpd's log |
| A-7 | §5: audit fields include model, tokens, cost, session, turn | OC audit rows hard-stamp `session_id="opencode-oc"`, `turn_index=-1`, `model=""`, `tokens_in=0`, `tokens_out=0`, `cost=0.0` | `dual-brain/controller/oc_audit_bridge.py:22-27,158-175` |
| A-8 | INV-1 §3 lines 152-153: PB "resolves the reference, evaluates the action against its policy rules, and either executes or escalates" | No PB policy pass on OC path — Gemini emits args, mcpd executes verbatim after opencode's ask | Same as A-1/A-2 |
| A-9 | INV-1 §5 line 130: raw screenshot bytes never touch the QB | `VisionGrounder` sends screenshot to `gemini-2.5-flash` — same provider as QB. In-QB vision second call has no whitepaper accommodation | `dual-brain/gui_agent/vision.py:5-6,28-30`; `dual-brain/controller/config.py:366` |

**Severity summary for OC edition**: INV-1 is not enforced. INV-8 is enforced only for mcpd tools (not iceui tools). Graduated Determinism §8 is degraded to allow/ask/deny with no dry-run preview. `run7_cot_q4km.gguf` is packaged but idle.

---

## Section B — Current edition (Textual TUI / GTK chatbot + Python daemon + local PB)

### What works

- QB tool-call block: QB backends never pass `tools`/`tool_choice`; `dual-brain/controller/backends/auditor.py:23-27` FORBIDDEN_KEYS enforces at every call. QB emits Intent Objects, not tool calls.
- Schema validation: `dual-brain/controller/main.py:698` + `1744` + `dual-brain/controller/agent_graph_nodes.py:501-513` via `dual-brain/controller/intent_schema.py::IntentSchema.validate`
- Risk classifier: `dual-brain/controller/main.py:765` (streaming), `1777` (non-streaming), `dual-brain/controller/agent_graph_nodes.py:322-348` (graph). Escalate-only (BP-5) at `dual-brain/controller/risk_classifier.py:228-233`
- HITL Tier-3 blocking gate: 3s lockout enforced (`dual-brain/controller/hitl.py:475`) — INV-6 latency portion satisfied
- Opaque ID minting: `dual-brain/controller/intent_store.py:63-68` UUID, passed to `build_pb_user_turn`
- Audit hash chain (INV-8): `dual-brain/controller/audit.py:429-433` O_APPEND + per-line fsync + `_recover_tail` + `verify_chain`. Rejects captured (`SCHEMA_REJECTED / HITL_DENIED / PB_SCHEMA_ERROR / TOOL_ERROR`)
- Four-tier classifier reachable
- Param validation (INV-4): `dual-brain/controller/main.py:1017-1040` + `2388-2389` `jsonschema.validate`

### Departures

| # | Whitepaper claim | Reality | Evidence |
|---|---|---|---|
| B-1 | INV-1 §3/§6: "PB never sees the raw data the QB processed" / "passes only an opaque reference ID to the PB" | PB payload carries `intent_id, allowed_tool, tool_schema, target, expected_content, pb_hint` — target string + content bytes + coaching text ride alongside the opaque ID. `session.py:3` docstring still claims "ONLY {intent_id, allowed_tool, tool_schema}" — code and docs diverged | `dual-brain/controller/session.py:297-335` (F-27 + F-41 fixes) |
| B-2 | §8.2 HITL prompt shape: "Dry-run: 3.2 GB will be freed. 847 files will be deleted" | Tier-3 HITL gate fires with `cow_summary=None`. Dry-run preview only appears in a **second** HITL round after mcpd returns `requires_cow_approval` — and only on the streaming path | `dual-brain/controller/main.py:840-845` (initial gate with None); `dual-brain/controller/main.py:1544-1578` (post-mcpd COW gate) |
| B-3 | §5 + §8 COW dry-run present at every destructive-op approval | AgentGraph path (`mcpd_dispatcher_node`) has **NO COW-approval branch at all**. Only streaming path has it | `dual-brain/controller/agent_graph_nodes.py:742-879` — no `requires_cow_approval` handler |
| B-4 | §6 "constrained-decoded via GBNF … the model literally CANNOT generate invalid output" | Controller-side `LlamaCppLocalBackend` only attaches grammar if `self._grammar is not None`; `__main__.py:170-178 _build_pb()` omits `grammar_path` → `self._grammar = None`. Server-side `start_pb.sh --grammar-file` is the only enforcement, and it **silently degrades** to unconstrained if the grammar file doesn't resolve at that path | `dual-brain/controller/backends/llama_local_backend.py:196-197,138-139`; `dual-brain/controller/__main__.py:170-178`; `dual-brain/scripts/start_pb.sh:62-65` (silent warn+skip) |
| B-5 | §6 GUI/RPA "User → QB → Controller → PB → mcpd" | AgentGraph path's `mcpd_dispatcher_node` intercepts `gui.*`/`rpa.*` and dispatches to `controller.gui_worker` subprocess directly — **mcpd is bypassed** for the GUI/RPA tool surface. Streaming path in `main.py` still routes through mcpd | `dual-brain/controller/agent_graph_nodes.py:798-841` |

### INV compliance ledger (current edition)

- INV-1: **PARTIAL** — QB tool-call block enforced, but PB payload deviates from opaque-only
- INV-2: **PARTIAL** — schema validated, but "ONLY opaque reference IDs to PB" violated per B-1
- INV-4: **ENFORCED** — param validation before dispatch
- INV-6 (COW + 3s lockout): **PARTIAL** — 3s lockout yes; COW preview only in second gate; AgentGraph path lacks COW branch entirely
- INV-8 (audit): **ENFORCED** — hash chain + O_APPEND + rejects captured
- INV-3/5/7 (mcpd Rust + model integrity): out of controller scope, covered by mcpd + start-pbd

### Additional PB gaps

- Speculative decoding: wired at server launch (`dual-brain/scripts/start_pb.sh:50-58 --draft-model ... --draft 8`) BUT requires `[pb] draft_model_id` in TOML, and `controller.toml.example` shows no `[pb]` section — **off unless operator explicitly enables**
- Grammar file silent-fail: `dual-brain/scripts/start_pb.sh:62-65` prints a warning if grammar file missing, then launches unconstrained. No refuse-to-boot. No boot-time assertion. Rot-safe: no.

---

## Section C — Cross-cutting (mcpd Rust sandbox, KPIs, Phase 7/8 roadmap, BP invariants)

### What ships as promised

- Landlock ruleset applied to mcpd before `server::run_stdio_server` (`src/mcpd/src/main.rs:52`, `src/mcpd/src/sandbox/landlock.rs:35-113`)
- Seccomp-BPF denylist + allowlist with `PR_SET_NO_NEW_PRIVS` before it (`src/mcpd/src/sandbox/seccomp.rs:38-57`)
- squashfs read-only for models (implicit — models live inside `filesystem.squashfs`)
- BP-1 registries: BrainBackend + Presenter + ModelRegistry all decorator-registered
- BP-8 secret hygiene: `_scrubbed_env` whitelist inheritance for mcpd + RPA, tested (`dual-brain/controller/tests/test_mcpd_env_scrub.py`)
- KPI 3: FEH eval pipeline shipped (`privileged-brain/07_evaluate.sh`)

### Departures

| # | Whitepaper §  | Expected | Actual | Evidence | Status |
|---|---|---|---|---|---|
| C-1 | §5 COW dry-run | tmpfs/overlayfs mount before destructive ops; "imagination layer" | ticket-only stub — no `cow.rs` file exists; `fs.write` outside `$HOME` and `fs.delete` return `requires_cow_approval` ticket without any actual overlay | `src/mcpd/src/tools/fs.rs:215,238,258-278`; grep for `src/mcpd/src/sandbox/cow.rs` → does not exist | **SHIPPED 2026-08-06 → F-109** (v6.16 M7.0.1a-h: `src/mcpd/src/tools/cow.rs` new + IntentStore + cow.commit RPC + controller two-phase commit; simulation approach per user design 2026-08-03, not literal overlayfs) |
| C-2 | §5 <10ms overhead | benchmark | none — only mcpd tools/list p95 | `src/mcpd/ci.sh:133-181` | NOT-STARTED |
| C-3 | §6 Speculative decoding | draft+target on PB | no draft ships; PB launched without `--draft-model` | `dual-brain/scripts/start-pbd:65-69`; no draft GGUF in tree | **DEPARTED (PB)** |
| C-4 | §6 Grammar-constrained decoding | PB uses `mcp_tool_call.gbnf` — "the model literally CANNOT generate invalid output" | PB explicitly grammarless; `dual-brain/scripts/start-pbd:63` comment says "PB uses no grammar — relies on post-hoc tool-call validation." QB gets `--grammar-file`, PB does not | `dual-brain/scripts/start-pbd:63`; `dual-brain/scripts/start-qbd:116` | **SHIPPED 2026-08-06 → F-110** (v6.16 M7.0.2a-g: controller `_build_pb` passes grammar_path + start-pbd wires --grammar-file + v6.manifest ships file + smoke-gate + bounded PbRetryLoop with QB-consult rescue for per-tool violations that survive generic grammar; per-tool GBNF tightening deferred to v6.17) |
| C-5 | §6 ~2 GB RAM footprint | measured | prose only | `docs/ARCHITECTURE.md:66` | NOT-STARTED |
| C-6 | INV-7 sha256 pin | receipts for all shipped GGUFs | only 1 file listed (`run7_cot_q4km.gguf`); no draft, no QB entries | `models/checksums.sha256` | PARTIAL |
| C-7 | §9 live-build | Debian live-build | debootstrap + mksquashfs + xorriso; live-build broken on Noble | `cx-distro/build.sh:549-550` (documented) | DEPARTED (deliberate) |
| C-8 | §9 boot <60s | gated first-boot timer | timer exists, no pass/fail gate | `cx-distro/distro/first-boot:42,279-289` | PARTIAL |
| C-9 | §9 GPG-signed ISO | signed release | not wired | `incremental/GROUND_TRUTH.md:38-39` (marked pending) | NOT-STARTED |
| C-10 | §10 Phase 7 M7.2–M7.7 | Playwright + MS Graph + Google Workspace MCPs, `analytics.chart`, GPG, hardening | none in tree; only M7.1 pre-work: `x-icebreaker-trust` in 24 schemas + empty allowlist toml | `cx-distro/distro/mcp_allowlist.toml` empty | NOT-STARTED (M7.2-M7.7); PARTIAL (M7.1) |
| C-11 | §10 Phase 8 | Goal/plan_steps/verifier/sub-agents | agent_graph bridge exists but single-turn only | `dual-brain/controller/agent_graph_bridge.py` | NOT-STARTED |
| C-12 | §13 KPI 1 latency | end-to-end p95 suite | only mcpd tools/list benchmark | `src/mcpd/ci.sh:133-181` | PARTIAL |
| C-13 | §13 KPI 5 Tier-3 telemetry | Tier-3/day aggregator | none | — | NOT-STARTED |
| C-14 | §14 sandlock / CX Linux / cortexd | primary refs | not integrated; direct `landlock` crate | `src/mcpd/src/sandbox/landlock.rs:27-33` | ASPIRATIONAL |
| C-15 | §14 Datasets (The Stack, man pages) | in training set | NL2SH / NL2Bash / CodeAlpaca only; no Stack shell, no man pages | `privileged-brain/data/raw/*` | PARTIAL |
| C-16 | BP-3 sanitize before terminal | universal ANSI/C0 scrub on model stream | applied on GUI audit fields only | `dual-brain/controller/audit.py:224,227`; `dual-brain/controller/main.py:1280,1322,1429` | PARTIAL |
| C-17 | BP-13 no repeat regressions | every F-xx has named regression lock | 48/94 F-xx rows have `regression lock` (~51%) | `incremental/GROUND_TRUTH.md § 7` | PARTIAL |

### Two findings worth highlighting (2026-08-03 audit-time; both now closed)

- **C-1 (COW was a stub)**: the whitepaper's flagship "Imagination Layer" — the thing that separates Icebreaker from "an AI that can make mistakes" from "an AI that cannot make irreversible ones" — was not implemented. mcpd returned a ticket that told the caller "you should run this through COW"; nobody actually did. Both editions of HITL preview showed the tool args, not a diff. **CLOSED 2026-08-06 by v6.16 M7.0.1a-h (F-109)** — simulation-based per user's 2026-08-03 design (walkdir+du + stat + apt-get -s); user-outcome-identical to literal overlayfs without the seccomp weakening. See `incremental/GROUND_TRUTH.md § 7 F-109` for full commit trail.
- **C-4 (PB had no GBNF)**: §6 said "the model literally CANNOT generate invalid output — it is mathematically impossible." Reality: PB was launched without `--grammar-file`; only the QB got grammar constraints. All PB safety came from post-hoc validation, which the whitepaper explicitly said is inferior to constrained generation. **CLOSED 2026-08-06 by v6.16 M7.0.2a-g (F-110)** — controller-side wire-up + server-side grammar arg + build-time smoke-gate + first-boot phase + bounded retry loop with QB-consult rescue for per-tool violations that survive the generic grammar. Per-tool GBNF tightening deferred to v6.17.

---

## Consolidated by severity

### Tier A — Whitepaper claims that are **flatly false today** (both editions)

**As of 2026-08-06**: items #1 and #2 shipped in v6.16; item #3 (OC edition INV-1 gap) scheduled for v6.18 (M7.6a-1).

| # | Claim | Reality | v6.16 status |
|---|---|---|---|
| **1** | "The AI cannot make irreversible mistakes — COW imagination layer" (§5) | Pre-v6.16: `src/mcpd/src/sandbox/cow.rs` **did not exist**. mcpd returned a `requires_cow_approval` ticket; no tmpfs/overlayfs mount ever happened. Neither edition showed a real dry-run diff at the approval gate. This was the flagship safety feature of the whitepaper — it was a stub. (C-1) | **SHIPPED → F-109** (M7.0.1a-h). Simulation-based per user design 2026-08-03 (walkdir+du for fs.delete; stat for fs.write; apt-get -s for package.*) — user-outcome-identical to literal overlayfs without moving `SYS_mount`/`SYS_umount2`/`SYS_unshare` from seccomp DENY→ALLOW. Whitepaper §5 text update deferred to M7.6b to acknowledge the implementation approach. |
| **2** | "The model literally CANNOT generate invalid output — mathematically impossible" (§6) | Pre-v6.16: PB launched WITHOUT `--grammar-file` from the controller side; server-side `start_pb.sh --grammar-file` **silently degraded to unconstrained** if the file didn't resolve. Only the QB got grammar-bound. (C-4, B-4) | **SHIPPED → F-110** (M7.0.2a-g). Controller-side + server-side wired end-to-end + build-time smoke-gate refuses ISO if grammar file missing + first-boot warn-not-crash. Bounded PbRetryLoop with QB-consult rescue catches per-tool schema violations that survive the generic grammar. Per-tool GBNF tightening (enumerate 25 tool names) deferred to v6.17. |
| **3** | "QB has zero direct access to system execution tools" (INV-1, §3) | Pre-v6.17: OC edition QB (Gemini via opencode) invoked mcpd + iceui tools directly. No Intent Object, no Controller, no PB, no schema/risk/HITL, no verifier, sentinel audit rows (`session_id="opencode-oc"`, `turn_index=-1`, `tokens=0`, `cost=0`). (A-1..A-8) | **SHIPPED → F-111** (v6.17 M7.6a-1a-i, pulled forward from planned v6.18). opencode locked to ONE tool (`iceui_submit_intent`); Gemini emits an Intent Object; new daemon RPC `intent.run` receives it; new `Controller.run_turn_from_intent` skips Step 1 and drives the full 11-step pipeline (schema/risk/COW/HITL/PB grammar/retry/verifier/dispatch/summarise); real Gemini QB wired via `GEMINI_API_KEY` for verifier + summariser + repair; audit deconfliction via IntentStore UUID ring prevents double-writes. |

### Tier B — Whitepaper claims that are **partial or drifted** in current edition too

**As of 2026-08-06**: item #5 (B-2 + B-3) shipped as part of v6.16 M7.0.1e/f. Items #4 (B-1) + #6 (B-5) remain open — both scheduled for v6.19 M7.6b (whitepaper §3/§4/§6 realignment + R14 backfill).

| # | Claim | Reality | v6.16 status |
|---|---|---|---|
| **4** | PB "receives only opaque reference IDs" (INV-1) | PB gets `intent_id + target + expected_content + pb_hint` (F-27, F-41). Documented in code as a "rich envelope"; whitepaper §3 line 152 was never updated to match. (B-1) | **OPEN** — whitepaper text realignment scheduled for v6.19 M7.6b (code is the source of truth; whitepaper §3 needs the rich-envelope acknowledgement). |
| **5** | HITL Tier-3 prompt shows dry-run diff (§8.2) | Pre-v6.16: Tier-3 gate carried `cow_summary=None`. Dry-run only in a **second** gate after mcpd, and only on streaming path. AgentGraph path had NO COW branch at all. (B-2, B-3) | **SHIPPED → F-109** (M7.0.1e streaming Step 3.5 pre-pass + M7.0.1f AgentGraph `cow_preview_node`). Real diff at initial HITL gate on BOTH paths. Second post-mcpd modal retained as fallback for pre-v6.16-mcpd operator downgrade. |
| **6** | GUI/RPA flows through mcpd (§4 tool domains, §6 arch diagram) | AgentGraph `mcpd_dispatcher_node` bypasses mcpd for `gui.*`/`rpa.*`, dispatches to `controller.gui_worker` directly. Streaming path still uses mcpd. Two paths, two rules. (B-5) | **OPEN** — subprocess isolation preserves the sandbox invariant; whitepaper diagram is over-specific about transport. Scheduled for v6.19 M7.6b whitepaper edit (documents the AgentGraph subprocess route as compliant per INV-5/6 discipline). |

### Tier C — Whitepaper claims that are **entirely aspirational** (not started)

| # | Claim | Reality |
|---|---|---|
| **7** | INV-8 hash-chained audit "captures every intent including rejected" | Current edition: enforced. **OC edition: `iceui_*` tool calls have ZERO audit write path** — `dual-brain/controller/mcp_gui_server.py` has 0 `AuditLog` references. (A-6) |
| **8** | Speculative decoding "must be implemented from day one" (§12 F2) | PB has no draft model shipped; QB has opt-in via TOML but no `[pb]` section in example config. Effectively off. (C-3) |
| **9** | GPG-signed ISO (§9, §10 M7.7) | Not wired anywhere. (C-9) |
| **10** | Phase 7 M7.2–M7.7 (Playwright + MS Graph + Google Workspace MCPs, analytics.chart, hardening, v1.0 release) | Not started. Only M7.1 pre-work (trust field in schemas + empty allowlist toml). (C-10) |
| **11** | Phase 8 (Goal state, self-verification, sub-agents, LangGraph adapter) | Not started. `agent_graph_bridge` exists but is single-turn plumbing. (C-11) |
| **12** | KPI benchmark suites (§13 KPI 1, 4, 5) | KPI 3 (FEH) has a runner. KPI 1 latency: only mcpd tools/list p95. KPI 4 boot: timer, no gate. KPI 5 Tier-3/day: nonexistent. (C-2, C-8, C-12, C-13) |

### Tier D — Departures that are **documented and deliberate** (not violations)

- **live-build → debootstrap+xorriso** (C-7): documented in `cx-distro/build.sh:549-550` — live-build broken on Noble. Fine.
- **QB is cloud Gemini, not local Phi-4-mini** (whitepaper §3 v1.1 revision): explicitly acknowledged in the whitepaper itself. Fine.
- **sandlock/CX Linux/cortexd** (C-14): §14 references, we use direct `landlock` crate. Fine.

---

## Honest summary in one paragraph

The **current edition** implements roughly 60% of the whitepaper's dual-brain promise: schema validation, risk classification, blocking HITL, opaque IDs, hash-chained audit all fire — but the *content* of intents leaks past the "opaque ID only" boundary (F-41 rich envelope), and the flagship COW dry-run "imagination layer" is a stub that just returns a ticket. The **OC edition** implements roughly 15% — the local PB is packaged but idle, the Controller is bypassed, and only mcpd's audit log is preserved (iceui tool calls have zero audit trail). The **PB itself** was supposed to be grammar-bound at every token so it *couldn't* emit invalid tool calls — that's silently degraded to "grammar if the file happens to be found." Phase 7 M7.2–M7.7 (the "broader tools + hardening + v1.0" milestones the roadmap promised for weeks 24-38) are not started; we're currently at v6.15 doing vision-grounding, which the whitepaper roadmap doesn't mention at all — a feature detour ahead of the promised hardening work.

Nothing here says "abandon and start over." A lot is either correctable in a few PRs (COW real overlay, PB grammar mandate, iceui audit sink), or is honest scope reduction that should be reflected in the whitepaper (rich envelope, GUI subprocess bypass). The OC edition question that triggered this audit is real, but it's the **most visible** departure — not the **only** one.

---

## Directions the reviewer will need to pick from (recorded but not yet chosen)

When this audit is reviewed, the choices likely to surface are:

- **Restore INV-1 inside OC edition** — opencode/Gemini gets ONE tool (`submit_intent(intent_object)`); Python daemon receives, routes to local PB, PB picks the real MCP tool call, mcpd executes. Preserves opencode UX; largest engineering cost.
- **Kill OC edition** — current edition becomes the sole shipping product. Simplest; loses opencode polish and the Fix V vision UX currently wired only through iceui.
- **Honest labels** — keep OC as a "convenience/preview" mode with a first-boot banner declaring "INV-1 not enforced in this mode"; current edition stays as the "secure" mode. Cheapest — documentation + banner + release-notes change.
- **Hybrid** — opencode as TUI shell only; user text relays to daemon → PB → tools → daemon → opencode narrates. Preserves opencode UX and honors INV-1 but Gemini's own model turns are minimized.

These are placeholders for the reviewer's convenience. The choice is not made by this document.

---

## Provenance

- Audit compiled by three parallel Explore agents against the codebase at `feat/v6.15-vision-grounding` HEAD on 2026-08-03.
- Every `file:line` in this doc was cited by one of the agents. No inferred rows.
- Whitepaper reference: `AI_Native_OS_Whitepaper.md` v1.1 (revision date 2026-07-09), 991 lines.
- No changes to code, config, or ISOs were made as part of producing this document.
