# Phase 5 Roadmap — Production UX, Graduated Determinism & Hardening

## §1 — Context & Status

> **Status: ⬜ PLANNING — not started. Predecessors Phase 0/1/3/4 complete on `main`;
> Phase 2 (Dual-Brain Controller) built, gated (G1–G11), and merged. Phase 5 turns that
> Controller into something installable in a Linux distro: hardened against the prompt-
> injection / spoofing surface, fast and configurable at the keyboard, and plug-and-play so
> the system keeps evolving.**

Phase 5 in the whitepaper (§8, §10 Phase 5) is **User Experience & Graduated Determinism** —
"a user-facing interface that is fast for routine tasks and safe for dangerous ones." The
Tier 0–3 classifier, the HITL gate, the provenance audit log, the pluggable-backend
abstraction, and the `prompt_toolkit` REPL already exist (Phase 2). Phase 5 **completes the
graduated-determinism loop** (Tier 2 LLM review), **hardens the human gate** against the
spoofing/bypass surface, makes the whole UX **user-configurable and plug-and-play**, and
prepares the Controller for **daemon-mode deployment**.

### Build philosophy (non-negotiable for this phase)

1. **Pareto first.** Work is split into **P0 (the ~20% that delivers ~80% of value)**, then P1,
   then P2. P0 alone makes the Controller safe-to-demo and pleasant to use. Ship the vital few
   well before the long tail. *Done > complete.*
2. **Plug-and-play everywhere.** Every load-bearing layer is swappable via config with no code
   edits — extending the repo's existing registry culture (`model_registry.py` +
   `catalogue.toml`, the `BrainBackend` registry, the `HitlPresenter` ABC). New extension
   points (risk-classifier strategy, Tier-2 reviewer strategy, keymap, audit sink) follow the
   same pattern. Every new behavior sits behind a feature flag so it can roll forward/back
   independently (the `[tier2] enabled` / `fs-test-roots` cargo-feature pattern).
3. **Configurable UX.** All keybindings come from config; the numeric/mnemonic set is the
   default, not a hard-coding.

### What already exists that Phase 5 builds on (grounded in code)

| Capability | Where | Phase 5 relevance |
|---|---|---|
| Tier 0–3 rule-based classifier | `dual-brain/controller/risk_classifier.py` | Tier 2 currently auto-executes+notifies; P5 adds the **escalate-only LLM review**; classifier becomes a swappable strategy |
| HITL gate + `HitlPresenter` ABC ("swap for a GUI presenter in Phase 5") | `dual-brain/controller/hitl.py` | Hardened (SF-1/2/3), made keymap-driven; `[E]`/`[M]` stubs (lines 125-127) implemented; `[T]rust` added |
| 14-step orchestration | `dual-brain/controller/main.py` (`Controller.run_turn`) | Inserts Tier-2 review (after step 3), the `[M]odify` reclassify loop, trust-store consult; `_build_presenter()` (line 381) is the GUI seam |
| `intent_store.revise()` | `dual-brain/controller/intent_store.py` | Built but **not wired** — the `[M]odify` path activates it |
| Provenance audit log (JSONL, O_APPEND, fsync, redaction, 0o640) | `dual-brain/controller/audit.py` | Made **tamper-evident** (hash chain) + redaction strengthened + pluggable sink |
| Config loader (secret-key rejection, `SecretRef`, schema-validated TOML) | `dual-brain/controller/config.py` | Gains `[keymap]`, `[risk]`, `[tier2]`, `[audit]`, `[cost]` sections |
| Pluggable model registry / QB backends | `model_registry.py`, `catalogue.toml`, `backends/` | OpenAI + OAuth backends drop in via the existing interface |
| `prompt_toolkit` REPL w/ status line (cost shown) | `dual-brain/controller/repl.py` | Streaming, `/undo`, `/trust`, audit viewer, shared keymap |

---

## §2 — Security Findings in the Current Implementation

