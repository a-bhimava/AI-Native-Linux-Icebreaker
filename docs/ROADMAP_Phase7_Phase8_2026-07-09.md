# Icebreaker Roadmap — What's Left, Phase 7 (Broader-Tools + Hardening + v1.0), Phase 8 (Autonomous Agents)

## Context

**Why now**: v6.61-arm64 has been built and downloaded (SHA `f8ef6120…c98b`) with the QB→PB rich-envelope rewrite that fixes the CSV bug + five other Phase-5-era prompt failures. UTM verification is pending but the underlying pipeline is landed and CI-green. That closes the *last* content-authoring bug on the roadmap and clears the way to plan the tool-surface work you've been asking about.

You've explicitly steered the roadmap this session:
- **Phase 7 gains broader tools directly** (was strictly hardening + v1.0).
- **Phase 8 is autonomous agent loops** (was undefined in codebase docs).
- **External MCP servers are consumed** — but only from a small curated allowlist, with pinning + egress sandbox + risk-tier labels.

This document lays out (1) the 2026 SOTA landscape that this plan sits inside, (2) what's on the floor after v6.61, (3) the Phase 7 scope with concrete milestones, (4) the Phase 8 scope, and (5) sequencing.

---

## 1. 2026 SOTA landscape (condensed input from web research)

The research pass covered protocol state, browser/office ecosystems, trust models, and agent orchestration. The pieces that matter for our planning:

- **MCP is no longer Anthropic-only.** Governance moved to the Agentic AI Foundation (Linux Foundation) in Dec 2025. Spec `2025-11-25` is current; June 2026 adds stateless transport + session mgmt. Official registry hit ~9,600 servers by May 2026. Two-tier trust model is *not* in the spec — that's our opportunity to publish an extension.
- **Trust is downstream, not spec.** NSA/CISA MCP Security Info Sheet (June 2026) recommends incremental scope consent + sandboxed egress + cryptographic attestation. This aligns almost exactly with our INV-4/INV-5/INV-6/INV-7 stack. **Icebreaker's risk-tier + COW + HITL is genuinely ahead of the ecosystem.**
- **Office won the "cloud API + MCP wrapper" pattern.** MS Graph via Microsoft 365 Agents Toolkit MCP server (GA April 2026). Google Workspace via `taylorwilsdon/google_workspace_mcp`. AT-SPI/UIA fell to fallback-only for local-only apps. That validates our Phase 6T `libreoffice.py` App API (LibreOffice has no cloud API) *and* argues that Word/Excel should enter via an external MCP allowlist, not our own AT-SPI clone.
- **Browsers: Playwright MCP won.** ~92% vs Computer Use's ~78% on shared benchmarks. Chrome DevTools MCP complements it for observation. Anthropic ships an official Claude "Playwright Plugin".
- **Split-brain is now called SLM-first.** Industry frame is cost/latency ("80–90% of turns stay local"). Ours is *security* (QB=untrusted input isolation, PB=privileged execution). The trust-tier moat is real; Phase 7 messaging should lead with it.
- **Agent orchestration**: LangGraph is the production standard for stateful workflows; Anthropic's Claude Agent SDK is the deepest-integrated. Phase 8's agent runtime should study TDP (Task-Decoupled Planning) and CODA — both 2026 architectures with published 60–82% token reductions.

Full research notes at `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf-agent-aff1b5afe478ab6ef.md`.

## 2. What's on the floor after v6.61

**Immediate (weeks, before Phase 7 kickoff):**
1. **UTM boot + NL sweep of v6.61** to confirm bugs #1, #3, #4, #5, #6 are fixed (the plan-mode-approved changes all landed offline; this is the acceptance test).
2. **Bug #2 (HITL `signal only works in main thread`)** — Python threading fix in the HITL presenter. Move to `loop.add_signal_handler`. Not in v6.61.
3. **Bug #7 (search intent gap)** — `fs.find` intent + mcpd tool + corpus entries. Not in v6.61.
4. **Test-suite drift**: the pre-existing `test_audit.py::test_all_outcome_enum_values_present` failure (expected set out-of-date since F-35 added `Outcome.unsupported`) and the `test_systemd_units.py` "no display" failure — small fixes, unrelated to v6.61.
5. **v6.6-amd64 regression rebuild** (task 36 still `in_progress` from earlier session) — the amd64 ISO built with the refactored scripts hasn't been gated yet.

