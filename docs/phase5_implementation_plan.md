# Phase 5 — Implementation Plan

> Companion to `docs/phase5_roadmap.md` (the design doc). This file is the build-order
> checklist: file-by-file changes, test plan, gates, PR sequence, rollout. Update it as
> work lands; treat the roadmap as the spec, this as the playbook.

## §0 — Status snapshot (update on every PR)

| Milestone | Status | PR | Notes |
|---|---|---|---|
| M5.1 Hardened HITL | ✅ Complete | PR #9 (P0) | `keymap.py`, `hitl.py` hardening, `trust_store.py`, REPL `/trust` commands |
| M5.2 Tier-2 escalate-only | ✅ Complete | PR #9 (P0) | `tier2_review.py`, classifier registry in `risk_classifier.py`, escalate-only insert in `main.py` |
| M5.3 Audit chain + redaction | ✅ Complete | PR #9 (P0) | Hash-chain (seq + prev_hash), `verify_chain()` CLI, Shannon entropy redaction, per-tool field allowlist, `AuditSink` ABC |
| M5.P1-sec | ✅ Complete | PR #10 (P1-A) | env scrub (`_scrubbed_env`), ephemeral REPL history, cost/limits governance, TOCTOU realpath fix. 981 tests. |
| M5.P1-viewer | ✅ Complete | PR #11 (P1-B) | Interactive TUI audit viewer: lazy-indexed, j/k nav, filter/search/verify/stats, `/audit` REPL command, CLI `--json` mode. 1060 tests. |
| M5.P1-stream | ✅ Complete | PR #11 (P1-BCD) | TurnEvent protocol, pipeline progress, QB token streaming, Ctrl+C cancel. 1182 tests. |
| M5.P1-undo | ✅ Complete | PR #11 (P1-BCD) | UndoHistory ring buffer, /undo scaffold, UndoConfig. Awaits mcpd rollback RPC. 1182 tests. |
| M5.P2-backends | ⬜ In progress | — | OpenAI backend + QB-verifier majority voting |
| M5.P2-daemon | ⬜ Queued | — | systemd user service, AF_UNIX socket, client/server split |
| M5.P2-access | ⬜ Queued | — | Screen-reader presenter, GTK presenter scaffold, presenter registry |

## §1 — Invariants we must not break

INV-1 (brain isolation), INV-2 (schema-only intents), INV-3 (mcpd stdio-only), INV-4
(per-tool schema), INV-5 (sandbox before fork), INV-6 (≥3 s lockout, monotonic clock),
INV-7 (model checksums), INV-8 (audit append-only/fsync/0o640/not-model-writable).

Engineering best practices BP-1…BP-12 from `CLAUDE.md` apply.

WF-6 — every PR touching `hitl.py`, `intent_store.*`, `audit.py`, `risk_classifier.py`
needs **two-human-reviewer** LGTM. WF-7 — one module per PR.

## §2 — PR order

1. **PR-A**: `feature/phase5-m5.1-keymap` — pure addition: new `controller/keymap.py` +
   tests + `config.py` adds `[keymap]` section. No behavior change. Trivial to review.
2. **PR-B**: `feature/phase5-m5.1-hitl-hardening` — `controller/hitl.py` hardening
   (sanitize, tcflush, raw keypress, ASCII/`$NO_COLOR` fallback), `Decision` extended,
   `_qb_explain` helper in `main.py`. Wires the keymap from PR-A.
3. **PR-C**: `feature/phase5-m5.1-trust-store` — `controller/trust_store.py` + `main.py`
   wiring (TRUST_APPLIED audit, `[T]` action), REPL slash-commands `/trust list|revoke|off`.
4. **PR-D**: `feature/phase5-m5.2-tier2-review` — `controller/tier2_review.py`, pluggable
   classifier registry, `main.py` escalate-only insert. Independent of PR-C.