The single most important Phase 5 input: a code-verified audit of the human-gate and audit
surfaces. Each finding maps to a milestone and an exit gate. **`hitl.py`,
`risk_classifier.py`, and `audit.py` are security-critical files — every fix needs WF-6 two-
reviewer sign-off.**

| # | Sev | Finding (verified in code) | Fix | Wave |
|---|---|---|---|---|
| **SF-1** | High | **HITL prompt-spoofing via terminal escapes.** `hitl.py:_render` interpolates `action`, `target`, `reason`, `blocked_pattern`, `backend` **raw**; only `cow_summary` is ANSI-stripped (`HitlPrompt.__init__`). `target`/`reason` originate from QB output (influenceable by document/user content). A crafted `target` with ANSI/`\r`/`\n` can redraw the prompt — hide the real path, paint a fake `READ-ONLY` risk label, or forge the `✓ Approved` line — defeating informed consent. | M5.1 | **P0** |
| **SF-2** | High | **3-second lockout (INV-6) bypass via pre-buffered stdin.** After `lockout()`, `read_decision` enters the `select`/`readline` loop with **no stdin flush**. Bytes typed/pasted/injected during the 3 s window stay buffered; the first `readline()` consumes `"A\n"` → instant auto-approve. The lockout exists precisely to stop reflexive/automated approval. | M5.1 | **P0** |
| **SF-3** | Med | **Line-based input, not single keypress.** `read_decision` uses `readline()` (needs Enter) — the UX gap and the enabler of SF-2 (a pre-injected newline confirms). | M5.1 | **P0** |
| **SF-4** | High | **Approval fatigue structurally unmitigated.** No bounded "always allow" path, so repeated identical Tier-2 prompts train rubber-stamping — the exact failure (whitepaper F4 / KPI 5) the architecture must avoid. | M5.1 (`[T]rust`) | **P0** |
| **SF-5** | High | **Audit log append-only but not tamper-evident.** `audit.py` gives O_APPEND + per-line `fsync` + 0o640, but no integrity chain — a same-uid compromise can rewrite/delete past lines undetectably, and the log lives in the user's own `~/.local/state` trust domain. | M5.3 | **P0** |
| **SF-6** | Med | **Secret redaction heuristic + shallow.** `_redact_params` matches key-name substrings / known value prefixes, one level deep; `target`/`reason` are never redacted; unprefixed free-form secrets reach disk. | M5.3 | **P0** |
| **SF-7** | Med | **API keys live in the process environment.** Config refuses to store keys (good) but they remain in `os.environ` — readable via `/proc/self/environ` by same-uid processes and **inherited by the spawned `mcpd` subprocess**, the tool-execution process least entitled to them. | M5.P1-sec | P1 |
| **SF-8** | Low | **REPL history persists all inputs in plaintext** (`FileHistory`, `repl_history`); NL commands may carry secrets/paths. | M5.P1-sec | P1 |
| **SF-9** | Med | **No resource governance.** Unbounded user-input size → QB; no turn-rate limit; cost ceiling still deferred. An injection or runaway loop on an API backend is a cost/DoS bomb. | M5.P1-sec | P1 |
| **SF-10** | Med | **TOCTOU + QB free-text in the security display.** Classifier resolves `realpath` (P2-F12) but mcpd executes later (symlink-swap window); HITL shows QB-authored `reason` next to decision-critical facts. Decision facts must come only from structured sources (intent schema + mcpd COW preview); the resolved path must be what mcpd acts on. | M5.P1-sec | P1 |
| **SF-11** | Med | **Daemon-mode surface (future).** When the Controller becomes long-running, its IPC must be a Unix socket with `SO_PEERCRED` uid check, 0600, **no TCP (INV-3 parity)**, plus a hardened systemd unit; per-session isolation must survive multiplexing. | M5.P2-daemon | P2 |

---

## §3 — Guiding Principles & Extension Points

### Plug-and-play extension points