**Deferred out of Phase 6T that Phase 7 will pick up:**
- Bare-metal GUI/RPA end-to-end tests (per `IMPLEMENTATION_PLAN.md:84`)
- CDP WebSocket live tests against real Chromium/Firefox
- Robot Framework keyword coverage past today's 36
- mcpd rollback RPC for undo
- Socket activation
- GTK live rendering tests
- arm64 GUI Agent AT-SPI under UTM (arm64 QEMU is green; arm64 UTM GUI check still pending per R11)

**Nice-to-have surface (deferred to Phase 7 as scope allows):**
- `fs.append`, `fs.copy`, `fs.move`, `fs.mkdir` — filesystem completeness
- `system.reboot`, `system.shutdown` (Tier 3, HITL) — full lifecycle
- `network.ping`, `network.dns.query` — active network ops (Tier 1)
- `analytics.summarize`, `analytics.chart` — visualization (natural QB→PB split: QB produces the spec, PB executes / renders)

## 3. Phase 7 — Broader Tools + Hardening + v1.0 GPG-signed release

**Duration estimate**: 10-14 weeks (vs. the original 4-6 for strict release). You explicitly accepted the delay in exchange for shipping v1.0 with a genuinely broader tool surface.

**Milestones** (each is a mergeable, gate-checked deliverable):

### M7.1 — Trust-tier taxonomy formalized + MCP extension draft (2 wk)
- Move our risk_level / tier assignment out of `main.py` and into a small standalone spec (`docs/spec/trust-tiers.md`).
- Draft an MCP extension: `x-icebreaker-trust`, a per-tool JSON field (`{ tier: 0|1|2|3, reversible: bool, requires_hitl: bool, requires_cow: bool }`). Publish to the LF Agentic AI Foundation as a public RFC — we get community input and positioning.
- Ship a corresponding schema check in `mcpd/schemas/*.json`: every tool must declare this or fail startup.

