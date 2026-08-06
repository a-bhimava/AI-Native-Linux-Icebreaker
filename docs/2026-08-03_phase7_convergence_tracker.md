# Icebreaker Phase 7 Convergence Tracker

**Product:** Icebreaker (AI-native Ubuntu fork)
**Sprint:** Phase 7, augmented with post-audit milestones (M7.0 / M7.6a / M7.6b)
**Baseline:** v6.15 arm64 (2026-08-03, both editions)
**Plan drafted:** 2026-08-03
**Target ISOs:** v6.16 → v6.25 → v1.0
**Related audit:** [`docs/2026-08-03_whitepaper_vs_reality_audit.md`](2026-08-03_whitepaper_vs_reality_audit.md) (26 whitepaper departures)
**Related roadmap:** [`docs/ROADMAP_Phase7_Phase8_2026-07-09.md`](ROADMAP_Phase7_Phase8_2026-07-09.md) (original M7.1–M7.7 definitions)

---

## Why this tracker exists

The 2026-08-03 audit found 26 departures between the whitepaper and shipping code. 22 of those departures were not covered by the current Phase 7 roadmap. Three of them (COW real overlay, PB grammar-constrained decoding, OC-edition INV-1 restoration) are "flatly false" whitepaper claims — safety-user-facing invariants that any reasonable reader would assume are enforced today. This tracker folds the audit into Phase 7 by adding three new milestones (M7.0, M7.6a, M7.6b) that close the gap, and locks the shipping order so the safety invariants land before any expanded tool surface.

Existing M7.1–M7.7 are preserved verbatim from the roadmap; only sequenced differently and paired with the new milestones. Nothing is deleted from the roadmap.

---

## The augmented Phase 7 milestones (11 total)

### M7.0 — Safety-invariant closure (NEW, 2026-08-03)

**Why first**: whitepaper §5 promises "the AI cannot make irreversible mistakes"; §6 promises "the model literally CANNOT generate invalid output." Both are user-facing safety claims and both are stubs today. Fix these before the "broader tools" milestones (M7.2+) so any external MCP we add (Playwright, MS Graph, Google Workspace) inherits real COW + real grammar-bound PB.

**Ships across**: v6.16 (M7.0.1 + M7.0.2), v6.17 (M7.0.3). ~2-3 weeks total.

#### M7.0.1 — COW real overlay (audit C-1 / Tier A #1)

- **What ships:** an actual overlay filesystem mount for `fs.delete`, `fs.write` outside `$HOME`, and `package.*` mutations. Diff surfaced to Controller HITL as `cow_summary`. Ticket-only `requires_cow_approval` path retired.
- **How it's built:** new `src/mcpd/src/sandbox/cow.rs` implementing tmpfs/overlayfs mount before dispatch, filesystem diff capture, atomic commit/rollback. Modify `src/mcpd/src/tools/fs.rs:215,238,258-278` and `src/mcpd/src/tools/package.rs:95-105`. Controller-side: `main.py:840-845` picks up the diff on the initial Tier-3 gate (removes B-2's "second gate after mcpd" pattern). `agent_graph_nodes.py:742-879` gains the same COW branch (closes B-3).
- **Where in code:** `src/mcpd/src/sandbox/cow.rs` (NEW), `src/mcpd/src/tools/{fs,package}.rs` (edit), `dual-brain/controller/main.py`, `dual-brain/controller/agent_graph_nodes.py`.
- **Regression lock:** `src/mcpd/tests/test_cow_real_overlay.rs` + `dual-brain/controller/tests/test_hitl_cow_summary_present.py` + smoke-gate assertion that `sandbox/cow.rs` exists in the binary.

#### M7.0.2 — PB grammar mandate (audit C-4 + B-4 / Tier A #2)