| Layer | Default → swap mechanism | Status |
|---|---|---|
| Models (QB/PB/draft) | `catalogue.toml` + `model_registry` resolve/verify; edit `model_id`, restart `llama-server` | **exists** (M2.5) |
| QB backends | `BrainBackend` registry; `[qb] backend = "local"|"anthropic"|"gemini"|...` | **exists** |
| **Risk-classifier algorithm** | new `classifier` registry; `[risk] strategy = "rules"` (default) → `"ml"`/`"policy"`, no `main.py` edits | Phase 5 (M5.2) |
| **Tier-2 reviewer** | strategy plugin; `[tier2] strategy = "llm"` (default) → `"rule"`/`"external"` | Phase 5 (M5.2) |
| HITL presenter | `HitlPresenter` ABC; `[hitl] presenter = "terminal"` → `"gtk"`/`"web"` | seam exists (M5.P2-access) |
| **Keymap** | `[keymap]` config (below); numeric/mnemonic defaults, fully overridable | Phase 5 (M5.1) |
| **Audit sink** | `[audit] sink = "file"` (default) → `"journald"`/`"system-appender"` | Phase 5 (M5.3 / daemon) |

### Configurable keymap (default → user-overridable)

```toml
[keymap]                       # raw single-keypress, no Enter; Esc always = deny (reserved)
approve = ["1", "a", "y"]
deny    = ["2", "d", "n"]
modify  = ["3", "m"]
explain = ["4", "e"]
trust   = ["5", "t"]           # bounded: Tier ≤ 2 only, never Tier 3
help    = ["?"]
```

A `Keymap` loader (new `controller/keymap.py`) validates that bindings are single printable
chars, rejects duplicate keys across actions, and keeps `Esc`/deny reserved. `?` always prints
the **active** (possibly customized) legend, so customization never costs discoverability. The
same `Keymap` instance drives the HITL gate, the REPL, and the audit viewer.

---

## §4 — P0: the vital few (ship the core promise — safe + fast + graduated)

Three flag-gated milestones. This wave closes every High-severity finding and delivers the
Phase-5 headline (graduated determinism with a snappy, configurable, spoof-proof gate).

### M5.1 — Hardened, configurable, single-keypress HITL gate  *(security-critical; SF-1/2/3/4)*

**Files:** `dual-brain/controller/hitl.py`, new `dual-brain/controller/keymap.py`, new
`dual-brain/controller/trust_store.py`, `dual-brain/controller/config.py` (`[keymap]`, extend
`HitlConfig`), `dual-brain/controller/main.py` (trust consult + `[M]` reclassify loop), tests.

**Actions:**
- **SF-1:** add `_sanitize_display(s)` — strip ANSI + C0/C1 control chars, neutralize
  `\r`/`\n`, flag homoglyph/confusable runs — and apply it to **every** field in
  `HitlDisplayData`, not just `cow_summary`.
- **SF-2/3:** after `lockout()` completes, `termios.tcflush(sys.stdin, TCIFLUSH)`, then read a
  **single raw keypress** (`tty.setcbreak`) instead of `readline()`. Preserve the non-TTY →
  deny path and the `time.monotonic()` lockout enforcement (INV-6).
- **Keymap-driven decisions:** map keypresses through the loaded `Keymap` (defaults `1/2/3` +
  `a/r/m`...); `?` renders the active legend.
- **Implement the deferred actions** (replace stubs at `hitl.py:125-127`): `[E]xplain` →
  QB plain-language consequence via the `_qb_summarise` pattern, re-display, lockout re-applies;
  `[M]odify` → edit `target`/`params` → `intent_store.revise()` → re-validate → **re-classify**
  → fresh HITL on the revised intent.
- **SF-4 — bounded session-trust (`[T]rust`):** `trust_store.py` records a per-session entry
  keyed by `(action, target-prefix)`, **Tier ≤ 2 only (never Tier 3)**, audited, revocable;
  `run_turn` consults it to skip a repeated Tier-2 prompt; `/trust list|revoke` manages it.

**Acceptance:** HITL-spoof corpus 0/N renders an escape; pre-buffered stdin during lockout is
discarded; keymap overrides load and `?` shows them; `[E]/[M]/[T]` work; `[T]` cannot cover
Tier 3; every decision (incl. trust grant/use) is audited.

