# Icebreaker v6.15 — Vision-Grounded UI Automation

**Product:** Icebreaker (AI-native Ubuntu fork)
**Feature:** Fix V — full-blown vision-grounded UI automation for the OC edition + current edition
**Baseline:** v6.14_OC-arm64.iso (SHA `d2c931b4…`, shipped 2026-07-31)
**Plan drafted:** 2026-08-01
**Target ISO:** `v6.15_OC-arm64.iso` / `v6.15-arm64.iso`

---

## Why this feature exists

Fix M (v6.14) gave the OC edition a `iceui` MCP server exposing 12 GUI/RPA tools, all of which route through **AT-SPI accessibility tree lookup**. That works cleanly for GTK/Qt/GNOME native apps, but returns *"element not found"* for Electron apps (Slack, VS Code, Discord), Chrome/Firefox web content, LibreOffice Impress canvas, most games, and cross-arch emulated apps under UTM. Users saw the "AI Terminal" refuse to click things it could clearly see, and gave up.

**Fix V** adds a vision layer: screenshot → cloud VLM (Gemini 2.5 Flash) → structured element list → pixel-level `xdotool` click, with an annotated preview shown to the user before every write. The `iceui` MCP server grows from 12 tools to 24. Both editions get it. The `notify-send`-based preview + trust store make the "ask before you click" UX tolerable enough that grounded automation is actually usable, not a per-action modal parade.

---

## The 8 commits — what each ships, and why

### V.1 — `VisionGrounder` (cloud VLM screen parser)

- **What ships:** the `parse_screen(image) → ParseResult` core. Takes any screenshot, calls Gemini 2.5 Flash vision (Claude Haiku 4.5 fallback), returns a structured list of every clickable element with pixel box + caption + kind + confidence.
- **How it's built:** LiteLLM `completion()` with `response_format={"type": "json_object"}`, prompt-engineered to enumerate interactive elements in reading order. Retries once on malformed JSON with lower temperature. Falls to the fallback backend on rate-limit or provider error. 2-second in-memory cache keyed on screenshot SHA-256 so a `parse_screen` + `grounded_click` chain pays the VLM cost once.
- **Governance:** every call bumps a per-turn cost counter; a call that would breach `cost_ceiling_usd_per_turn` (default $0.01) is refused before touching the network — BP-10 anti-runaway. `reset_turn_cost()` between turns.
- **Where in code:** `dual-brain/gui_agent/vision.py` (~380 LOC). `ElementBox`, `ParseResult`, `VisionGrounder`, `VisionCostCeilingExceeded`, `VisionMalformedResponse`, `VisionAllBackendsFailed`, `VisionDisabled`.
- **Tests:** 15 unit tests in `dual-brain/gui_agent/tests/test_vision.py` — all mocked (no network). Covers cache hit/miss/expiry, malformed-JSON retry, fallback backend, cost ceiling before-the-call, disabled mode, markdown-fence tolerance, ID renumbering, kind-coercion.

### V.2a — `MonitorLayout` (HiDPI + multi-monitor)

- **What ships:** the coord-translation backbone. Detects every physical display via `xrandr --query` and lets callers translate logical (X server) coords ↔ physical (VLM screenshot) pixels.
- **How it's built:** parses `xrandr` output into frozen `Monitor(name, origin_x, origin_y, width, height, scale)` records. HiDPI scale factor is inferred by comparing the connected line's logical resolution to the active mode's physical resolution — a 3840×2160 physical mode on a 1920×1080 logical monitor is a 2× Retina. `hidpi_scale_override` config knob for VM guests where xrandr misreports. 30-second cache so click-heavy loops don't respawn `xrandr` on every action.
- **Fail-safe:** xrandr missing or errors → returns a single-monitor 1× fallback layout. `detect()` never raises.
- **Where in code:** `dual-brain/gui_agent/geometry.py` (~200 LOC).
- **Tests:** 10 unit tests in `test_geometry.py` — single/multi-monitor parse, HiDPI 2×, scale override, xrandr missing/errors fallback, cache dedup, translation roundtrip.

