# Phase 6T — AI Terminal: Implementation Plan

> Companion to the design document (`docs/ai_terminal_design.md`). This file is the
> build-order checklist: file-by-file changes, test plan, gates, PR sequence, rollout.
> Update it as work lands; treat the design doc as the spec, this as the playbook.
>
> Design validated via interactive mockup (`docs/mockups/ai_terminal_mockup.html` v4).
>
> **Prerequisites:** Phases 0–6 complete (1446 tests, PRs #1–#21 merged to `main`).

## §0 — Status snapshot (update on every PR)

| Milestone | Status | PR | Notes |
|---|---|---|---|
| M6T.1 CoT events + daemon emission | ✅ Complete | #22 | `CotEvent` dataclass, emitter in `main.py`, `turn.cot` notification, 19 new tests (1465 total) |
| M6T.2 Textual TUI scaffold | ⬜ Not started | — | Split-pane layout, PTY spike, explanation panel renderer |
| M6T.3 Input router + NL mode | ⬜ Not started | — | `#` prefix, `F2` toggle, `Ctrl+Space` one-shot, HITL presenter |
| M6T.4 GUI Agent scaffold + AT-SPI | ⬜ Not started | — | Sandboxed process, AT-SPI client, screenshot manager |
| M6T.5 GUI tools + risk + audit | ⬜ Not started | — | Tool schemas, risk classifier extension, COW preview, audit fields |
| M6T.6 App API + LibreOffice PoC | ⬜ Not started | — | App API registry, UNO bridge, D-Bus wrappers |
| M6T.7 RPA Bridge + Robot Framework | ⬜ Not started | — | RPA bridge process, workflow generator, image matcher |
| M6T.8 Escalation + RPA HITL | ⬜ Not started | — | Tier B→C escalation, crosshair preview, timeout enforcement |
| M6T.9 Polish + security audit | ⬜ Not started | — | Additional app APIs, accessibility, performance, hardening |
| M6T.10 CI gates + integration tests | ⬜ Not started | — | G16–G22 CI gates, end-to-end tests, doc updates |

## §1 — Invariants we must not break

All eight invariants from `CLAUDE.md` remain in force. Phase 6T extends the attack
surface to GUI and RPA operations; every invariant must apply to the new execution
tiers:

- **INV-1 (brain isolation):** QB never receives screenshot pixel data or window content.
  PB never receives raw user NL input. GUI screenshots are opaque references (file paths
  with SHA-256 hashes). PB receives only element locators, never screenshot data.
- **INV-2 (schema-only intents):** GUI intent params (`element_role`, `element_name`,
  `window`) validated against the same metacharacter-denylist pattern as CLI intents.
  AT-SPI element selectors are data, not code.
- **INV-3 (mcpd stdio-only):** mcpd itself is unchanged. GUI Agent and RPA Bridge are
  separate processes with their own transport (AF_UNIX to Controller daemon).
- **INV-4 (per-tool schema):** Every new GUI and RPA tool gets a JSON schema. Params
  validated before execution.
- **INV-5 (sandbox before fork):** GUI Agent and RPA Bridge get their own Landlock + Seccomp
  profiles applied before any GUI interaction occurs.
- **INV-6 (COW + 3s lockout):** GUI writes use screenshot-based COW preview. RPA always
  requires HITL with 3s lockout. Monotonic clock enforced.
- **INV-7 (model checksums):** Unchanged. No new model files.
- **INV-8 (audit):** GUI and RPA operations get extended audit fields (`execution_tier`,
  `screenshot_before_hash`, `element_locator`, `rpa_keywords_executed`, etc.).

Engineering best practices BP-1…BP-12 from `CLAUDE.md` apply. Especially:
- **BP-1 (plug-and-play):** GUI Agent, RPA Bridge, and app APIs use registry + config
  selector patterns.
- **BP-3 (sanitize display):** All GUI-sourced strings (window titles, element names)
  sanitized through `_sanitize_display()` before rendering.
- **BP-5 (escalate-only risk):** RPA is always Tier 3. GUI readonly is always Tier 0.
  ML/policy can only escalate, never de-escalate.
- **BP-12 (test the boundary):** Adversarial corpus for GUI element selectors. Differential
  tests between AT-SPI locators and screenshot visual state.

WF-5 — new modules: `dual-brain/terminal/` (TUI engineer), `dual-brain/gui_agent/`
(Backend engineer), `dual-brain/rpa_bridge/` (Backend engineer). WF-7 — one module per
PR (strictly enforced).

## §2 — Architecture decisions

| ADR | Decision | Key constraint |
|---|---|---|
| ADR-10 | **Textual for TUI framework** | `textual>=0.80` + `rich>=13`. Pure Python, no C extensions. PTY embedding via `textual-terminal` or subprocess fallback. Textual CSS theming must replicate mockup v4 appearance: dark OLED palette (`#0a0a0e` bg, `#818cf8` accent, `#34d399` PS1 green), JetBrains Mono font, glow text-shadows on NL-mode elements. `$NO_COLOR` / high-contrast fallback required. |
| ADR-11 | **GUI Agent is a separate sandboxed process** | Not embedded in mcpd. Landlock: `$HOME` (ro) + `/tmp/icebreaker-gui/` (rw). Seccomp: D-Bus sockets only. No raw X11. No `/dev/uinput`. No network. |
| ADR-12 | **AT-SPI primary, D-Bus app APIs preferred** | Use app-specific D-Bus APIs (LibreOffice UNO, Firefox DevTools) when available. Fall back to generic AT-SPI. Never use raw X11 (xdotool). |
| ADR-13 | **RPA is opt-in, always Tier 3** | `[rpa] enabled = false` by default. Every RPA operation requires HITL. Trust store hard-blocks RPA (Tier >= HIGH → never grantable). Time-boxed: 30s default. |
| ADR-14 | **Screenshot-based COW for GUI** | Before-screenshot + predicted outcome before write ops. After-screenshot for audit. Hash-only in audit log (not pixels). Screenshots stored in `/tmp/icebreaker-gui/` with 0600 perms, cleaned on session end. |
| ADR-15 | **Three execution tiers with automatic escalation** | Tier A (CLI/mcpd) → Tier B (GUI/AT-SPI) → Tier C (RPA/Robot Framework). Controller decides tier. Escalation from B→C only when AT-SPI element not found. |
| ADR-16 | **CoT events are Controller-generated, not LLM-generated** | Chain-of-thought text comes from the Controller's own structured data (risk classifier output, schema validation results, sandbox status). Never from model free-text. Aligns with BP-6. Each `CotEvent` carries a `step_state` field (`pending` / `active` / `done` / `failed`) for right-panel card rendering with visual state progression. |
| ADR-17 | **RPA Bridge uses Robot Framework as library, not CLI** | Import `robot.api` directly. Workflow keywords generated by the Controller, not free-form. Max 20 keywords per workflow. Screenshot at every keyword step. |
| ADR-18 | **Input bar at top of terminal** | Input bar positioned above the split pane, not below. Rationale: discoverability for new users (Fitts's law — top edge is a target boundary; bottom edge competes with OS taskbar). Mode indicator (Shell/NL) always visible. Configurable via `[terminal.appearance] input_position`. |
| ADR-19 | **Right panel dual-mode (companion)** | Right panel header and content switch between "Chain of Thought" (during execution, showing pipeline step cards with `pending`/`active`/`done`/`failed` states) and "Interpretation" (after completion, showing bar charts, plain English summary, actionable suggestions, tier badge). Single `CompanionPanel` widget swaps content based on turn state. |

## §3 — PR order

```
PR #22 (CoT events + daemon)
         |
PR #23 (Textual TUI scaffold)
         |
PR #24 (Input router + NL mode + HITL)
         |                              PR #25 (GUI Agent + AT-SPI)
         |                                       |
         └────────────┬──────────────────────────┘
                      |
              PR #26 (GUI tools + risk + COW + audit)
                      |
              PR #27 (App API registry + LibreOffice)
                      |
              PR #28 (RPA Bridge + Robot Framework)
                      |
              PR #29 (Escalation + RPA HITL + timeout)
                      |
              PR #30 (Polish + security audit + CI gates)
```

**Why this order:**

- **PR #22** is the foundation: `CotEvent` dataclass and daemon emission. All subsequent
  PRs depend on it for the explanation panel.
- **PR #23** is the TUI framework. Needs CoT events to render in the right panel.
- **PR #24** completes the terminal UX: input routing, NL mode, and HITL integration.
  This gives us a **working split-pane NL terminal** for CLI tools — the first shippable
  milestone.
- **PR #25** can be developed **in parallel** with PRs #22–#24 (different module:
  `gui_agent/` vs `terminal/`). It's the GUI Agent scaffold with AT-SPI client.
- **PR #26** integrates GUI tools into the Controller: risk classification, COW preview,
  audit logging. Depends on both the terminal (#24) and the GUI Agent (#25).
- **PR #27** adds app-specific APIs (LibreOffice UNO). Builds on the GUI Agent.
- **PR #28** is the RPA Bridge. Independent of GUI Agent internals but depends on the
  tool dispatch pattern established in #26.
- **PR #29** adds the escalation path (Tier B → Tier C) and RPA-specific HITL
  (screenshot-with-crosshair preview, timeout enforcement).
- **PR #30** is the final polish: additional app APIs, accessibility, performance,
  security audit, CI gates G16–G22, and doc updates.

## §4 — Files touched per PR

### PR #22 — CoT events + daemon emission (`feature/phase6t-cot-events`)

**Module:** `dual-brain/controller/` | ~150 lines production + ~80 lines test

| Action | File | Change |
|---|---|---|
| MOD | `controller/turn_events.py` | Add `CotEvent` dataclass: `step_index`, `step_name`, `step_state` (`pending` / `active` / `done` / `failed`), `heading`, `body`, `data: dict`, `timestamp_ms` |
| MOD | `controller/main.py` | Add `_emit_cot()` helper. Call it at each of the 14 pipeline steps in `run_turn_streaming()`. Emit `CotEvent` with structured body derived from step output (risk classifier result, schema validation status, etc.) |
| MOD | `controller/daemon.py` | Handle `CotEvent` in worker thread → send `turn.cot` JSON-RPC notification to connected client |
| MOD | `controller/client.py` | Handle `turn.cot` notification in `_reader_loop()`. Add `_on_cot()` callback. `ClientRepl` prints CoT in verbose mode |

**Tests (~14):**
- Unit: `CotEvent` construction, serialization, field validation (~4)
- Unit: `step_state` transitions — `pending` → `active` → `done`/`failed`, invalid values rejected (~2)
- Integration: daemon emits `turn.cot` during a turn, client receives all 14 steps (~4)
- Regression: existing `turn.progress`/`turn.token`/`turn.result` unaffected (~4)

---

### PR #23 — Textual TUI scaffold (`feature/phase6t-tui-scaffold`)

**Module:** `dual-brain/terminal/` (new package) | ~600 lines production + ~120 lines test

| Action | File | Change |
|---|---|---|
| NEW | `terminal/__init__.py` | Package init |
| NEW | `terminal/app.py` | Textual `App` subclass. Layout: `Vertical(InputBar, Horizontal(ExecutionPanel, CompanionPanel), StatusBar)`. Input bar at TOP (ADR-18). Resizable divider. Status bar with F-key legend. `$NO_COLOR` / high-contrast CSS. |
| NEW | `terminal/execution.py` | Left panel widget. PTY-embedded shell via `textual-terminal` or subprocess fallback. Scrollback buffer. Monospace output, green PS1 prompt, raw command output. |
| NEW | `terminal/companion.py` | Right panel widget (ADR-19). Dual-mode: CoT step cards with `s-pending`/`s-active`/`s-done`/`s-fail` visual states during execution → Interpretation view (summary, bar charts, actionable suggestions, tier badge) after completion. Header switches between "Chain of Thought" and "Interpretation". Collapsible sections. Verbosity levels (minimal/normal/verbose/debug). |
| NEW | `terminal/input_bar.py` | Top input bar widget. Mode indicator (Shell/NL), `#` prefix detection for NL routing, glow border in NL mode, disabled state ("working…") during execution. Label: "Type a command or # for natural language". |
| NEW | `terminal/styles.css` | Dark OLED theme (ADR-10). `#0a0a0e` background, `#818cf8` indigo accent, `#34d399` PS1 green. Text-shadow glow on NL elements. Pulsing status dot. `$NO_COLOR` overrides for accessibility. |
| MOD | `pyproject.toml` | Add `textual>=0.80`, `rich>=13` to `[project.dependencies]`. Add `terminal` to `[project.scripts]` or console entry point. |

**Tests (~14):**
- Textual snapshot: split-pane renders correctly at 80x24, 120x40, 200x60 (~3)
- Textual snapshot: right panel collapsed, left panel collapsed (~2)
- Unit: `CompanionPanel` renders CoT step cards during `turn.cot`, switches to interpretation on `turn.result` (~3)
- Unit: `InputBar` renders at top, shows mode indicator, glow border in NL mode (~2)
- Unit: dark OLED theme colors applied (`#0a0a0e`, `#818cf8`, `#34d399`) (~2)
- Unit: verbosity cycling (F5) changes detail level (~2)

---

### PR #24 — Input router + NL mode + HITL (`feature/phase6t-input-router`)

**Module:** `dual-brain/terminal/` | ~350 lines production + ~120 lines test

| Action | File | Change |
|---|---|---|
| NEW | `terminal/input_router.py` | `InputRouter` class. Detects `#` prefix at column 0, strips prefix, routes to daemon client. Manages NL mode state (Shell/NL toggle). `Ctrl+Space` one-shot handler. Edge case handling (quoted `#`, leading space, inline comment). |
| NEW | `terminal/presenter.py` | `AiTerminalPresenter(HitlPresenter)`. Renders approval prompt in left panel. Shows full context (risk, COW, element preview) in right panel. Registered via `@register_presenter("ai_terminal")`. |
| MOD | `terminal/app.py` | Wire `InputRouter` to keyboard event handler. Wire `AiTerminalPresenter` for HITL callbacks. Add NL mode indicator to status bar. Handle `F2` toggle and `Ctrl+Space` one-shot. |
| MOD | `controller/__main__.py` | Add `--terminal` CLI flag (mutually exclusive with `--repl`, `--daemon`, `--connect`, `--safe-mode`). When `--terminal`: start daemon in background thread, launch Textual app as foreground. |
| MOD | `controller/config.py` | Add `[terminal]` config section: `split_ratio`, `explanation_verbosity`, `shell_explanation`, `cot_scrollback`, `nl_prefix`. Add `[terminal.keys]` for keybindings. Validate no conflicts with HITL keymap. |

**Tests (~15):**
- Unit: `InputRouter` — `#` prefix detection for all edge cases from design doc (~6)
- Unit: NL mode toggle state machine (Shell→NL→Shell, one-shot→Shell) (~3)
- Unit: `AiTerminalPresenter` — renders tier label, respects lockout, returns Decision (~3)
- Integration: `--terminal` flag recognized, mutually exclusive with other modes (~2)
- Config: `[terminal]` section parsed, defaults applied, bad values rejected (~1)

---

### PR #25 — GUI Agent scaffold + AT-SPI (`feature/phase6t-gui-agent`)

**Module:** `dual-brain/gui_agent/` (new package) | ~600 lines production + ~150 lines test

| Action | File | Change |
|---|---|---|
| NEW | `gui_agent/__init__.py` | Package init |
| NEW | `gui_agent/agent.py` | `GuiAgent` class. Subprocess entry point. Landlock sandbox: `$HOME` (ro), `/tmp/icebreaker-gui/` (rw). Seccomp: D-Bus sockets only. Accepts commands from Controller via AF_UNIX JSON-RPC. Timeout enforcement per operation. |
| NEW | `gui_agent/atspi.py` | `AtSpiClient` class. D-Bus connection to `org.a11y.atspi.Registry`. Tree query: find elements by role, name, label. Element interaction: click, type, select, get_value. Element verification: re-check position before action. AT-SPI path → stable locator mapping. |
| NEW | `gui_agent/screenshots.py` | `ScreenshotManager` class. D-Bus call to `org.freedesktop.portal.Screenshot` (Wayland) or `org.gnome.Shell.Screenshot` (X11/GNOME). Capture full screen or window. SHA-256 hash. Store in `/tmp/icebreaker-gui/` with 0600 perms. Cleanup on session end. Retention limit (configurable, default 50). |
| NEW | `gui_agent/sandbox.py` | `apply_gui_sandbox()`. Landlock ruleset for GUI Agent: `$HOME` read-only, `/tmp/icebreaker-gui/` read-write, D-Bus socket paths. Seccomp filter: allow `socket(AF_UNIX)`, `connect`, `sendmsg`, `recvmsg`, `openat`, `read`, `write`, `mmap`, `close`. Deny `execve`, `mount`, `ptrace`, `/dev/uinput`. |
| NEW | `gui_agent/protocol.py` | JSON-RPC protocol between Controller and GUI Agent. Methods: `gui.screenshot`, `gui.find_element`, `gui.get_window_list`, `gui.get_element_tree`, `gui.click`, `gui.type`, `gui.select`. Response includes before/after screenshot hashes. |
| MOD | `pyproject.toml` | Add `PyGObject>=3.42`, `pyatspi>=2.46`, `Pillow>=10.0` to `[project.optional-dependencies.gui]`. |

**Tests (~20):**
- Unit: `AtSpiClient` tree query with mock D-Bus service (~5)
- Unit: `ScreenshotManager` capture, hash, store, cleanup, retention limit (~4)
- Unit: `apply_gui_sandbox()` constructs correct Landlock/Seccomp profiles (~3)
- Unit: JSON-RPC protocol serialization/deserialization (~3)
- Integration: GUI Agent subprocess starts, applies sandbox, responds to ping (~2)
- Adversarial: element selector with metacharacters rejected by schema (~3)

---

### PR #26 — GUI tools + risk + COW + audit (`feature/phase6t-gui-tools`)

**Module:** `dual-brain/controller/` + `dual-brain/gui_agent/` | ~400 lines production + ~120 lines test

| Action | File | Change |
|---|---|---|
| MOD | `controller/turn_events.py` | Add `GuiEvent` dataclass: `phase` (preview/executing/complete), `app`, `window_title`, `element`, `action`, `screenshot_before_hash`, `screenshot_after_hash`, `predicted_outcome` |
| MOD | `controller/main.py` | Add GUI dispatch path: if intent action starts with `gui.*`, route to GUI Agent instead of mcpd. Emit `GuiEvent` at preview and completion. Integrate screenshot-based COW: before-screenshot → HITL (if Tier 2+) → execute → after-screenshot. |
| MOD | `controller/daemon.py` | Handle `GuiEvent` → send `turn.gui` notification |
| MOD | `controller/client.py` | Handle `turn.gui` in reader loop, add `_on_gui()` callback |
| MOD | `controller/risk_classifier.py` | Add GUI tool categories: `GUI_READONLY_TOOLS` (Tier 0), `GUI_WRITE_TOOLS` (Tier 1 for user docs, Tier 2 for system UI), `GUI_SYSTEM_TOOLS` (Tier 2), `GUI_DESTRUCTIVE_TOOLS` (Tier 3). Add classification logic after existing tool checks. |
| MOD | `controller/_mcpd_tools.py` | Add GUI tools to `ALL_TOOLS`. Add to appropriate tier sets. (Manual edit, not auto-generated — GUI tools are not in mcpd.) |
| MOD | `controller/audit.py` | Add optional GUI extra fields to `write_fields()`: `execution_tier`, `element_locator`, `element_role`, `element_name`, `app_name`, `window_title`, `screenshot_before_hash`, `screenshot_after_hash`, `a11y_tree_hash`. Redaction: window titles and element names sanitized, text typed into password fields redacted. |
| MOD | `controller/verifier.py` | Extend QB verification schema for GUI: add optional `concerns` array (`wrong_element`, `unintended_side_effect`, `timing_issue`, `accessibility_violation`) and `predicted_state` string. |
| MOD | `controller/config.py` | Add `[gui]` config section: `enabled`, `screenshot_dir`, `screenshot_retention`, `prefer_app_api`, `a11y_timeout_ms`. |
| MOD | `terminal/companion.py` | Render `GuiEvent` in explanation panel: show app name, element, before/after screenshot hashes, predicted outcome. |

**Tests (~18):**
- Unit: risk classifier — GUI readonly → Tier 0, write → Tier 1/2, destructive → Tier 3, RPA → Tier 3 (~6)
- Unit: GUI dispatch routing in Controller — `gui.*` → GUI Agent, `fs.*` → mcpd (~3)
- Unit: audit extra fields written correctly, GUI-specific redaction (~3)
- Unit: verifier accepts/rejects GUI tool calls with concerns (~2)
- Integration: full GUI turn — NL input → intent → GUI Agent → screenshot → result → audit (~2)
- Adversarial: GUI element selector injection attempts rejected (~2)

---

### PR #27 — App API registry + LibreOffice PoC (`feature/phase6t-app-apis`)

**Module:** `dual-brain/gui_agent/` | ~450 lines production + ~80 lines test

| Action | File | Change |
|---|---|---|
| NEW | `gui_agent/app_apis/__init__.py` | App API package init |
| NEW | `gui_agent/app_apis/registry.py` | `@register_app_api(app_name)` decorator. `get_app_api(app_name) → AppApi`. `list_app_apis() → tuple[str, ...]`. Follows same pattern as `presenters/registry.py`. |
| NEW | `gui_agent/app_apis/base.py` | `AppApi` ABC: `supports(window_title) → bool`, `execute(action, params) → dict`, `capabilities() → list[str]`. |
| NEW | `gui_agent/app_apis/libreoffice.py` | `LibreOfficeApi(AppApi)`. UNO bridge via D-Bus (`com.sun.star.*`). Supports: open/save/close document, insert/delete/format cells (Calc), insert/delete/navigate slides (Impress), format text (Writer). Returns structured results, not raw UNO objects. |
| NEW | `gui_agent/app_apis/generic_atspi.py` | `GenericAtSpiApi(AppApi)`. Fallback for any app without a dedicated wrapper. Uses `AtSpiClient` for all interactions. Always returns `supports() = True`. |
| MOD | `gui_agent/agent.py` | On incoming GUI tool call: check if an app-specific API is registered for the target window. If yes, use it. If no, fall back to `GenericAtSpiApi`. Log which API path was taken. |
| MOD | `controller/config.py` | Add `gui.prefer_app_api = true` config (already specced in #26, wired here). |

**Tests (~12):**
- Unit: registry decorator, `get_app_api()`, `list_app_apis()` (~3)
- Unit: `LibreOfficeApi.supports()` matches LibreOffice window titles (~2)
- Unit: `LibreOfficeApi.execute()` with mock UNO bridge (~3)
- Unit: `GenericAtSpiApi` fallback for unknown apps (~2)
- Integration: agent selects LibreOffice API for `.ods` window, generic for unknown app (~2)

---

### PR #28 — RPA Bridge + Robot Framework (`feature/phase6t-rpa-bridge`)

**Module:** `dual-brain/rpa_bridge/` (new package) | ~500 lines production + ~120 lines test

| Action | File | Change |
|---|---|---|
| NEW | `rpa_bridge/__init__.py` | Package init |
| NEW | `rpa_bridge/bridge.py` | `RpaBridge` class. Subprocess entry point. Landlock sandbox: `/tmp/icebreaker-rpa/` (rw), `$HOME` (ro), `/dev/uinput` (rw, group-gated). Imports `robot.api.TestSuite`. Executes keyword sequences. Screenshot at each keyword. Hard timeout (configurable, default 30s). SIGKILL on expiry. |
| NEW | `rpa_bridge/workflow_gen.py` | `WorkflowGenerator` class. Translates PB tool call params into Robot Framework keyword sequences. Max 20 keywords per workflow. Validates keyword names against allowlist (no arbitrary Robot Framework keywords). Generates `.robot` file in `/tmp/icebreaker-rpa/`. |
| NEW | `rpa_bridge/image_match.py` | `ImageMatcher` class. Template matching via Pillow (no OpenCV dependency). Confidence threshold (configurable, default 0.85). Returns bounding box + confidence. Used for `rpa.find_by_image` when AT-SPI can't locate elements. |
| NEW | `rpa_bridge/sandbox.py` | `apply_rpa_sandbox()`. Landlock: `/tmp/icebreaker-rpa/` (rw), `$HOME` (ro), `/dev/uinput` (rw). Seccomp: same as GUI Agent + `ioctl` for uinput. More permissive than GUI Agent — this is why RPA is always Tier 3. |
| NEW | `rpa_bridge/protocol.py` | JSON-RPC protocol between Controller and RPA Bridge. Methods: `rpa.execute_workflow`, `rpa.find_by_image`, `rpa.list_workflows`. Response includes per-keyword screenshot hashes and timing. |
| MOD | `pyproject.toml` | Add `robotframework>=7.0`, `rpaframework>=28.0` to `[project.optional-dependencies.rpa]`. |

**Tests (~16):**
- Unit: `WorkflowGenerator` — intent → keyword sequence for common patterns (~4)
- Unit: `WorkflowGenerator` — rejects > 20 keywords, rejects disallowed keywords (~3)
- Unit: `ImageMatcher` — finds template in test image, returns bounding box (~2)
- Unit: `ImageMatcher` — below confidence threshold → not found (~1)
- Unit: `apply_rpa_sandbox()` constructs correct profiles (~2)
- Integration: RPA Bridge subprocess starts, runs a 3-keyword workflow, screenshots captured (~2)
- Timeout: workflow exceeding timeout is SIGKILL'd, partial results returned (~2)

---

### PR #29 — Escalation + RPA HITL + timeout (`feature/phase6t-escalation`)

**Module:** `dual-brain/controller/` | ~300 lines production + ~100 lines test

| Action | File | Change |
|---|---|---|
| MOD | `controller/turn_events.py` | Add `RpaEvent` dataclass: `phase` (preview/executing/step/complete), `workflow_name`, `keyword_index`, `keyword_total`, `current_keyword`, `timeout_remaining_ms`, `screenshot_path`, `screenshot_hash` |
| MOD | `controller/main.py` | Add escalation logic: if GUI Agent returns `element_not_found` with `reason: "no_a11y_tree"`, check `[rpa] enabled`. If enabled, escalate to RPA Bridge. If disabled, return error with guidance. Emit `RpaEvent` at each step. Enforce hard timeout. HITL gate for every RPA operation (no bypass, no trust grant). |
| MOD | `controller/daemon.py` | Handle `RpaEvent` → send `turn.rpa` notification |
| MOD | `controller/client.py` | Handle `turn.rpa` in reader loop, add `_on_rpa()` callback |
| MOD | `controller/risk_classifier.py` | Add `RPA_TOOLS = {"rpa.execute_workflow", "rpa.record_macro"}`. Any `rpa.*` action → `Tier.HIGH` unconditionally (hard floor, no conditional logic). Add `rpa.find_by_image` and `rpa.list_workflows` to `TIER0_TOOLS`. |
| MOD | `controller/trust_store.py` | Add explicit check: if `action.startswith("rpa.")` and `tier >= Tier.HIGH`, return `None` (never grantable). This is defense-in-depth — the hard floor in risk_classifier already blocks it, but the trust store double-checks. |
| MOD | `controller/audit.py` | Add RPA extra fields: `execution_tier: "rpa_fallback"`, `rpa_keywords_executed`, `rpa_timeout_ms`, `rpa_elapsed_ms`, `rpa_screenshot_hashes: list[str]`, `rpa_fallback_reason`. |
| MOD | `controller/hitl.py` | Extend `HitlDisplayData` with optional `screenshot_path` and `crosshair_coords: tuple[int, int]` for RPA visual preview. `TerminalPresenter` renders crosshair description. `AiTerminalPresenter` shows screenshot thumbnail in right panel with crosshair overlay. |
| MOD | `controller/config.py` | Add `[rpa]` config section: `enabled = false`, `timeout_seconds = 30`, `max_keywords_per_workflow = 20`, `screenshot_every_step = true`. |
| MOD | `terminal/companion.py` | Render `RpaEvent` in explanation panel: show workflow name, keyword progress, timeout countdown, per-step screenshots, red border for RPA indicator. |

**Tests (~14):**
- Unit: escalation — GUI `element_not_found` + RPA enabled → escalate (~2)
- Unit: escalation — GUI `element_not_found` + RPA disabled → error with guidance (~2)
- Unit: RPA always Tier 3, trust store rejects RPA grants (~3)
- Unit: RPA timeout — workflow exceeding limit → SIGKILL + audit `TOOL_TIMEOUT` (~2)
- Unit: HITL display data includes crosshair coords for RPA preview (~1)
- Integration: full escalation path — NL → GUI fail → RPA → HITL → execute → audit (~2)
- Regression: CLI and GUI paths unaffected by RPA additions (~2)

---

### PR #30 — Polish + security audit + CI gates (`feature/phase6t-polish`)

**Module:** Multiple (final integration) | ~400 lines production + ~80 lines test

| Action | File | Change |
|---|---|---|
| NEW | `gui_agent/app_apis/firefox.py` | `FirefoxApi(AppApi)`. DevTools Protocol via D-Bus or websocket. Supports: navigate, screenshot tab, get DOM element, click, type in form. |
| NEW | `gui_agent/app_apis/gnome_files.py` | `GnomeFilesApi(AppApi)`. `org.gnome.Nautilus` D-Bus interface. Supports: open folder, select file, rename. |
| NEW | `cx-distro/tests/test_gui_agent.sh` | Static checks: sandbox profile validates, protocol schema compiles, no raw X11 imports, no `/dev/uinput` access in GUI Agent. |
| NEW | `cx-distro/tests/test_rpa_bridge.sh` | Static checks: sandbox profile validates, Robot Framework keyword allowlist complete, timeout enforcement path exists. |
| MOD | `dual-brain/controller/ci.sh` | Add G16–G22 gates (see §5 below). |
| MOD | `terminal/companion.py` | Accessibility: screen reader support for GUI/RPA traces. Each GUI step announced as "GUI Step N: [action] on [element] in [app]". RPA steps announced with timeout countdown. |
| MOD | `gui_agent/atspi.py` | Performance: lazy AT-SPI tree loading (don't fetch full tree for simple queries). Cache window list for 2s (configurable). |
| MOD | `gui_agent/screenshots.py` | Performance: JPEG compression for screenshots (configurable quality). Thumbnail generation for terminal display. |
| MOD | `CLAUDE.md` | Add GUI/RPA tools to security-critical files table. Update phase status. Add GUI/RPA to forbidden patterns section. |
| MOD | `README.md` | Update architecture diagram to include GUI Agent and RPA Bridge. Update directory map. Update test count. |
| MOD | `docs/ARCHITECTURE.md` | Add GUI automation and RPA sections. Update component hierarchy. |
| MOD | `docs/implementation_plan.md` | Add Phase 6T close-out note. Update phase status table. |

**Tests (~10):**
- Unit: `FirefoxApi.supports()` matches Firefox window titles (~1)
- Unit: `GnomeFilesApi.supports()` matches Nautilus windows (~1)
- Static: `test_gui_agent.sh` passes (~1)
- Static: `test_rpa_bridge.sh` passes (~1)
- CI: G16–G22 all pass (~6)

---

## §5 — CI gates (G16–G22)

Gates continue from Phase 6's G15. Added to `dual-brain/controller/ci.sh` via delegation
to `cx-distro/tests/` and new gate scripts.

| Gate | Name | What it checks |
|---|---|---|
| G16 | GUI Agent sandbox integrity | `gui_agent/sandbox.py` constructs correct Landlock ruleset. No raw X11 imports (`import Xlib`, `xdotool`). No `/dev/uinput` in GUI Agent (only in RPA Bridge). Verify with `grep -r`. |
| G17 | GUI tool schema validation | Every `gui.*` tool has a JSON schema. Schemas are parseable by `jsonschema`. `_mcpd_tools.py` includes all GUI tools in correct tier sets. |
| G18 | RPA sandbox integrity | `rpa_bridge/sandbox.py` includes `/dev/uinput`. RPA tools are in `RPA_TOOLS` set. All RPA tools → Tier 3 in risk classifier. Trust store rejects RPA grants. |
| G19 | Display sanitization | All GUI-sourced strings pass through `_sanitize_display()`. Grep for raw `window_title` or `element_name` rendering without sanitization → fail. |
| G20 | CoT event coverage | `run_turn_streaming()` emits `CotEvent` for all 14 pipeline steps. Test by counting emitted events in a mock turn. |
| G20.1 | Companion panel dual-mode | `CompanionPanel` renders CoT step cards (with `step_state` progression) during `turn.cot` events and switches to interpretation view on `turn.result`. Textual snapshot test. |
| G21 | Screenshot cleanup | `ScreenshotManager.cleanup()` removes all files in scratch dir. Verify no screenshots leak across sessions. |
| G22 | RPA timeout enforcement | RPA Bridge SIGKILL's workflows exceeding `timeout_seconds`. Test with a deliberately slow keyword sequence. |

## §6 — Test budget

| PR | New Tests | Notes |
|---|---|---|
| #22 CoT events | ~14 | Event construction, `step_state` transitions, daemon emission, client receipt |
| #23 TUI scaffold | ~14 | Textual snapshots, companion dual-mode, input bar, OLED theme, verbosity |
| #24 Input router | ~15 | Edge cases, NL mode, HITL presenter, config |
| #25 GUI Agent | ~20 | AT-SPI mock, screenshots, sandbox, protocol, adversarial |
| #26 GUI tools | ~18 | Risk classification, dispatch, audit, verifier, integration |
| #27 App APIs | ~12 | Registry, LibreOffice mock, generic fallback |
| #28 RPA Bridge | ~16 | Workflow gen, image match, sandbox, timeout |
| #29 Escalation | ~14 | Escalation logic, HITL, trust store, audit, regression |
| #30 Polish | ~10 | App APIs, static checks, CI gates |
| **Total new** | **~135** | |

Running total: 1446 + ~135 = **~1581 tests**.

## §7 — Configuration additions

New TOML sections added to `controller.toml`:

```toml
[terminal]
split_ratio = 0.6                    # left panel width (0.0–1.0)
explanation_verbosity = "normal"     # minimal | normal | verbose | debug
shell_explanation = "none"           # none | man | exit_code
cot_scrollback = 50                  # number of CoT traces to keep
nl_prefix = "#"                      # character that triggers NL mode

[terminal.appearance]
theme = "oled-dark"                  # oled-dark | dark | light | high-contrast
accent_color = "#818cf8"             # indigo accent for NL elements
ps1_color = "#34d399"                # green PS1 prompt
glow_effects = true                  # text-shadow glow on NL mode elements
input_position = "top"               # top | bottom (ADR-18: default top)

[terminal.interpretation]
show_bar_charts = true               # render bar charts in interpretation view
show_suggestions = true              # show actionable suggestions
show_tier_badge = true               # show tier classification badge
max_chart_items = 10                 # limit items in bar charts

[terminal.keys]
toggle_nl = "f2"
oneshot_nl = "ctrl+space"
resize = "f9"
toggle_right_panel = "f3"
toggle_left_panel = "f4"
cycle_verbosity = "f5"
gui_log = "f6"
quit = "f10"

[gui]
enabled = true                       # enable GUI automation tools
screenshot_dir = "/tmp/icebreaker-gui"
screenshot_retention = 50            # keep last N screenshots
prefer_app_api = true                # prefer D-Bus app APIs over generic AT-SPI
a11y_timeout_ms = 5000               # timeout for AT-SPI tree queries

[rpa]
enabled = false                      # RPA disabled by default (opt-in)
timeout_seconds = 30                 # hard timeout per RPA workflow
max_keywords_per_workflow = 20       # limit workflow complexity
screenshot_every_step = true         # screenshot at each Robot Framework keyword
```

All new sections are backward-compatible: an older config without `[terminal]`,
`[gui]`, or `[rpa]` loads without error, using the defaults above.

## §8 — Rollout

1. Each PR is a feature branch off `main`. No commits directly on `main`.
2. PR title format: `phase6t(scope): description` (e.g. `phase6t(cot-events): CotEvent
   dataclass + daemon emission`).
3. PR body: **no Claude attribution** (no `Co-Authored-By:`, no "Generated with Claude
   Code" footer). Include a "Risk & rollback" section.
4. After merge, update §0 status table in this file.
5. After PRs #22–#24 land, tag `phase6t-terminal-complete` (first shippable milestone).
6. After PRs #25–#27 land, tag `phase6t-gui-complete`.
7. After PRs #28–#29 land, tag `phase6t-rpa-complete`.
8. After PR #30 lands, tag `phase6t-complete` and write the closeout note in
   `docs/implementation_plan.md`.

## §9 — Hardware & environment test matrix

| Config | Spec | Desktop | Test |
|---|---|---|---|
| Dev (macOS) | M4, 32GB | N/A | Terminal TUI, CoT events, unit tests. No GUI/RPA (no AT-SPI). |
| VM (Ubuntu GNOME) | 4 vCPU, 8GB, GNOME 42+ | Wayland + X11 | Full GUI automation: LibreOffice, Firefox, GNOME Files. AT-SPI tree inspection. Screenshot capture. |
| VM (Ubuntu KDE) | 4 vCPU, 8GB, KDE Plasma 5.27+ | Wayland + X11 | GUI automation with KDE apps. AT-SPI compatibility. |
| ISO (bare metal) | Mid-range: i7, 32GB, NVMe | GNOME | Full stack: terminal + GUI + RPA. Boot-to-AI-ready timing. |

GUI and RPA tests require a Linux desktop with a display server. macOS CI can run
terminal and unit tests only. GUI integration tests require a VM or bare metal with
GNOME or KDE.

## §10 — Out-of-scope (explicit, deferred to Phase 7+)

| Item | Reason |
|---|---|
| Voice input (speech-to-text) | Separate feature with its own dependency chain (whisper.cpp or system dictation). Phase 7+. |
| Video recording of GUI actions | Screenshots are sufficient for audit. Video adds storage/privacy concerns. Phase 7+. |
| Multi-step GUI workflow chaining | Phase 6T does one GUI action per NL turn. Multi-step decomposition is Phase 7+. |
| OpenCV for image matching | Pillow template matching is sufficient. OpenCV adds a heavy C dependency. Phase 7+ if needed. |
| Custom Robot Framework libraries | Phase 6T uses `RPA.Desktop` and `RPA.Images` only. Custom libraries are Phase 7+. |
| Wayland-native input injection | Phase 6T uses D-Bus/AT-SPI only (no ydotool). Wayland input injection is Phase 7+. |
| KDE-specific app APIs | Phase 6T targets GNOME apps only. KDE wrappers are Phase 7+. |
| Browser extension for deep web automation | Phase 6T uses Firefox DevTools Protocol. A dedicated extension is Phase 7+. |
| Install-to-disk (Calamares) | Deferred from Phase 6. Still Phase 7. |
| Man pages | Deferred from Phase 6. Still Phase 7. |

## §11 — Risk register (Phase 6T specific)

| # | Risk | Mitigation |
|---|---|---|
| P6T-R1 | AT-SPI tree unavailable on target app | Graceful fallback to RPA (if enabled) or error with guidance. GUI Agent returns `element_not_found` with reason. |
| P6T-R2 | `textual-terminal` PTY embedding too immature | Fallback: subprocess-based left panel (no embedded PTY). Shell output rendered in `RichLog`. Tab completion and job control degraded but functional. |
| P6T-R3 | Screenshot capture blocked by Wayland compositor | Use `org.freedesktop.portal.Screenshot` (portal API). If portal denied, return error — don't fall back to X11 screengrab. |
| P6T-R4 | LibreOffice UNO bridge version skew | Pin minimum LibreOffice version (7.4+). UNO API is stable. Test against two versions in CI matrix. |
| P6T-R5 | RPA coordinate drift (DPI/resolution change) | `ImageMatcher` verifies display geometry before click. Abort on mismatch. Re-screenshot and re-match. |
| P6T-R6 | Robot Framework dependency size | `robotframework` is ~10MB. `rpaframework` is ~50MB. Both are optional (`[project.optional-dependencies.rpa]`). Only installed if `[rpa] enabled = true`. |
| P6T-R7 | D-Bus session bus eavesdropping | GUI Agent uses D-Bus method calls, not signals. Calls are point-to-point. Monitor with `dbus-monitor` in security audit. |
| P6T-R8 | GUI Agent sandbox escape via D-Bus | D-Bus policy file restricts GUI Agent's allowed destinations to AT-SPI registry + known app APIs. No `org.freedesktop.systemd1` access. |
| P6T-R9 | RPA `/dev/uinput` privilege escalation | `/dev/uinput` requires `input` group membership. RPA Bridge runs as user, not root. Seccomp blocks `setuid`/`setgid`. Time-boxed (30s). |
| P6T-R10 | Textual version incompatibility | Pin `textual>=0.80,<1.0`. Snapshot tests catch rendering regressions. |
| P6T-R11 | GUI operation modifies system state unexpectedly | Screenshot-based COW preview before all write ops. After-screenshot for audit. User sees predicted outcome. Undo guidance shown (e.g., "Ctrl+Z in LibreOffice"). |
| P6T-R12 | RPA keyword injection | `WorkflowGenerator` validates keyword names against an explicit allowlist. No arbitrary Robot Framework keywords. Max 20 per workflow. |
| P6T-R13 | Input bar discoverability | Top-positioned input bar (ADR-18) may be mistaken for a toolbar or title bar by new users. Mitigation: pulsing glow border on first launch, `?` help shows input bar purpose, label text "Type a command or # for natural language". |
| P6T-R14 | Mode confusion (Shell vs NL) | Users may not realize they are in NL mode vs Shell mode. Mitigation: distinct background color for NL mode, mode label always visible in input bar, `Esc` always returns to Shell mode, visual flash on mode switch. |

## §12 — Acceptance criteria

### PR #22 — CoT events + daemon emission

- `CotEvent` dataclass constructed with all required fields.
- `run_turn_streaming()` emits `CotEvent` for all 14 pipeline steps.
- Daemon forwards `turn.cot` notification to connected client.
- `ClientRepl` prints CoT in verbose mode, ignores in normal mode.
- Existing `turn.progress`/`turn.token`/`turn.result` behavior unchanged.

### PR #23 — Textual TUI scaffold

- `python3 -m terminal` launches a split-pane Textual app.
- Input bar renders at top of terminal, above the split pane (ADR-18).
- Input bar shows mode indicator (Shell/NL), glow border in NL mode, disabled state during execution.
- Left panel renders terminal output with green PS1 prompt and raw command output.
- Right panel renders CoT step cards (with `pending`/`active`/`done`/`failed` states) during execution, switches to interpretation view (summary, bar charts, suggestions, tier badge) after completion (ADR-19).
- Dark OLED theme matches mockup v4 color palette (`#0a0a0e`, `#818cf8`, `#34d399`).
- `F3` collapses right panel. `F4` collapses left panel. `F9` resizes.
- `$NO_COLOR` suppresses colors, glow effects, and uses ASCII box drawing.
- Minimum 80-column width auto-hides right panel.

### PR #24 — Input router + NL mode + HITL

- `#` at column 0 routes to daemon, all other input to PTY/shell.
- All edge cases from design doc handled (quoted `#`, leading space, inline `#`).
- `F2` toggles NL mode. Prompt changes. `!` prefix runs shell in NL mode.
- `Ctrl+Space` one-shot NL: one line NL, then back to shell mode.
- `--terminal` flag recognized, mutually exclusive with other modes.
- HITL approval renders in left panel with context in right panel.

### PR #25 — GUI Agent scaffold + AT-SPI

- GUI Agent starts as subprocess with Landlock + Seccomp applied.
- AT-SPI tree query returns elements matching role/name/label.
- Screenshot capture works on GNOME (both X11 and Wayland via portal).
- Screenshots stored with 0600 perms, hashed, cleaned on session end.
- No raw X11 imports anywhere in `gui_agent/`.
- No `/dev/uinput` access in GUI Agent (only in RPA Bridge).

### PR #26 — GUI tools + risk + COW + audit

- `gui.screenshot` and `gui.find_element` classified as Tier 0.
- `gui.click` and `gui.type` classified as Tier 1 for user documents.
- `gui.keystroke` classified as Tier 3 (always HITL).
- GUI write operations show before-screenshot preview.
- Audit entries include `execution_tier`, `element_locator`, screenshot hashes.
- Explanation panel renders GUI events with app name and element.

### PR #27 — App API registry + LibreOffice PoC

- `@register_app_api("libreoffice")` registers the LibreOffice wrapper.
- LibreOffice API opens, formats, and saves a Calc document.
- Generic AT-SPI fallback works for apps without dedicated wrappers.
- `gui.prefer_app_api = true` routes to app API when available.

### PR #28 — RPA Bridge + Robot Framework

- RPA Bridge starts as subprocess with Landlock + Seccomp + `/dev/uinput`.
- `WorkflowGenerator` produces valid Robot Framework keyword sequences.
- Max 20 keywords per workflow enforced.
- Keyword allowlist prevents arbitrary Robot Framework keywords.
- `ImageMatcher` locates template with > 85% confidence.
- Screenshot captured at every keyword step.
- Hard timeout (30s default) kills workflow on expiry.

### PR #29 — Escalation + RPA HITL + timeout

- GUI `element_not_found` + RPA enabled → automatic escalation to Tier C.
- GUI `element_not_found` + RPA disabled → error with guidance message.
- Every RPA operation is Tier 3 (no exceptions).
- Trust store rejects all `rpa.*` grants (defense-in-depth).
- HITL display includes crosshair coordinates for RPA preview.
- Timeout: workflow exceeding limit → SIGKILL + audit `TOOL_TIMEOUT`.
- Explanation panel shows escalation reasoning and RPA step progress.

### PR #30 — Polish + security audit + CI gates

- G16–G22 CI gates pass.
- Firefox and GNOME Files app APIs registered and functional.
- Screen reader announces GUI/RPA steps correctly.
- AT-SPI tree lazy-loaded (no full tree fetch for simple queries).
- Screenshots JPEG-compressed (configurable quality).
- `CLAUDE.md`, `README.md`, `docs/ARCHITECTURE.md` updated.

### "Phase 6T complete" aggregate

All ten PR acceptance sections above, plus:

- [ ] Terminal TUI works on macOS (dev) and Ubuntu GNOME (VM).
- [ ] GUI automation works on Ubuntu GNOME with LibreOffice and Firefox.
- [ ] RPA fallback works on Ubuntu GNOME for an app without AT-SPI.
- [ ] Terminal appearance matches mockup v4 (dark OLED, top input bar, companion dual-mode).
- [ ] All 1446+ existing tests pass (no regressions).
- [ ] ~135 new tests pass.
- [ ] G16–G22 + G20.1 CI gates green.
- [ ] Security audit: GUI Agent sandbox verified, RPA timeout verified, D-Bus policy reviewed.
- [ ] `docs/phase6t_implementation_plan.md` §0 updated to reflect all milestones as ✅.
- [ ] One human reviewer + module owner LGTM per PR (WF-6).

## §13 — Update protocol for this file

- On every PR merge: update §0 status row, set the PR column, append the merge SHA in
  parens.
- On every milestone closeout: write a 3-bullet closeout note at the bottom of §14
  (what shipped, what's deferred, guidance for next collaborator).
- Do NOT edit the design doc (`docs/ai_terminal_design.md`) to reflect status — status
  lives here.

## §14 — Closeout notes (update as PRs land)

*(Empty — PRs have not started.)*