### M5.2 — Tier 2 LLM review pass, escalate-only  *(the Graduated-Determinism headline)*

**Files:** new `dual-brain/controller/tier2_review.py`, `dual-brain/controller/risk_classifier.py`
(expose a pluggable `classifier`/`tier2` strategy registry — principle #2),
`dual-brain/controller/main.py` (insert between step 3 classify and step 5 store),
`config.py` (`[risk]`, `[tier2]`), tests.

**Actions:** a `BrainBackend.complete()` call with schema `{approved, escalate, reason}` that
reviews **Tier-2** intents for scope-escalation / injection / unintended consequence; on
`escalate` → route to the Tier-3 HITL gate; else proceed with the existing non-blocking
notification. **Invariant: the review may only escalate 2→3 — it can never lower a rule-based
tier.** Behind `[tier2] enabled` and `[tier2] strategy` (default `"llm"`). Decision audited.

**Acceptance:** flagged medium-risk ops escalate to HITL; Tier 0/1/3 rule-based classification
is unchanged (parity on the existing tier corpus); the reviewer is swappable via config.

### M5.3 — Tamper-evident audit + stronger redaction  *(security-critical; SF-5/6)*

**Files:** `dual-brain/controller/audit.py` (+ a `--verify` CLI), `config.py` (`[audit]`), tests.

**Actions:** add `prev_hash` (SHA-256 chain over the canonical serialized line) and a
`verify_chain()` checker + CLI; make the sink pluggable (`[audit] sink`, file default;
journald / root-owned system-appender documented for daemon mode so the AI/user can append but
not rewrite); strengthen redaction with entropy heuristics + per-tool field allowlists; never
log raw `target` for credential-class tools. Preserve O_APPEND + per-line `fsync` + 0o640 +
INV-8.

**Acceptance:** any edit/deletion of a past line is detected by `verify_chain()`; the
strengthened-redaction corpus shows zero secrets on disk; INV-8 still holds.

### P0 exit gates (Phase-5 wave-1 definition-of-done)

| Gate | Assertion |
|---|---|
| G5.1 | HITL renders no attacker-controlled escape/control sequences (SF-1 spoof corpus 0/N) |
| G5.2 | Lockout un-bypassable: input buffered during the 3 s window is discarded; single keypress required after flush (SF-2/3, INV-6) |
| G5.3 | Keymap is config-driven and user-overridable; `?` prints the active legend; invalid/duplicate bindings are rejected at load |
| G5.4 | `[E]xplain`/`[M]odify`/`[T]rust` work; `[M]` routes through `intent_store.revise()`+reclassify; `[T]` cannot grant Tier 3; all audited (SF-4) |
| G5.5 | Tier-2 LLM review escalates flagged ops and **never downgrades** a rule-based tier |
| G5.6 | Audit hash-chain verifies; tampering with any prior line is detected; redaction corpus clean; INV-8 holds (SF-5/6) |

---

## §5 — P1: second wave (high-value, sequenced after P0)

- **M5.P1-sec — Credential & resource hygiene (SF-7/8/9/10).** Scrub the `mcpd` subprocess env
  of API keys + move key material to systemd `LoadCredential=` / a keyring; secure (and
  optionally ephemeral) REPL history; input-size cap + turn-rate limit + **per-session cost
  ceiling** (accumulated in `SessionState`, surfaced in `/status` and the REPL status line);
  pass mcpd the realpath-resolved target and ensure HITL decision facts derive only from
  structured sources.
- **M5.P1-undo — `/undo` (and `u`)** over mcpd's existing COW rollback — a large safety/UX win;
  the reversal is itself audited.
- **M5.P1-stream — token streaming + multi-step progress + cancel**, extending the existing
  spinner; Ctrl+C already cancels a turn.
- **M5.P1-viewer — keyboard-navigable audit viewer** (`controller/audit_viewer.py` +
  `python -m controller.audit_viewer`): filter by `session_id`/`backend`/`tier`/`outcome`/
  action/time; `j/k` navigation, `/` search via the shared keymap; reads across rotated
  segments; never displays redacted secrets.

---

## §6 — P2: third wave (breadth / deployment)

- **M5.P2-backends — OpenAI backend + OAuth-subscription scaffolding** (`backends/openai_backend.py`,
  native JSON mode, `transform_schema_for_provider` reuse; OAuth token-refresh + browser
  callback behind the existing `BrainBackend` interface) and **QB-verifier majority voting
  (k=3)** in `Controller._qb_verify` behind `[verify] k`.
- **M5.P2-daemon — daemon mode (SF-11).** Long-running Controller as a hardened systemd *user*
  service (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, seccomp);
  `AF_UNIX` socket 0600 + `SO_PEERCRED` uid check; **no TCP listener (INV-3 parity,
  CI-asserted as for mcpd)**; log rotation (from M5.3); multiplexed sessions that preserve
  per-session INV-1/INV-2 isolation and per-session audit. (Full Unix-socket *brain* transport
  — the `transport="unix"` seam reserved in `llama_local_backend.py` — remains Phase 6.)
- **M5.P2-access — accessibility & GUI/web presenter.** No-color / no-Unicode / screen-reader
  plain mode (seeded by `TerminalPresenter`'s `isatty` checks); a GUI/web `HitlPresenter`
  implementation via the existing ABC + `HitlConfig.presenter` seam.

---

## §7 — Release-readiness (closes the phase)

- **M5.V1 — Red-team the new surfaces:** HITL-spoofing corpus, lockout-bypass corpus,
  trust-store abuse (attempt to cover Tier 3 / escalate scope), cost-bomb / rate-abuse. Extends
  the Phase-2 adversarial harness; assertion remains *0 unintended dispatch on any backend*
  (G6 preserved).
- **M5.V2 — Usability simulation → KPI 5:** simulate ~1 week of usage; derive Tier-3 frequency
  and approval latency **from the existing audit log** (no new logging — `audit.py` already
  records `tier`, `outcome`, `duration_ms`). Targets: **Tier 3 < 5/day, 0 rubber-stamped, NL
  task completion**.
- **M5.V3 — Exit gates + closeout:** extend `dual-brain/controller/ci.sh` with the P0 (and,
  when reached, P1/P2) gates; write §Closeout with per-gate evidence, mirroring Phase 2.

---

## §8 — Files Added or Modified (across all waves)

| Path | Action | Wave |
|---|---|---|
| `dual-brain/controller/hitl.py` | Modify — sanitize all fields, flush+raw keypress, keymap-driven, `[E]/[M]/[T]` | P0 (M5.1) |
| `dual-brain/controller/keymap.py` | New — config-driven keymap loader/validator | P0 (M5.1) |
| `dual-brain/controller/trust_store.py` | New — bounded, audited, revocable session trust | P0 (M5.1) |
| `dual-brain/controller/tier2_review.py` | New — escalate-only Tier-2 LLM review (pluggable) | P0 (M5.2) |
| `dual-brain/controller/risk_classifier.py` | Modify — pluggable classifier/tier2 strategy registry | P0 (M5.2) |
| `dual-brain/controller/audit.py` | Modify — hash-chain + `verify_chain()` + redaction++ + pluggable sink | P0 (M5.3) |
| `dual-brain/controller/main.py` | Modify — Tier-2 insert, trust consult, `[M]` reclassify loop | P0 |
| `dual-brain/controller/config.py` | Modify — `[keymap]`,`[risk]`,`[tier2]`,`[audit]`,`[cost]` | P0/P1 |
| `dual-brain/controller/audit_viewer.py` | New — keyboard-navigable viewer | P1 |
| `dual-brain/controller/backends/openai_backend.py` | New — OpenAI + OAuth scaffolding | P2 |
| `dual-brain/scripts/icebreaker-controller.service` (+ socket) | New — hardened systemd unit | P2 |
| `dual-brain/controller/ci.sh` | Modify — Phase 5 gates | all |
| `dual-brain/controller/tests/*` | New (`test_*`) + corpora | all |
| `~/.config/icebreaker/controller.toml.example` | Modify — new sections incl. `[keymap]` | P0/P1 |
| `docs/phase5_roadmap.md` | This file | planning |
| `CLAUDE.md` | Phase Status reconcile (Phase 2 → Complete; Phase 5 → this doc) | planning |
| `shell/pb_*`, `src/mcpd/*` | **Do not modify** in Phase 5 | — |

---

## §9 — Failure Modes Register (seeded from the security findings)

| # | Failure | Mitigation |
|---|---|---|
| P5-F1 | HITL prompt spoofed via terminal escapes (SF-1) | `_sanitize_display()` on every rendered field; spoof corpus in CI (G5.1) |
| P5-F2 | 3 s lockout bypassed by pre-buffered input (SF-2/3) | `tcflush` + single raw keypress after lockout; timing + buffered-input tests (G5.2) |
| P5-F3 | Bounded trust escalated to cover Tier 3 / widened scope (SF-4) | Trust store hard-caps at Tier ≤ 2, keyed by `(action, target-prefix)`, session-scoped, audited, revocable; red-team in M5.V1 |
| P5-F4 | Tier-2 reviewer downgrades a rule-based tier | Escalate-only invariant; classifier strategy cannot lower the rule floor; parity test (G5.5) |
| P5-F5 | Audit history rewritten/deleted undetected (SF-5) | SHA-256 `prev_hash` chain + `verify_chain()`; INV-8 perms (G5.6) |
| P5-F6 | Free-form / `target` secrets leak to disk (SF-6) | Entropy + per-tool field allowlists; never log raw `target` for credential-class tools |
| P5-F7 | API key leaks into mcpd subprocess / history / logs (SF-7/8) | Scrub mcpd env; systemd `LoadCredential`/keyring; secure/ephemeral history (P1) |
| P5-F8 | Cost / DoS bomb via injection or runaway loop (SF-9) | Input-size cap + turn-rate limit + per-session cost ceiling (P1) |
| P5-F9 | Symlink swap between classify and execute (SF-10) | Pass resolved realpath to mcpd; structured-only HITL facts; mcpd Landlock/COW backstop |
| P5-F10 | Daemon IPC reachable by another uid / over TCP (SF-11) | `AF_UNIX` 0600 + `SO_PEERCRED`; no TCP (INV-3, CI-asserted); systemd hardening (P2) |
| P5-F11 | Keymap override creates an un-exitable / duplicate binding | Loader rejects duplicates, reserves `Esc`/deny, requires single printable chars (G5.3) |

---

## §10 — Deferred to Phase 6 / 7

| Item | Phase | Why |
|---|---|---|
| Unix-socket **brain** transport (`transport="unix"`) | 6 | Tied to the system-wide install layout / socket perms |
| ISO embedding of Controller + systemd units + models | 6 | Distribution engineering |
| End-to-end pen-test of the full installed stack | 7 | Hardening/release |
| `schema_version` negotiation | 7 | No live drift problem yet |

---

## §11 — References

- `AI_Native_OS_Whitepaper.md` §8 (UX & Graduated Determinism — Tier 0–3, HITL design), §10 Phase 5, §12 F4, §13 KPI 5
- `docs/implementation_plan.md` §0 (authoritative phase status)
- `dual-brain/docs/phase2/phase2_roadmap_CLOSED_2026-06-11.md` §10 (deferred-to-Phase-5 list), §5 (cross-cutting decisions)
- `dual-brain/controller/hitl.py` (SF-1/2/3 — `_render`, `read_decision`, `lockout`)
- `dual-brain/controller/audit.py` (SF-5/6 — append-only, `_redact_params`)
- `dual-brain/controller/config.py` (`SecretRef`, `[hitl] presenter` seam, secret-key rejection)
- `dual-brain/controller/risk_classifier.py`, `main.py`, `intent_store.py`, `repl.py`
- `CLAUDE.md` § INV-1…INV-8, § Test-Only Knobs (feature-flag pattern)

---

*Created June 2026 — Phase 5 planning. Living document; update as milestones land and gates pass.*