### V.2b — `input_synth` (xdotool wrappers, allowlisted)

- **What ships:** the actuation layer. Every mouse and keyboard action the OC/current edition can perform, wrapped in a Python API: `click`, `right_click`, `double_click`, `hover`, `type_text`, `type_at_coords`, `drag` (with waypoints), `scroll` (4 directions), `press_key` (combos), `key_sequence` (mixed combo + text).
- **How it's built:** every function shells out to `xdotool` with an explicit argv list (no `shell=True` anywhere), bounded by a 5-second subprocess timeout. Every argument passes through a strict validator: coords bounded 0..32768; button/direction/count enumerated; text ≤ 4096 chars and rejects C0/C1 control characters; key combos are `+`-split then each token regex-matched against a ~90-name allowlist. `$(rm -rf /)` and ``ctrl+`whoami` `` are structural rejects, not runtime failures.
- **HiDPI plumbing:** every mouse fn takes an optional `MonitorLayout` (from V.2a) and translates logical → physical before handing to xdotool, so VLM-provided physical coords land on the right pixel.
- **Where in code:** `dual-brain/gui_agent/input_synth.py` (~450 LOC). Returns `ActionResult` matching the shape `atspi.py` produces so callers can't tell whether the click went through the a11y tree or coord path.
- **Tests:** 37 unit tests in `test_input_synth.py` — every mouse/keyboard fn happy path + coord/button/count validation + control-char reject + 4 shell-injection patterns rejected on `press_key` + `key_sequence` fails-fast + subprocess timeout returns failed `ActionResult` + HiDPI translation applied.

### V.2c — This rollup doc

- **What ships:** the doc you're reading. Single-file, 2-minute scan of the whole Fix V shape for anyone (new engineer, reviewer, the user's future self) who doesn't want to read the 400-line plan or the code.
- **How it's built:** plain Markdown, one section per commit V.1..V.8, 4-5 bullets each. Filename encodes product + version + feature + date so it sorts near the v6.14 `docs/OC_edition_privacy.md` note in the repo listing.
- **Purpose:** honest transparency. Users of a "3 months of work" product get a straight answer to *"what's actually in v6.15?"* without a plan-file rabbit hole.
- **Where in code:** `docs/Icebreaker_v6.15_Vision-Grounded-UI-Automation_2026-08-01.md`. Lives next to `docs/OC_edition_privacy.md`.
- **Maintenance:** updated in-place as each remaining commit lands (V.3 through V.8) — the same file, so no drift between "plan" and "shipped".

### V.3 — `annotate` + `trust_store` + `ib-trust` CLI (shipped 2026-08-01, split into 3 sub-commits)

Split into V.3a / V.3b / V.3c for fine-grained bisect surface per user request.

**V.3a — annotate.py** (SHA `f9f9118`, 20 tests): Pillow-based annotated preview PNGs with kind-colored borders (iOS palette — button=red, input=blue, link=green, icon=gray, +8 more), numbered ID chips, target highlight (thick outline + red arrow + 40% dim on non-target regions), confidence signal (dashed border + "?" for < 0.7), corner watermark with timestamp + element count so PNG can't be mistaken for real UI. Fires `notify-send --icon=<png>` so users see what's about to happen *before* the opencode ask prompt lands.