- **What ships:** the PB CANNOT boot without GBNF-constrained decoding. No silent-fallback. Whitepaper §6 guarantee ("mathematically impossible to emit invalid output") becomes real.
- **How it's built:** (a) `dual-brain/controller/__main__.py:170-178 _build_pb()` reads `grammar_path` from config; passes to `LlamaCppLocalBackend`. (b) `LlamaCppLocalBackend.__init__` refuses to construct if `grammar` is None on the PB path (raise, don't degrade). (c) `dual-brain/scripts/start_pb.sh:62-65` exits non-zero (not warn+skip) if the grammar file doesn't resolve. (d) `incremental/build/smoke-gate.sh` asserts `--grammar-file` in the PB process argv on first boot.
- **Where in code:** `dual-brain/controller/__main__.py`, `dual-brain/controller/backends/llama_local_backend.py`, `dual-brain/scripts/start_pb.sh`, `incremental/build/smoke-gate.sh`.
- **Regression lock:** `dual-brain/controller/tests/test_pb_grammar_mandatory.py` + smoke-gate assertion `_pb_grammar_argv_present`.

#### M7.0.3 — iceui audit sink (audit A-6 + A-7 / Tier C #7)

- **What ships:** every `iceui_*` tool call (all 24 Fix V vision + AT-SPI tools on OC edition) is written to the same hash-chained INV-8 audit log the current-edition controller uses. Fields (`session_id`, `turn_index`, `model`, `tokens_in`, `tokens_out`, `cost`) carry real values, not sentinels.
- **How it's built:** `dual-brain/controller/mcp_gui_server.py` grows `AuditLog.write_fields()` calls at each `tools/call` entry/exit (currently 0 refs). `oc_audit_bridge.py:22-27,158-175` stops hard-stamping and reads real fields from an in-band envelope.
- **Where in code:** `dual-brain/controller/mcp_gui_server.py`, `dual-brain/controller/oc_audit_bridge.py`.
- **Regression lock:** `dual-brain/controller/tests/test_iceui_audit_written.py` + G-gate integration test asserting audit file grows by one hash-linked line per iceui tool call.

---

### M7.1 — Trust-tier taxonomy + `x-icebreaker-trust` MCP extension (from roadmap, PARTIAL)

**Status coming in**: schema field present in 24 mcpd tool schemas (Scope G work); R13 startup enforcement NOT shipped.

**What still needs to ship**:
- `docs/spec/trust-tiers.md` (canonical spec, submittable as public RFC to LF Agentic AI Foundation)
- R13 enforcement: mcpd refuses to start if any tool schema omits `x-icebreaker-trust`
- Ships alongside M7.0.3 in v6.17

**Regression lock**: `src/mcpd/tests/test_r13_startup_refuse.rs`.

---

### M7.6a — OC edition INV-1 restoration (NEW, 2026-08-03; audit A-1..A-9 / Tier A #3)

**Why after M7.0 but before M7.2+**: adding external MCPs (Playwright / MS Graph / Google Workspace in M7.3/M7.4) inherits the OC-edition INV-1 gap — Gemini invokes them directly with the same "opencode ask" flow that today lets it fire `fs.delete` unmediated. Restore INV-1 before the surface expands.

**Blocking decision** (user picks before M7.6a-1 lands): which of the audit's 4 candidate directions?

- **Option A (recommended)**: Restore INV-1 inside OC — opencode/Gemini gets ONE tool `submit_intent(intent_object)`; the Python daemon receives, drives the local PB, PB emits real MCP calls to mcpd/iceui. Preserves opencode UX; the shipped `run7_cot_q4km.gguf` finally earns its ISO byte-count.
- **Option B**: Kill OC edition — current edition becomes sole shipping product.
- **Option C**: Honest labels — first-boot banner declaring "INV-1 not enforced in OC". Ships as v6.17.
- **Option D**: Hybrid — opencode as TUI shell only; user text relays to daemon → PB → tools → daemon → opencode narrates.

**Ships across (Option A)**: v6.18 skeleton, v6.19 parity.

#### M7.6a-1 — `submit_intent` MCP + daemon route (v6.18 skeleton)