5. **PR-E**: `feature/phase5-m5.3-audit-chain` — `controller/audit.py` hash-chain +
   `verify_chain()` CLI + entropy redaction + pluggable sink. Independent of A–D.

Why this order: PR-A is a no-op and lands `keymap.py` — used by PR-B, PR-C, and (later)
REPL/viewer. PR-B is the highest-impact security fix; we ship it first. PR-C and PR-D both
build on the `Decision` enum from PR-B and can land in either order. PR-E is fully
parallel and can land any time after `main` is at PR-A + PR-B.

## §3 — Files touched per PR

(Cross-reference `docs/phase5_roadmap.md` §4 for the design rationale; this is the
file-by-file diff index.)

### PR-A — Keymap

- NEW `dual-brain/controller/keymap.py` — `Action` enum, `Keymap` frozen dataclass,
  `DEFAULTS`, `load_keymap(cfg)`, `legend()`.
- MOD `dual-brain/controller/config.py` — add `KeymapConfig` to `RunConfig`, add `[keymap]`
  TOML loader, reject duplicate keys at config load (raise `BrainConfigError`).
- MOD `dual-brain/controller/schemas/controller_config.json` — `[keymap]` schema.
- NEW `dual-brain/controller/tests/test_keymap.py` (G5.3).
- MOD `dual-brain/controller.toml.example` — add commented `[keymap]` block.

### PR-B — HITL hardening

- MOD `dual-brain/controller/hitl.py` —
  - Add `_sanitize_display(s, *, max_len=256)` module fn (strips ANSI + C0/C1, neutralizes
    `\r\n`, truncates).
  - Add `_ascii_safe()` / `_colors_enabled()` env detection (`$NO_COLOR`, `$LC_ALL`,
    `$LANG`, `sys.stdout.isatty()`).
  - `HitlDisplayData.__post_init__` sanitizes **every** string field (`action`, `target`,
    `reason`, `blocked_pattern`, `backend`, `cow_summary`).
  - `Decision` enum gains `MODIFY`, `EXPLAIN`, `TRUST`.
  - `TerminalPresenter.read_decision(keymap, timeout_seconds)`:
    `termios.tcflush(TCIFLUSH)` **after** lockout returns; `tty.setcbreak`;
    `os.read(...,1)` single-byte read; `SIGINT` handler maps to DENY; EOF → DENY;
    ESC → DENY; unknown key → re-render legend, keep waiting.
  - `_render(data, keymap)` reads footer from `keymap.legend()`.
  - ASCII fallback path (`_ascii_safe()` swaps `⚠`/`⛔`/`✓`/`✗`/box-drawing → ASCII).
  - Audit-field augmentation: `decision_id`, `key_pressed_class`, `latency_ms`.