**V.3b — trust_store.py** (SHA `97a694e`, 33 tests): four-tier grant model (`once` / `session` / `persistent` / `deny_always`), wildcards (`*` or `prefix*`, no general globs — narrow by design so wildcards can't sneak past denies), session-id binding for `session` tier, TTL for persistent, never-silent expiry (matching-but-expired persistent grants return denied with explicit "trust expired at ... — ask again" reason). Built-in `_HARD_DENY` frozenset overrides EVERYTHING for gnome-terminal / xterm / konsole / terminator / sudo — defense in depth against a corrupt defaults file. Append-only JSONL at `/var/lib/icebreaker/gui_trust.jsonl` with `fcntl.LOCK_EX` + `fsync` for concurrent-daemon safety.

**V.3c — `ib-trust` CLI + shipped defaults** (SHA `38478c1`, 29 tests): discoverable CLI at `/usr/local/bin/ib-trust`. No-args → full help + current state at a glance. Fuzzy match (`ib-trust add slack click` expands `click` → `gui.grounded_click`). `--dry-run` on every mutation. `ib-trust undo` via a 16-deep per-user JSON ring at `~/.cache/icebreaker/ib_trust_undo.json`. `ib-trust why APP TOOL` explains the decision + matched grant + exits 0/1 for scripting. `ib-trust export/import` for JSONL backup. Stdlib-only coloring (respects `NO_COLOR` + `sys.stdout.isatty()`). Shipped `cx-distro/distro/gui_trust_defaults.jsonl` (~50 lines): 24h auto-approve for 10 read-only tools everywhere; gnome-calculator pre-approved for grounded_click / click_at_coords / press_key; hard-deny for 7 terminal emulators + sudo/keyring/seahorse/polkit-*.

Where in code: `dual-brain/gui_agent/annotate.py`, `dual-brain/gui_agent/trust_store.py`, `dual-brain/scripts/ib_trust.py`, `cx-distro/distro/gui_trust_defaults.jsonl`. Tests split into `test_annotate.py` (20), `test_trust_store.py` (33), `test_ib_trust.py` (29) — 82 total for V.3, all mocked.

### V.4 — Protocol schemas + agent dispatch + mcp_gui_server registration (shipped 2026-08-01, split into 5 sub-commits)

Split into V.4a / V.4b / V.4c / V.4d / V.4e per user request for maximum bisect surface on the most integration-heavy commit in Fix V.

**V.4a — protocol.py schemas** (SHA `a15b6c2`, 59 tests): 12 new constants + 12 `_PARAM_SCHEMAS` entries covering parse_screen, click_at_coords, type_at_coords, drag (with waypoints), scroll, hover, press_key, key_sequence, and the 4 grounded_* orchestrators. `ALL_GUI_METHODS` grows from 8 to 20. Shared schema atoms (`_COORD_SCHEMA`, `_BUTTON_SCHEMA`, `_KEY_COMBO_SCHEMA` with regex-layer defense) dedup across tools. Schema-driven wins: adding a schema entry auto-registers the tool in `mcp_gui_server.py::_build_tools_list()` AND in `gen-oc-config.sh`'s permission-map harvest — zero call-site edits.

**V.4b — agent.py raw pixel/keyboard handlers** (SHA `4fa58df`, 18 tests): 7 `_handle_*` methods for click_at_coords, type_at_coords, drag, scroll, hover, press_key, key_sequence. Each is a thin lambda into V.2b's `input_synth` via a common `_synth_wrap` that lazy-imports (so headless CI can still import agent.py), calls `MonitorLayout.detect()` for HiDPI translation, and converts `ActionResult` → the existing `{success, error, latency_ms, physical_coords, logical_coords, extra}` dict shape. Structured `reason` fields on every failure branch (`xdotool_missing`, `validation_error`, `internal_error`).

**V.4c — agent.py parse_screen handler + notify-send** (SHA `344ccc1`, 19 tests): `_handle_parse_screen` composes 4 subsystems: ScreenshotManager + VisionGrounder + annotate + `_try_notify_send` helper. VisionGrounder is lazy-init on `self._vision` so unit tests can inject a MagicMock; production config threads through via `_extract_vision_config(config)` that accepts either namespace or dict. Best-effort annotate: if Pillow explodes, `preview_path` becomes an `"annotate-failed: ..."` string but the element list still surfaces. `_try_notify_send()` is a 2s-timeout subprocess wrapper — returns False on any failure so the audit chain records `notify_sent=False` rather than raising.

**V.4d — agent.py grounded_* orchestrators** (SHA `a80d24f`, 19 tests): the marquee capability — 4 handlers that turn Fix V from "raw pixel tools" into "natural language → any UI action". `_grounded_orchestrate` shared plumbing for grounded_click/type/scroll (single endpoint); `_handle_grounded_drag` for two-endpoint. Pipeline: capture → parse (cache-aware) → first preview + notify → `_llm_pick_element` (LiteLLM completion with response_format=json_object, markdown-fence stripping, retry once on malformed JSON with temp 0.4→0.0) → re-preview with target highlighted → `_synth_wrap` actuation. Full failure taxonomy with structured `reason`: `llm_pick_import_failed / provider_error / malformed / out_of_range`. Result dict includes `picked` sub-object with element id + caption + kind + LLM confidence for audit.

**V.4e — mcp_gui_server registration** (SHA `b01ea51`, 15 tests): adds 12 `_TOOL_DESCRIPTIONS` entries written for Gemini's tool-picker (says what each tool DOES + scope hints like "HiDPI-aware", "all keys allowlisted", "atomic sequence"). Bumps `_EXPECTED_TOOL_COUNT` from 12 to 24 as a module-level constant so smoke-gate self-test + unit tests + gen-oc-config all read from one source. New guard test `test_every_tool_has_a_description` fails fast if a future schema addition forgets a description.

Where in code: `dual-brain/gui_agent/protocol.py`, `dual-brain/gui_agent/agent.py`, `dual-brain/controller/mcp_gui_server.py`. Tests split into `test_protocol_v.py` (59), `test_agent_v_raw.py` (18), `test_agent_v_parse.py` (19), `test_agent_v_grounded.py` (19), plus 2 new mcp_gui_server tests + 3 updated — **117 new/updated tests for V.4 alone**. Full suite: 371 passed, 1 skipped.

### V.5 — annotated_screenshot HITL presenter (shipped 2026-08-02, split into 3 sub-commits)

Refined vs the original one-liner after research probes (codebase Explore + GTK4/LibAdwaita docs). Key change: **subclass the existing `LibAdwaitaHitlPresenter`, not the older `GtkPresenter`** — inherits ~250 LOC of visual `Gtk.ProgressBar` lockout + tier-colored `Adw.HeaderBar` badge + COW diff pane + full keymap for free.

**V.5a — `HitlDisplayData.preview_image_path` field + sanitizer** (SHA `818d0fe`, 23 tests): adds `preview_image_path: Optional[str] = None` to the frozen dataclass. `_sanitize_preview_path()` enforces four rules — must be str, absolute path with no `..`, resolves under `/tmp/icebreaker-gui/`, ends in `.png`. Invalid input → silent drop to `None` so the HITL prompt still fires with fallback text. First defense layer against a compromised agent setting `preview_image_path="/root/.ssh/id_rsa.png"`.

**V.5b — `annotated_screenshot` presenter subclass** (SHA `7a68133`, 13 tests): tiny 3-line hook added to parent `_run_dialog` (`_add_extra_content(vbox, data)` — no-op by default). Subclass overrides that hook to inject `Gtk.Picture.new_for_filename(path)` with `content-fit=CONTAIN` + 400px height cap + `Gtk.ScrolledWindow` wrap + `Adw.PreferencesGroup` "Preview" card + Orca-friendly a11y label. All 39 pre-existing dialog tests still pass — hook addition is regression-safe. Registration via `@register_presenter("annotated_screenshot")` — additive, doesn't touch existing `libadwaita`. Wired through `controller/presenters/__init__.py` with try/except so headless daemons don't crash. `controller.toml.example` documents the new option alongside `terminal | screen_reader | gtk | libadwaita`.

**V.5c — Rollup doc update** (this commit): ledger updated with V.5a/b SHAs + test counts + the "caller wiring deferred to V.6" caveat.

**Caller wiring is DEFERRED to V.6.** V.5 ships the presenter + data field only. The caller-wiring path — how `preview_image_path` threads from `agent_graph_nodes.py::mcpd_dispatcher_node` into `HitlDisplayData` at HITL-fire time — is a real architectural change worth its own bisect surface. The presenter is functional today; it just needs a caller to populate the field.

Where in code: `dual-brain/controller/hitl.py` (data field + sanitizer), `dual-brain/gui/hitl/dialog.py` (3-line hook), `dual-brain/gui/hitl/annotated_dialog.py` (NEW ~150 LOC subclass), `dual-brain/controller/presenters/__init__.py` (registration import), `dual-brain/controller/controller.toml.example` (documented option). Tests: `controller/tests/test_hitl_display_data.py` (23), `gui/tests/test_hitl_annotated_dialog.py` (13) — **36 new tests for V.5**, plus 39 pre-existing dialog + 11 pre-existing hitl tests still green.

Anti-patterns from GTK4 research explicitly avoided: `Gtk.MessageDialog` (deprecated), `Gtk.Image` for photos (icon-only, would produce a thumb), `time.sleep()` for lockout (freezes UI — parent uses `GLib.timeout_add`, inherited), `gtk4-layer-shell` for "always on top" (Mutter ignores it — modal + transient-for is the only real answer, parent handles), `present()` from non-GTK thread (parent handles thread spawn + `threading.Event` blocking, inherited).

### V.6 — Build wiring: sandbox + packages + smoke-gate + docs + GT rows

- **What ships:** everything that makes v6.15 install correctly on a fresh ISO. `gui_agent/sandbox.py` gets seccomp allowlist entries for `execve` of `/usr/bin/xdotool` + `/usr/bin/notify-send` + `/usr/bin/xrandr`, plus Landlock rules for the trust-store path and preview dir. `packages-desktop.txt` adds `xdotool` (~200 KB). `smoke-gate.sh` L3 grows 5 new assertions: tool_count=24, xdotool present, trust defaults present, `ib-trust list` runs, `python -m controller.gui_agent.vision --self-test` returns "vision OK". `GROUND_TRUTH.md` gets F-103..F-106 rows (per BP-13 no-repeat-regressions discipline).
- **How it's built:** each of these is a small, isolated wiring change — 5-30 lines each, no logic. The `packages-desktop.txt` addition is a one-time base-cache rebuild (~60 min) on the GCP build VM.
- **Config keys:** `[gui.vision]`, `[gui.trust]`, `[gui.preview]`, `[gui.geometry]` sections added to `controller.toml` schema in `controller/config.py`, each with `enabled` toggles for surgical rollback.
- **Where in code:** `dual-brain/gui_agent/sandbox.py`, `incremental/build/packages-desktop.txt`, `incremental/build/smoke-gate.sh`, `dual-brain/controller/config.py`, `incremental/GROUND_TRUTH.md`.
- **Tests:** the smoke-gate assertions ARE the tests — they run on every ISO build and fail-close if the wiring drifts. Plus regression guards for sandbox denials.

### V.7 — Live tests (5 cases) + Xvfb fixtures

- **What ships:** confidence that v6.15 actually works against real Gemini + real X apps, not just mocks. `incremental/tests/test_vision_live.py` gated by `RUN_LIVE_TESTS=1` — burns real API credits, so it runs pre-ISO-cut, not on every push.
- **How it's built:** (1) bundled `tests/fixtures/screenshots/login_form.png` — assert VLM returns ≥3 elements with expected captions; (2) bundled busy-Firefox PNG — assert VLM identifies the URL bar; (3) Xvfb + gnome-calculator — `gui.grounded_click "the 5 button"`, assert calculator display shows "5"; (4) Xvfb + gnome-text-editor — `gui.grounded_type "hello world"`, assert content present; (5) Xvfb + nautilus split-pane — `gui.grounded_drag` file across panes, assert file moved.
- **Why Xvfb:** the tests need to actually spawn apps and inspect their post-action state without requiring the operator to sacrifice their real desktop. Xvfb virtualises the display; the test harness spawns app, fires the tool call, screenshots the result, assert.
- **Where in code:** `incremental/tests/test_vision_live.py`, `tests/fixtures/screenshots/*.png`.
- **Tests:** the file IS the tests. All 5 marked `pytest.mark.live` — CI skips unless `RUN_LIVE_TESTS=1`. Total per-run cost: ~$0.01.

### V.8 — Hot-patch v6.14 guest + rebuild v6.15 + UTM verify

- **What ships:** v6.15 in your hands. Two ISOs: `v6.15-arm64.iso` (current edition, ~4.0 GB), `v6.15_OC-arm64.iso` (OC edition, ~4.3 GB). Same GCP build pipeline that produced v6.14. arm64-first; amd64 follows only if requested.
- **How it's built:** V.1..V.7 code is `rsync`ed onto the running v6.14 OC guest (still booted from the 2026-07-31 ISO), the daemon is restarted, and the 12-scenario UTM checklist runs against the hot-patched guest first. Only when every scenario passes does the ISO rebuild kick off — this is the "prove it works before spending an hour baking a bad image" discipline that BP-13 was born from.
- **Rebuild path:** GCP `icebreaker-build-vm` gets a `systemd-run` transient service `v615-arm64-rebuild.service` with `V67_INCLUDE_ARM64=1 V67_INCLUDE_AMD64=0 LABEL=v6.15 V67_EDITION=both`. Monitor watches for smoke-gate results + xorriso completion. rsync to Mac. SHA verify. UTM boot on Apple Silicon.
- **Where in code:** no new code — this commit is scripts + build orchestration + verification. The `cx-distro/rebuild/rebuild-v67.sh` pipeline handles the actual bake.
- **Tests:** the 12 UTM scenarios from the plan (menu-click boot; screenshot + describe; open Firefox + grounded_click search box; type + press_key; scroll 5x; right-click; open calculator + key_sequence 47×82; drag file; screenshot-loop cost meter; `ib-trust add/list/revoke` roundtrip). Audit-chain check: `/var/log/mcpd/audit.log` + `/var/log/icebreaker/controller-audit.log` show N parse rows + N action rows with screenshot SHAs and VLM cost. Total sweep cost target <$0.05.

---

## Progress ledger (updated in-place as commits land)

| Commit | Status | SHA | Tests |
|---|---|---|---|
| **V.1** VisionGrounder | ✅ shipped 2026-07-31 | `f90ed5c` | 15 green |
| **V.2a** MonitorLayout | ✅ shipped 2026-08-01 | `16792a6` | 10 green |
| **V.2b** input_synth | ✅ shipped 2026-08-01 | `8e222f8` | 37 green |
| **V.2c** rollup doc | ✅ shipped 2026-08-01 | *(this commit)* | n/a |
| **V.3a** annotate | ✅ shipped 2026-08-01 | `f9f9118` | 20 green |
| **V.3b** trust_store | ✅ shipped 2026-08-01 | `97a694e` | 33 green |
| **V.3c** ib-trust CLI + defaults | ✅ shipped 2026-08-01 | `38478c1` | 29 green |
| **V.4a** protocol schemas | ✅ shipped 2026-08-01 | `a15b6c2` | 59 green |
| **V.4b** agent raw pixel/keyboard handlers | ✅ shipped 2026-08-01 | `4fa58df` | 18 green |
| **V.4c** agent parse_screen handler + notify-send | ✅ shipped 2026-08-01 | `344ccc1` | 19 green |
| **V.4d** agent grounded_* orchestrators | ✅ shipped 2026-08-01 | `a80d24f` | 19 green |
| **V.4e** mcp_gui_server registration + count bump | ✅ shipped 2026-08-01 | `b01ea51` | 15 green |
| **V.5a** HitlDisplayData.preview_image_path field + sanitizer | ✅ shipped 2026-08-02 | `818d0fe` | 23 green |
| **V.5b** annotated_screenshot presenter subclass | ✅ shipped 2026-08-02 | `7a68133` | 13 green (+ 39 parent tests still green) |
| **V.5c** rollup doc update | ✅ shipped 2026-08-02 | *(this commit)* | n/a |
| **V.6** sandbox + packages + smoke-gate + docs | pending | — | — |
| **V.7** live tests + Xvfb fixtures | pending | — | — |
| **V.8** hot-patch + rebuild + UTM verify | pending | — | — |

---

## Related documents

- Full technical plan: `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
- Baseline architecture: `AI_Native_OS_Whitepaper.md`
- v6.14 privacy note: `docs/OC_edition_privacy.md`
- Ground-truth failure log (per BP-13): `incremental/GROUND_TRUTH.md` (F-103..F-106 rows in V.6)