- **What ships:** `cx-distro/distro/qb_oc.json.template` exposes exactly one MCP server (`icebreaker-controller`) with one tool (`submit_intent`). `icebreaker` (mcpd) and `iceui` disappear from opencode's visible surface. New `dual-brain/controller/mcp_submit_intent_server.py` receives intents from opencode, validates schema → risk classify → HITL → route to local PB → PB emits actual tool call to mcpd/iceui → combined result envelope back to opencode.
- **Where in code:** `dual-brain/controller/mcp_submit_intent_server.py` (NEW), `cx-distro/distro/qb_oc.json.template`, `incremental/build/gen-oc-config.sh`.
- **Regression lock:** `dual-brain/controller/tests/test_submit_intent_route.py` + G-gate assertion that OC-edition mcpd stdio pipe sees no direct `tools/call` from opencode (only from the daemon).

#### M7.6a-2 — Vision + AT-SPI feature parity via submit_intent (v6.19)

- **What ships:** all 24 Fix V vision + AT-SPI tools work through the submit_intent route, not the direct iceui exposure. Trust store consulted at the daemon level (not opencode's `ask`). HITL modal shows COW dry-run + annotated preview from V.5b presenter (now that the caller-wiring gap V.6b flagged has a real caller — the daemon).
- **Where in code:** `dual-brain/controller/mcp_submit_intent_server.py` (grow), `dual-brain/gui_agent/agent.py` (bypass opencode-shaped return path), `dual-brain/controller/mcp_gui_server.py` (deprecate `_handle_tools_call` direct dispatch — kept as legacy for a version, gated by a config knob).
- **Regression lock:** `dual-brain/gui_agent/tests/test_vision_via_submit_intent.py` + updated V.7 live tests.

**Fallback**: if Option A proves too invasive during M7.0, ship Option C (honest labels) as v6.17 to close the visible gap while we scope Option A properly. Decide at end of M7.0.

---

### M7.6b — Whitepaper realignment + R14 backfill (NEW, 2026-08-03; audit B-1 / B-2 / B-3 / B-5 / C-17)

**Why paired with M7.6**: the hardening pass is the natural moment to reconcile "code is correct, whitepaper is stale" vs "code drifted from a real invariant, must fix code." Whitepaper should be stable enough to publish alongside v1.0 (M7.7).

- **B-1 (rich envelope)**: whitepaper §3 line 152 says "opaque reference ID only"; code ships `intent_id + target + expected_content + pb_hint` since F-27/F-41. **Recommendation**: update whitepaper §3 to acknowledge the envelope. The envelope is load-bearing (F-41 fixes CSV content-hallucination); reversing would break the flagship content-writing UX.
- **B-2 (Tier-3 HITL has no dry-run)**: closed automatically by M7.0.1 (real COW ships diff at first gate).
- **B-3 (AgentGraph has no COW branch)**: closed automatically by M7.0.1.
- **B-5 (AgentGraph bypasses mcpd for gui/rpa)**: whitepaper §4/§6 diagram shows all tools through mcpd. Real state: gui/rpa live in Python daemon, mcpd is Rust. **Recommendation**: update whitepaper — the isolation guarantee (subprocess + sandbox) is the invariant, not the transport language. Alternative (Rust port of gui/rpa) is 10× the effort for zero user-facing benefit.
- **C-17 (R14 gap)**: 46 of the 94 F-xx rows lack a `regression lock (R14):` annotation. Backfill sprint: identify the surviving test/marker/gate for each, cite it inline. Any row where no lock exists → write one or explicitly retire the fix.

**Ships across**: v6.19 whitepaper text edits + architecture diagram rebase + F-xx backfill.

**Regression lock**: `test_whitepaper_departures_have_dispositions.py` — walks `incremental/GROUND_TRUTH.md § 10` and asserts every row has a resolved disposition or a live milestone link.

---

### M7.2–M7.7 (from roadmap, unchanged)

M7.2 external MCP allowlist + spawner (v6.20). M7.3 Playwright MCP (v6.21). M7.4 MS Graph + Google Workspace MCP + OAuth 2.1 (v6.22). M7.5 `analytics.chart` + `analytics.summarize` + `fs.find` (v6.23). M7.6 hardening + adversarial pen-test + p95 latency (v6.24 — inherits M7.0's COW and grammar guarantees so pen-test hits real invariants). M7.7 docs + GPG-signed ISO + v1.0 tag (v6.25 → v1.0).

See `docs/ROADMAP_Phase7_Phase8_2026-07-09.md` for full definitions.

---

## Locked shipping sequence (v6.16 → v1.0)

| ISO | Milestones landing | Audit rows closed |
|---|---|---|
| **v6.16** | M7.0.1 COW real overlay + M7.0.2 PB grammar mandate | C-1, C-4, B-2, B-3, B-4 (5, incl. Tier A #1 + #2) |
| **v6.17** | M7.0.3 iceui audit sink + M7.1 R13 enforcement + (fallback) M7.6a Option C honest banner if Option A blocked | A-6, A-7, C-10 remainder |
| **v6.18** | M7.6a-1 submit_intent skeleton (assumes Option A) | A-1, A-2, A-3, A-4, A-5, A-8 (6, incl. Tier A #3) |
| **v6.19** | M7.6a-2 vision parity via submit_intent + M7.6b whitepaper realignment + C-17 R14 backfill | A-9, B-1, B-5, C-17 |
| **v6.20** | M7.2 external MCP allowlist + spawner | (foundation for M7.3-M7.5) |
| **v6.21** | M7.3 Playwright MCP | (roadmap original) |
| **v6.22** | M7.4 MS Graph + Google Workspace MCP + OAuth 2.1 | (roadmap original) |
| **v6.23** | M7.5 analytics.chart + analytics.summarize + fs.find | (roadmap original) |
| **v6.24** | M7.6 E2E hardening + adversarial pen-test + p95 profile | C-2, C-12, C-13 (partial), C-16 |
| **v6.25** | M7.7 docs + GPG-signed ISO | C-5, C-6, C-8, C-9 |
| **v1.0** | GPG-signed release, both editions | (Phase 7 exit) |

**Estimated wall clock**: v6.16 → v1.0 = 10 ISO cuts × ~1.5-2 weeks each = ~14-20 weeks. Extends the original 10-14 week Phase 7 estimate by ~2-6 weeks — the cost of restoring INV-1 + implementing the safety-invariant claims for real. Judged worth it because the alternative is shipping v1.0 with three publicly-stated safety guarantees that don't hold.

**Deferred post-v1.0** (into Phase 8 or backlog):
- C-3 speculative decoding on PB — needs a fine-tuned draft model
- C-11 Phase 8 (already Phase 8)
- C-14 sandlock/CX Linux/cortexd — deliberate departure, document only
- C-15 The Stack + man pages training data — training-data hygiene sprint

---

## Progress ledger (updated in-place as commits land)

| Milestone | Sub-commit | Status | Commit SHA | ISO | Tests | Regression lock |
|---|---|---|---|---|---|---|
| M7.0.1 | COW simulation dry-run + gated commit (both paths) — 7 sub-commits (a-g) | **SHIPPED 2026-08-06** | see below | v6.16 | 247 mcpd + 2235 controller | See F-109 in `incremental/GROUND_TRUTH.md § 7` |
| M7.0.1a | mcpd cow.rs primitives (DryRunDiff + simulators + IntentStore) | shipped 2026-08-03 | `c3ac412` | v6.16 | 25 green | `src/mcpd/src/tools/cow.rs` inline tests |
| M7.0.1b | Ticket envelope extension in fs.rs + package.rs | shipped 2026-08-03 | `2568391` | v6.16 | 8 green | `src/mcpd/tests/integration.rs::fs_delete/write/package_install_returns_cow_gate` |
| M7.0.1c | cow.commit RPC + real-op commit handlers + guards | shipped 2026-08-03 | `6484ca6` | v6.16 | 8 integ + 2 unit | `src/mcpd/tests/integration.rs::cow_commit_*` |
| M7.0.1d | controller cow_summary formatter + mcpd_client extension | shipped 2026-08-03 | `99a8c71` | v6.16 | 32 green | `test_cow_summary_formatter.py` + `test_mcpd_client_unit.py::test_dry_run_diff_*` |
| M7.0.1e | streaming + non-streaming pre-pass + Step 10 rewrite | shipped 2026-08-06 | `7e1c195` | v6.16 | 1 updated | `test_e2e.py::test_tier3_hitl_deny_skips_dispatch` |
| M7.0.1f | AgentGraph cow_preview_node + interrupt payload + dispatcher commit_cow | shipped 2026-08-06 | `ab991f6` | v6.16 | 2 updated | `test_agent_graph_run.py::test_tier2_pauses/deny_short_circuits` |
| M7.0.1g | smoke-gate L6 assertions + § 10 dispositions + F-109 failure-log row | shipped 2026-08-06 | *(this commit)* | v6.16 | 0 new | smoke-gate: `strings mcpd \| grep cow.commit` + `command -v apt-get` |
| M7.0.2 | PB grammar mandate (fail-loud + smoke-gate assert) | pending | — | v6.16 | — | `test_pb_grammar_mandatory.py` |
| M7.0.3 | iceui audit sink + INV-8 field parity | pending | — | v6.17 | — | `test_iceui_audit_written.py` |
| M7.1 | R13 startup enforcement + `docs/spec/trust-tiers.md` | pending | — | v6.17 | — | `test_r13_startup_refuse.rs` |
| M7.6a (decision) | Pick Option A/B/C/D | pending | — | — | — | — |
| M7.6a-1 | `submit_intent` MCP + daemon route + opencode surface shrink | pending | — | v6.18 | — | `test_submit_intent_route.py` |
| M7.6a-2 | Vision + AT-SPI parity via submit_intent | pending | — | v6.19 | — | `test_vision_via_submit_intent.py` |
| M7.6b | Whitepaper §3/§4/§6 realignment + R14 backfill | pending | — | v6.19 | — | `test_whitepaper_departures_have_dispositions.py` |
| M7.2 | mcp_allowlist populated + external MCP spawner + integrity gate | pending | — | v6.20 | — | `test_external_mcp_integrity.sh` |
| M7.3 | Playwright MCP consumption + Firefox App API prefer | pending | — | v6.21 | — | (per roadmap) |
| M7.4 | MS Graph + Google Workspace + OAuth 2.1 | pending | — | v6.22 | — | (per roadmap) |
| M7.5 | analytics.chart + analytics.summarize + fs.find | pending | — | v6.23 | — | (per roadmap) |
| M7.6 | Bare-metal E2E + adversarial pen-test + p95 latency suite | pending | — | v6.24 | — | (per roadmap) |
| M7.7 | Docs + GPG-signed ISO + v1.0 tag | pending | — | v6.25 → v1.0 | — | (per roadmap) |

---

## Governance

- **Every commit lands under its own plan file** (per project convention). This tracker records the outcome; per-milestone plans record the design. No back-door edits without a plan.
- **R14 discipline**: every ship-row above names a regression lock. Cannot mark a row `shipped` until the lock is present in code and the test is green.
- **R17 (new, this convergence)**: added to `incremental/GROUND_TRUTH.md § 2` — every whitepaper claim shipping differently must be an active row in § 10 Whitepaper Departures Ledger, either resolved (→ F-xx / → milestone) or explicitly deferred.
- **Whitepaper edits are gated on tracker milestones landing.** Do not edit `AI_Native_OS_Whitepaper.md` § 3/§ 4/§ 6/§ 8 until M7.6b executes — otherwise the whitepaper drifts from § 10 mid-sprint.

---

## Related documents

- Audit: [`docs/2026-08-03_whitepaper_vs_reality_audit.md`](2026-08-03_whitepaper_vs_reality_audit.md)
- Original roadmap: [`docs/ROADMAP_Phase7_Phase8_2026-07-09.md`](ROADMAP_Phase7_Phase8_2026-07-09.md)
- Whitepaper: [`AI_Native_OS_Whitepaper.md`](../AI_Native_OS_Whitepaper.md)
- Ground truth: [`incremental/GROUND_TRUTH.md`](../incremental/GROUND_TRUTH.md) (§ 10 Whitepaper Departures Ledger + R17)
- Fix V rollup (progress-ledger template): [`docs/Icebreaker_v6.15_Vision-Grounded-UI-Automation_2026-08-01.md`](Icebreaker_v6.15_Vision-Grounded-UI-Automation_2026-08-01.md)
- Convergence plan file: `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