### M7.2 — Curated external MCP allowlist infrastructure (3 wk)
- Add a `mcp_allowlist.toml` shipped in `cx-distro/distro/`. Format: `{name, git_url, commit_sha, sha256, tier_ceiling, egress_hosts}`. Ship with 4 entries initially (Playwright MCP, Chrome DevTools MCP, MS Graph MCP, `google_workspace_mcp`).
- Extend `mcpd` to spawn external MCP servers via child stdio (matching MCP spec). Add: process-level Landlock (server can only access declared paths), seccomp filter (deny fork/exec beyond the initial subprocess), egress firewall via `nftables` (server can only reach declared hosts + ports), Tier ceiling enforcement (external server can't produce Tier < 2 intents that skip HITL).
- New CI gate: `external-mcp-integrity.sh` verifies SHA + Git SHA + egress rules for every allowlist entry.

### M7.3 — Browser (Playwright MCP integration) (2 wk)
- Consume Playwright MCP as the FIRST external allowlist entry. Ship default HITL prompt for every browser interaction (Tier 2 auto-execute-with-audit inside the current tab; Tier 3 lockout for new tabs / navigation to external domains).
- Update Firefox App API (`gui_agent/app_apis/firefox.py`) to prefer Playwright MCP over CDP direct where available; CDP + AT-SPI stay as fallback.
- Golden intent corpus rows: "screenshot the current page", "click the login button", "fill this form with X".

### M7.4 — Office (MS Graph MCP + Google Workspace MCP) (3 wk)
- Second and third allowlist entries. Both require OAuth 2.1 flow (per spec 2025-11-25) — implement via `pkexec ib-setup-oauth --provider={ms365|google}`, mirroring the existing `ib-setup-key` pattern.
- Store OAuth tokens in `/etc/icebreaker/secrets/oauth-{provider}.token`, root-only, forwarded to the MCP server subprocess via systemd credentials.
- Golden intent corpus rows: "make a Word doc listing my project files", "create an Excel sheet with columns name, size, date from my Downloads", "share the Word doc with team@company".
- Native cloud APIs beat AT-SPI here; we keep LibreOffice App API but MS Word / Excel are now available even if LibreOffice isn't installed.

### M7.5 — Analytics + visualization (`analytics.chart`, `analytics.summarize`) (2 wk)
- Two new mcpd tools that fit the QB↔PB split perfectly:
  - `analytics.summarize(data)` — QB gets structured tool output, produces a 1-3 sentence natural-language summary. Already the pipeline; formalize as a tool.
  - `analytics.chart(data, kind)` — QB produces a chart spec (Vega-Lite JSON) with data embedded from a prior tool result. PB validates the spec is well-formed. mcpd renders to SVG (via `vega-cli` shipped in v6 packages) into the Terminal's companion pane.
- Corpus rows: "show disk usage as a pie chart", "chart file sizes in this folder as bars", "summarize the last hour of nginx logs".

### M7.6 — E2E hardening + pen-test (2 wk)
- Bare-metal GUI/RPA end-to-end tests (Phase 6T deferred item).
- Adversarial pen-test pass: prompt injection through Word doc content, sandbox escape from external MCP server, OAuth token exfiltration attempts, browser MCP navigating to malicious URLs. Contract a third-party or run internally with a documented threat model.
- p95 latency profiling under the expanded surface. Target: <500ms for Tier 0 tools, <15s for Tier 1 writes with content (already met), <30s for Tier 2 external MCP calls.

### M7.7 — Docs + v1.0 release (2 wk)
- User guide, developer API reference, security architecture overview.
- ISO signed with a fresh GPG key documented in `docs/security/release-signing.md`.
- Publish INV-1 through INV-8 as public architecture claims — this is our security-first differentiator (the SLM-first pattern the industry ships is a cost story; our version is a trust story).

**Phase 7 exit criteria**: 30+ mcpd tools (23 existing + 4 external MCP wrappers + 3+ new in-house) + all Phase 6T deferred items closed + pen-test report + `icebreaker-v1.0-amd64.iso` and `icebreaker-v1.0-arm64.iso` GPG-signed and published.

## 4. Phase 8 — Autonomous Agent Loops

**Framing**: Icebreaker becomes an *agent runtime*, not just a natural-language command translator. Multi-turn planning, sub-goal decomposition, self-verification.

**Reference architectures** (both 2026): **TDP (Task-Decoupled Planning)** for sub-goal scoping (published 82% token reduction); **CODA (Cerebrum-Cerebellum)** for strategic-vs-tactical split (aligns with our QB↔PB semantic-vs-structural split).

### M8.1 — Multi-turn goal state (3 wk)
- Extend `Session` (dual-brain/controller/session.py) with a `Goal` object: `{intent, plan_steps, executed_steps, remaining_steps, verification_criteria}`.
- QB emits a plan when the user's request maps to more than one atomic action ("install nginx AND start it AND show me the logs" — today `system.unsupported`; tomorrow a 3-step plan).
- HITL applies at plan-approval time (whole plan, not per step) unless a step escalates tier mid-plan.

### M8.2 — Self-verification loop (2 wk)
- After each executed step, QB verifies the step's output matches the sub-goal's expected outcome (existing `qb_verifier` prompt extended).
- On mismatch: re-plan, retry step, or escalate to human. Bounded retries per step + per plan.

### M8.3 — Sub-agent surface (3 wk)
- Introduce "roles" that are QB-configured for a task: `research_agent` (browser + summarize), `data_agent` (analytics + read + chart), `office_agent` (Word + Excel + email). Each role gets a tool subset from the M7.2 allowlist.
- Roles are labeled with the *maximum* risk tier they can produce. `research_agent` is Tier 1 read-only; `office_agent` is Tier 2 with HITL; nothing autonomous ever gets Tier 3.

### M8.4 — LangGraph adapter (2 wk)
- Expose Icebreaker's execution surface as a LangGraph-compatible tool set. External LangGraph workflows can execute on Icebreaker under our trust-tier + HITL rules. This positions Icebreaker as a *constrained execution runtime for agent frameworks*, not a competitor.

### M8.5 — Fleet-safe defaults (2 wk)
- Multi-agent autonomous loops require observability. Ship: session-level audit graph (steps + verifier votes + HITL decisions as a DAG), cost accounting per plan (not just per turn), rate limiting per role.
- Deferred to Phase 8 not Phase 7 because Phase 7's v1.0 hardening focuses on single-turn correctness first.

**Phase 8 exit criteria**: Icebreaker can execute a 3-4 step plan without human intervention within HITL boundaries; sub-agent role system in production; LangGraph adapter published + one worked example in `docs/examples/langgraph-icebreaker.md`.

## 5. Cross-cutting decisions

- **Every new tool declares `x-icebreaker-trust` in its schema.** No exceptions. Ship a build-time check that rejects a tool without it. Applies to in-house AND allowlisted external tools.
- **External MCP servers run as unprivileged subprocesses of `mcpd`.** They inherit no capabilities, get scoped Landlock roots for read/write, and hit an nftables egress allowlist. The `mcp_allowlist.toml` is the single source of truth for what's allowed to run.
- **OAuth 2.1 tokens live in `/etc/icebreaker/secrets/` root-only.** Forwarded to child MCP processes via systemd credentials, never via env vars.
- **Corpus grows with every new tool.** Non-negotiable — no PR merges without corpus rows for every new intent phrasing.
- **Backwards compatibility on the intent JSON**: `content` and `pb_hint` are optional; older ISOs keep working with the new controller.

## 6. Recommended sequencing

**Weeks 1-2 (immediate)**: UTM verify v6.61; fix bug #2 (HITL threading), bug #7 (`fs.find`); fix the two stale test-suite failures; complete task 36 (v6.6-amd64 regression build + gate).

**Weeks 3-14 (Phase 7)**: Milestones M7.1 → M7.7 in order. Each milestone is a mergeable branch; the ISO version bumps at each merge (v6.7, v6.8, v6.9, v6.10, v6.11, v6.12, v6.13 → v1.0). This preserves the incremental-verify model we've been using through V6.

**Weeks 15-26 (Phase 8)**: Milestones M8.1 → M8.5. This is post-v1.0 work; ISOs during this period are v1.1, v1.2, etc.

**Non-goals across both phases**:
- Do NOT retrain the Privileged Brain during Phase 7 — the current `run7_cot_q4km.gguf` is proven. Retraining is Phase 8+ if we choose SLM distillation later.
- Do NOT expand QB tool access beyond the allowlist during Phase 7. INV-1 stays intact until Phase 8 sub-agent surface lands with explicit role labeling.
- Do NOT introduce fleet management or multi-instance sync in Phase 7 or 8. That becomes Phase 9 or a Phase 8 sub-track only if a customer explicitly asks.

## 7. Files this planning touches

Nothing to modify yet — this is a roadmap document. When Phase 7 kickoff arrives:

- `AI_Native_OS_Whitepaper.md` — add Phase 7 (revised) and Phase 8 sections.
- `docs/IMPLEMENTATION_PLAN.md` — replace old Phase 7 (Hardening only) with the M7.1-M7.7 scope. Add Phase 8 section.
- `incremental/GROUND_TRUTH.md` — add rules R12 (external-MCP allowlist enforcement) and R13 (trust-tier declaration required).
- `CLAUDE.md` — phase status table + add file paths for `docs/spec/trust-tiers.md`, `cx-distro/distro/mcp_allowlist.toml`.

## Verification

This is a strategy document, not a code change. Verification is: (a) the user agrees Phase 7 as scoped here is a shippable v1.0 and (b) Phase 8 as scoped is a coherent "agent runtime" milestone. Sign-off = plan approval, not a test run.