- MOD `dual-brain/controller/audit.py` — add 3 new `Outcome` enum values
  (`TRUST_APPLIED`, `TRUST_GRANTED`, `MODIFY_REQUESTED`); no chain logic yet (that's PR-E).
- MOD `dual-brain/controller/main.py` — `_build_presenter()` passes the loaded `Keymap`
  to `TerminalPresenter`; `_qb_explain(intent, cls)` helper (one-shot QB call,
  schema-constrained `{explanation: str}`, reuses `_qb_summarise` plumbing); modify-loop
  scaffold (bounded to 3 cycles, then forced DENY).
- NEW `dual-brain/controller/tests/test_hitl_sanitize.py`, `test_hitl_rawinput.py`,
  `test_hitl_actions.py`, `test_hitl_a11y.py`, `test_hitl_audit_fields.py` (G5.1–G5.4).
- NEW `dual-brain/controller/tests/corpus/hitl_spoof.json`,
  `dual-brain/controller/tests/corpus/lockout_bypass.json`.

### PR-C — Trust store

- NEW `dual-brain/controller/trust_store.py` — `TrustGrant`, `TrustStore` (in-memory,
  session-scoped); hard floor encoded in BOTH `grant()` (raises on Tier ≥ HIGH) AND
  `is_trusted()` (returns False on Tier ≥ HIGH).
- MOD `dual-brain/controller/main.py` — pre-Step-4 `is_trusted` consult + audit
  `TRUST_APPLIED` echo + one-line user-visible "auto-approved" notification; `[T]rust`
  action → `grant()` + audit `TRUST_GRANTED`.
- MOD `dual-brain/controller/repl.py` — `/trust list`, `/trust revoke <id>`, `/trust off`
  slash-commands.
- MOD `dual-brain/controller/config.py` — `[hitl] trust_ttl_seconds` (default **0** =
  disabled out of the box; trust-key dropped from active legend when disabled).
- NEW `dual-brain/controller/tests/test_trust_store.py` (G5.4).

### PR-D — Tier-2 escalate-only review

- NEW `dual-brain/controller/tier2_review.py` — `Tier2Decision`, `Tier2Reviewer` ABC,
  `LlmTier2Reviewer` (default `strategy="llm"`), `RuleTier2Reviewer`, `get_reviewer()`.
- MOD `dual-brain/controller/risk_classifier.py` — pluggable classifier registry; keep
  rule-based default. `register_classifier(name, fn)`, `get_classifier(name)`.
- MOD `dual-brain/controller/main.py` — escalate-only insert between Step 3 (classify)
  and Step 5 (store); runtime guard `assert cls.tier >= original_tier`.
- MOD `dual-brain/controller/config.py` — `[risk] strategy`, `[tier2] enabled/strategy/max_retries`.
- NEW `dual-brain/controller/prompts/tier2_review.txt`.
- NEW `dual-brain/controller/tests/test_tier2_review.py`,
  `dual-brain/controller/tests/test_classifier_registry.py`.
- NEW `dual-brain/controller/tests/corpus/tier2_escalation.json`.

### PR-E — Audit chain + redaction + pluggable sink

- MOD `dual-brain/controller/audit.py` —
  - `seq` (monotonic) + `prev_hash` (SHA-256 of previous canonical line; "GENESIS" for
    seq 0) appended to every entry.
  - On `_open()`, recover `(_last_seq, _last_hash)` from the tail.
  - `verify_chain(path) -> tuple[bool, Optional[int]]` classmethod (returns ok + first bad
    seq or None).
  - `AuditSink` ABC + `FileSink`/`JournaldSink`/`SystemAppenderSink`.
  - `_high_entropy(s, *, min_len=20, bits=3.5)` Shannon-entropy gate; redacts long
    high-entropy strings even with innocuous key names.
  - Per-tool field allowlist (`_TOOL_FIELD_ALLOWLIST`) — e.g. `fs.write` logs `path`
    only, never `content`.
- NEW `python -m controller.audit --verify [path]` CLI entry (prints OK or first bad
  seq; exit code reflects status; wired into `ci.sh`).
- MOD `dual-brain/controller/config.py` — `[audit] sink`, `path`, `redact_entropy_bits`.
- NEW `dual-brain/controller/tests/test_audit_chain.py`,
  `dual-brain/controller/tests/test_audit_redaction.py`.

### PR-F — OpenAI backend + verifier voting (M5.P2-backends)

- NEW `dual-brain/controller/backends/openai_backend.py` — `@register_backend("openai")`
  extending `_ApiBackend`, native Structured Outputs mode (`response_format` with
  `type: "json_schema"`, `strict: True`). `_call_provider()` and `_stream_provider()`
  following the same pattern as `anthropic_backend.py`.
- NEW `dual-brain/controller/verifier.py` — `VerifierStrategy` ABC, `SingleVerifier`
  (backward-compat default), `MajorityVoter` (ThreadPoolExecutor, configurable N-of-M
  threshold). `VerifierResult` dataclass.
- MOD `dual-brain/controller/__main__.py` — add `openai_backend` to self-registration
  import list (line 30).
- MOD `dual-brain/controller/schemas/controller_config.json` — add `"openai"` to
  `qb.backend` enum; add `"openai": {"$ref": "#/definitions/api_section"}`; add
  `[verifier]` section schema.
- MOD `dual-brain/controller/config.py` — add `VerifierConfig` dataclass + builder;
  add `qb_openai` to `PromptsConfig`.
- MOD `dual-brain/controller/main.py` — replace inline `_qb_verify()` with
  `self._verifier.verify()`.
- MOD `dual-brain/controller/controller.toml.example` — add commented `[qb.openai]`
  and `[verifier]` blocks.
- NEW `dual-brain/controller/prompts/qb_openai.txt` — system prompt for OpenAI backend.
- NEW `dual-brain/controller/tests/test_openai_backend.py` (~20 tests).
- NEW `dual-brain/controller/tests/test_verifier.py` (~25 tests).

### PR-G — Presenter registry + screen-reader + GTK scaffold (M5.P2-access)

- NEW `dual-brain/controller/presenters/__init__.py` — package init.
- NEW `dual-brain/controller/presenters/registry.py` — `@register_presenter(name)`
  decorator, `_PRESENTER_REGISTRY` dict, `make_presenter(config, keymap)` factory.
- NEW `dual-brain/controller/presenters/terminal.py` — `TerminalPresenter` moved here
  + `@register_presenter("terminal")`.
- NEW `dual-brain/controller/presenters/screen_reader.py` —
  `@register_presenter("screen_reader")`. No ANSI, no box-drawing, no countdown
  overwrites. Structured labeled output optimized for NVDA/JAWS/VoiceOver/Orca.
- NEW `dual-brain/controller/presenters/gtk.py` — `@register_presenter("gtk")` scaffold.
  GTK4 dialog, lazy PyGObject import, lockout via `GLib.timeout_add_seconds`, blocks
  caller via `threading.Event`.
- NEW `dual-brain/controller/presenters/forwarding.py` —
  `@register_presenter("forwarding")`. Serializes `HitlDisplayData` over socket for
  daemon mode (PR-H).
- MOD `dual-brain/controller/hitl.py` — keep ABC + `HitlDisplayData` + coordinator;
  re-export `TerminalPresenter` for backward compatibility.
- MOD `dual-brain/controller/main.py` — `_build_presenter()` uses
  `make_presenter(cfg.hitl, cfg.keymap)`.
- MOD `dual-brain/controller/schemas/controller_config.json` — extend `presenter` enum:
  `["terminal", "screen_reader", "gtk"]`.
- MOD `dual-brain/controller/controller.toml.example` — document presenter options.
- NEW `dual-brain/controller/tests/test_presenter_registry.py` (~15 tests).
- NEW `dual-brain/controller/tests/test_screen_reader_presenter.py` (~20 tests).
- NEW `dual-brain/controller/tests/test_gtk_presenter.py` (~10 tests, mock GTK).

### PR-H — Daemon + client/server split + systemd (M5.P2-daemon)

- NEW `dual-brain/controller/daemon.py` — AF_UNIX server, 0600 socket perms,
  SO_PEERCRED UID authentication, session management, SIGTERM handler.
- NEW `dual-brain/controller/client.py` — thin client: connects to AF_UNIX socket,
  sends JSON-RPC requests, receives TurnEvent streams + HITL forwarding.
- NEW `dual-brain/controller/protocol.py` — shared JSON-RPC 2.0 message types for
  bidirectional client↔daemon communication. Request types: `turn.run`, `session.new`,
  `session.reset`, `hitl.respond`, `daemon.status`, `daemon.shutdown`. Notification
  types: `turn.progress`, `turn.token`, `turn.result`, `hitl.prompt`.
- MOD `dual-brain/controller/__main__.py` — `--daemon` mode, `--connect <socket>` mode.
- MOD `dual-brain/controller/repl.py` — client-mode REPL that delegates to `client.py`.
- MOD `dual-brain/controller/config.py` — `DaemonConfig` dataclass (socket_path,
  pid_file, max_clients) + builder.
- MOD `dual-brain/controller/schemas/controller_config.json` — `[daemon]` section.
- MOD `dual-brain/controller/controller.toml.example` — commented `[daemon]` block.
- NEW `dual-brain/controller/systemd/icebreaker-controller.service` — systemd user unit.
- NEW `dual-brain/controller/systemd/icebreaker-controller.socket` — socket activation.
- NEW `dual-brain/controller/tests/test_daemon.py` (~20 tests).
- NEW `dual-brain/controller/tests/test_client.py` (~15 tests).
- NEW `dual-brain/controller/tests/test_protocol.py` (~15 tests).

## §4 — `ci.sh` updates (Phase-5 gates)

After the existing G1–G11 block in `dual-brain/controller/ci.sh`:

```bash
# G5.1 — HITL spoof-sanitization + a11y fallback
python -m pytest tests/test_hitl_sanitize.py tests/test_hitl_a11y.py -x -q

# G5.2 — Lockout / raw-input
python -m pytest tests/test_hitl_rawinput.py -x -q

# G5.3 — Keymap
python -m pytest tests/test_keymap.py -x -q

# G5.4 — Actions + trust store + audit fields
python -m pytest tests/test_hitl_actions.py tests/test_trust_store.py \
                 tests/test_hitl_audit_fields.py -x -q

# G5.5 (PR-D) — Tier-2 escalate-only + classifier registry
python -m pytest tests/test_tier2_review.py tests/test_classifier_registry.py -x -q

# G5.6 (PR-E) — Audit hash-chain + redaction; verify a freshly written log
python -m pytest tests/test_audit_chain.py tests/test_audit_redaction.py -x -q
python -m controller.audit --verify /tmp/audit-smoke.log
```

`ci.sh` continues to be the authoritative pre-merge gate. Live end-to-end (PR-B onward
exercises a PTY-driven real prompt, not a mock) is the final guardrail — mocked gates
miss real bugs (lesson #4 from `HANDOFF.md`).

P1 gates (added with PRs #10 and #11):

```bash
# G5.P1a — Credential & resource hygiene
python -m pytest tests/test_env_scrub.py tests/test_cost_limits.py \
                 tests/test_ephemeral_history.py tests/test_toctou.py -x -q

# G5.P1b — Audit viewer (TUI + REPL integration)
python -m pytest tests/test_audit_viewer.py -x -q

# G5.P1b-cli — Audit viewer CLI JSON mode
python -m controller.audit_viewer --json /tmp/audit-smoke.log | head -1 | python -c "import sys,json; json.load(sys.stdin)"

# G5.P1c — Streaming + pipeline progress
python -m pytest tests/test_streaming.py tests/test_turn_events.py -x -q

# G5.P1d — Undo scaffold
python -m pytest tests/test_undo.py -x -q
```

P2 gates (added with PRs #12, #13, #14):

```bash
# G5.P2a — OpenAI backend + verifier voting
python -m pytest tests/test_openai_backend.py tests/test_verifier.py -x -q

# G5.P2b — Daemon + client + protocol
python -m pytest tests/test_daemon.py tests/test_client.py tests/test_protocol.py -x -q

# G5.P2c — Presenter registry + screen-reader + GTK scaffold
python -m pytest tests/test_presenter_registry.py tests/test_screen_reader_presenter.py \
                 tests/test_gtk_presenter.py -x -q
```

## §5 — Rollout

1. Each PR is a feature branch off `main`. No commits directly on `main`.
2. PR title format: `phase5(m5.1a): keymap module + config` (lowercase milestone, scope
   suffix when more than one PR lands a milestone).
3. PR body: **no Claude attribution** (no `Co-Authored-By:`, no "🤖 Generated with
   Claude Code" footer). Include a "Risk & rollback" section so the reviewer knows the
   blast radius and the revert command.
4. After merge, update §0 status table in this file (`Status`, `PR` columns).
5. After all P0 PRs land, tag `phase5-p0-complete` and write the P0 closeout note in
   `docs/implementation_plan.md` §0.

## §6 — Out-of-scope (explicit)

- mcpd changes — none in Phase 5. Phase 5 is Controller-side only.
- Model retrain — explicitly not in scope. run7 stands; do not casually retrain.
- ISO build — Phase 6, after Phase 5.
- ~~Persistent trust grants across REPL restarts~~ — shipped in P1-A (PR #10).
- ~~GUI/web presenter~~ — now in scope: P2 PR #14 (`M5.P2-access`).
- ~~`/undo` (mcpd COW rollback)~~ — scaffold shipped in P1-D (PR #11); mcpd rollback RPC remains Phase 6.
- ~~OpenAI backend / QB-verifier voting~~ — now in scope: P2 PR #12 (`M5.P2-backends`).
- ~~Daemon-mode systemd unit~~ — now in scope: P2 PR #13 (`M5.P2-daemon`).

## §7 — Acceptance for "P0 complete"

All of:

- G5.1–G5.6 green in `ci.sh`.
- Manual PTY-driven smoke (mocked QB) exercises:
  - Tier-2 escalation via M5.2 reviewer.
  - `[M]odify` revise loop, then approve.
  - `[T]rust` grant + auto-apply on a second matching Tier-2 intent + `/trust revoke`.
  - A tampered audit line caught by `python -m controller.audit --verify`.
- `docs/implementation_plan.md` §0 updated to reflect Phase-5 P0 complete.
- One human reviewer + module owner LGTM per WF-6.

## §8 — UX bar (production-ready, applies to every P0 PR)

Each PR that touches user-facing surfaces must, before merge, demonstrate:

- **Heuristics table** in the PR body: which Nielsen / Fitts / Hick / progressive
  disclosure / safe-default points the change applies, and where in the diff.
- **Render mockup** for each new visible state (UTF-8 with color, ASCII fallback, and
  the trust-applied echo if relevant).
- **Failure-mode table** where every row defaults to DENY (or no-op): timeout, non-TTY,
  EOF, SIGINT, malformed input, unknown key, config error.
- **Observability**: every decision carries `decision_id`, `key_pressed_class`,
  `latency_ms` in the audit log so we can later measure P0's actual UX.
- **A11y seam**: `$NO_COLOR` honored, ASCII fallback present, `[hitl] sr_mode = true`
  config seam exists even if the full screen-reader presenter is P2.

This bar is encoded in the project memory (`feedback_production_ready_ux.md`) so it
carries forward without being re-derived per PR.

## §9 — Risk register (Phase 5 specific)

| # | Risk | Mitigation |
|---|---|---|
| R-5.1 | `[T]rust` becomes a silent auto-approve channel for prompt injection | Hard floor encoded twice (`grant`+`is_trusted`); opt-in default OFF (`trust_ttl_seconds=0`); session-scoped; always-visible "auto-approved" echo; full audit (`TRUST_GRANTED`+`TRUST_APPLIED`) |
| R-5.2 | Tier-2 reviewer downgrades a Tier-3 op via malformed output | `TIER2_SCHEMA` + max_retries + runtime `assert cls.tier >= original_tier` — escalate-only is an invariant, not a convention |
| R-5.3 | Sanitization removes legitimate diff characters from `cow_summary` | `cow_summary` already sanitized today; the new module-level `_sanitize_display` keeps the same behavior for that field; spoof corpus tests both spoof rejection AND legitimate-diff retention |
| R-5.4 | Audit hash-chain breaks on log rotation or concurrent write | `_lock` is held around seq/prev_hash update + `os.write` + `os.fsync` (atomic from the file's perspective); rotation is out of scope for M5.3 (P1 viewer ships rotation-aware reader) |
| R-5.5 | A keymap that disables DENY produces an un-exitable prompt | Loader requires DENY to retain ≥1 binding; `Esc` is hard-reserved DENY; `Ctrl+C` signal handler is DENY — three independent paths to deny |
| R-P2.1 | OpenAI Structured Outputs silently drops schema constraints the local validator enforces | Local `jsonschema.Draft7Validator` remains the safety floor (INV-2-pluggable). OpenAI's grammar is an optimization, not a gate. |
| R-P2.2 | Majority voting majority-fails on a correct tool call due to QB stochastic variance | Default `votes=1` (no voting). When enabled, operator sets threshold knowingly. Sampling decay still applies per-vote. |
| R-P2.3 | AF_UNIX socket leaks to another user via symlink race | Create socket in a 0700 directory + 0600 socket + SO_PEERCRED UID check. Three independent barriers. |
| R-P2.4 | Daemon HITL forwarding introduces a TOCTOU gap (client sends approval, daemon acts on stale intent) | Daemon thread holds the intent lock from `hitl.prompt` send through `hitl.respond` receive. No re-evaluation between. |
| R-P2.5 | GTK presenter on Wayland crashes due to display server access from a non-main thread | GTK dialog runs on GLib main loop; caller thread blocks on a `threading.Event`. Standard GTK thread-safety pattern. |
| R-P2.6 | Screen-reader presenter's `time.sleep(lockout)` blocks without progress indication | Deliberate: screen readers handle silence better than rapid-fire timer updates. One message before, one after. |

## §10 — Update protocol for this file

- On every PR merge: update §0 status row, set the PR column, append the merge SHA in
  parens.
- On every milestone closeout (M5.1, M5.2, M5.3): write a 3-bullet closeout note at the
  bottom of the relevant §3 subsection (PR-A/B/C/D/E) — what shipped, what's deferred,
  one-line guidance for the next collaborator.
- Do NOT edit `docs/phase5_roadmap.md` to reflect status — that's the design doc.
  Status lives here.

## §11 — P1 closeout notes (update as P1 PRs land)

### PR #10 — M5.P1-sec (Credential & Resource Hygiene)

Merged 2026-06-14. Closes SF-7 (env leak), SF-8 (history leak), SF-9 (cost/DoS), SF-10 (TOCTOU).

- **Shipped:** `_scrubbed_env()` whitelist in `mcpd_client.py`, ephemeral REPL history (default on),
  `CostConfig` + `LimitsConfig` in `config.py`, rate limiting + input size cap + cost ceiling
  in `repl.py`, `os.path.realpath()` TOCTOU fix in `main.py`, new `Outcome.LIMIT_EXCEEDED` +
  `Outcome.COST_EXCEEDED`. 32 new tests. CI gate G5.P1a green.
- **Deferred:** None — all four security findings closed.
- **Next:** PR-P1-B (audit viewer) or PR-P1-C (streaming).

### PR #11 — M5.P1-viewer (Interactive Audit Log Viewer)

Merged 2026-06-14. Enterprise-grade terminal TUI for audit log inspection.

- **Shipped:** `audit_viewer.py` (~650 lines) with `LogIndex` (lazy byte-offset index for
  100K+ entry performance), `FilterSpec` (session/tier/outcome/action/backend/time/regex),
  `ChainStatus` (per-entry hash-chain verification), 5 view modes (summary/detail/help/stats/filter).
  Keyboard navigation (j/k/g/G, Enter, Esc, /, f, v, s, ?). `$NO_COLOR` + ASCII fallback.
  Non-TTY JSONL dump mode (`--json`). REPL `/audit` command. CLI `python -m controller.audit_viewer`.
  79 viewer tests + 2 REPL integration tests. CI gates G5.P1b + G5.P1b-cli green. 1060 total tests.
- **Deferred:** Log rotation awareness, `n/N` search navigation, `r` refresh, export,
  mouse support, configurable viewer keymap — all future enhancements.
- **Next:** PR-P1-C (streaming) or PR-P1-D (undo scaffold).

### PR #11 — M5.P1-BCD (Streaming + Undo + Audit Viewer bundled)

Built 2026-06-14. Bundled P1-B viewer + P1-C streaming + P1-D undo scaffold.

- **Shipped:** TurnEvent protocol (`ProgressEvent`/`TokenEvent`/`ResultEvent`/`ErrorEvent`),
  `run_turn_streaming()` generator in `main.py`, `_stream_provider()` + `stream_complete()`
  in `base.py`, SSE/Anthropic/Gemini streaming overrides, pipeline progress rendering,
  Ctrl+C cancel with `Outcome.CANCELLED` audit, `UndoHistory` ring buffer, `/undo` REPL
  command, `UndoConfig`. 122 new tests. CI gates G5.P1c + G5.P1d green. 1182 total tests.
- **Deferred:** mcpd rollback RPC (undo always returns "unavailable"), streaming error
  recovery, per-step timing in audit.
- **Next:** P2 — OpenAI backend, verifier voting, daemon, presenters.

## §12 — P2 acceptance criteria

### PR #12 — M5.P2-backends (OpenAI Backend + Verifier Voting)

All of:

- G5.P2a green in `ci.sh`.
- `qb.backend = "openai"` runs a turn with mocked OpenAI SDK (unit tests — no live API
  key required in CI).
- Verifier voting with `votes=3` produces majority verdict; audit `extra` records
  `verifier_votes` and `verified_count`.
- Backward compat: `votes=1` (default) produces identical behavior to current single-call
  `_qb_verify()`.
- `transform_schema_for_provider()` handles OpenAI-specific quirks (e.g.
  `additionalProperties: false` injection).
- Older configs without `[qb.openai]` or `[verifier]` sections load without error.

### PR #13 — M5.P2-daemon (systemd User Service)

All of:

- G5.P2b green in `ci.sh`.
- Daemon starts on `--daemon`, creates AF_UNIX socket with 0600 permissions.
- SO_PEERCRED (Linux) / LOCAL_PEERCRED (macOS) rejects cross-UID connections.
- Client connects via `--connect`, sends turn, receives TurnEvent stream.
- HITL forwarding round-trip: daemon sends `hitl.prompt`, client responds `hitl.respond`,
  daemon proceeds with the human decision (BP-4 preserved).
- `--repl` without `--connect` still works (monolithic mode unchanged, BP-2).
- systemd unit files present and syntactically valid (`systemd-analyze verify`).

### PR #14 — M5.P2-access (Presenter Registry + Screen-Reader + GTK)

All of:

- G5.P2c green in `ci.sh`.
- `make_presenter("terminal", ...)` returns `TerminalPresenter` (backward compat).
- `make_presenter("screen_reader", ...)` returns a presenter that emits zero ANSI escapes,
  zero box-drawing characters, plain-text labeled fields.
- `make_presenter("gtk", ...)` raises a clear error if PyGObject is missing; renders a
  dialog with lockout + approve/deny when PyGObject is present.
- `TerminalPresenter` import from `hitl.py` still works (re-export backward compat).
- Registry rejects duplicate registrations and unknown presenter names.

### "P2 complete" aggregate

All three PR acceptance sections above, plus:

- `docs/phase5_implementation_plan.md` §0 updated to reflect all P2 milestones as ✅.
- All P2 CI gates (G5.P2a, G5.P2b, G5.P2c) added to `ci.sh` and passing.
- Total test count ≥ 1300.
- One human reviewer + module owner LGTM per PR (WF-6).
