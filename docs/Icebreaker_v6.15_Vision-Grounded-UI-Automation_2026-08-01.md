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

### V.3 — `annotate` + `trust_store` + `ib-trust` CLI

- **What ships:** the "preview before you approve" UX and the "trust this app for the next hour" affordance. `annotate.render_annotated(image, elements, target_id)` draws numbered bounding boxes + optional red arrow onto the screenshot; `parse_screen` fires `notify-send --icon=<png>` so the user sees the annotated view on their desktop *before* any `grounded_click` "ask" prompt lands. `TrustStore` persists per-app per-tool grants so users approve once, not every click.
- **How it's built:** `annotate.py` uses Pillow (already in the RPA extra). Writes 0o600 PNGs to `/tmp/icebreaker-gui/preview-<sha>.png` with 50-file retention (reuses `ScreenshotManager`'s pattern). `trust_store.py` is a JSONL file at `/var/lib/icebreaker/gui_trust.jsonl`, `O_APPEND`-written (audit-discipline), with defaults loaded from `/etc/icebreaker/gui_trust.d/defaults.jsonl` — hover/scroll/parse pre-trusted 24h; click/type per-app 5-min grant on first approval; drag always asks; terminal never trusted.
- **User controls:** `/usr/local/bin/ib-trust list | add <app> <tool> [--ttl N] | revoke <app> <tool> | defaults`. The paranoid can revoke; the pragmatic can `--ttl 86400` and move on.
- **Where in code:** `dual-brain/gui_agent/annotate.py`, `dual-brain/gui_agent/trust_store.py`, `dual-brain/scripts/ib_trust.py`, `cx-distro/distro/gui_trust_defaults.jsonl`.
- **Tests:** 8+ unit tests — annotation renders correctly, notify-send fires with correct icon path, trust default allows, user-grant persists across process restarts, TTL expiry, revoke works, `ib-trust list` output shape.

### V.4 — Protocol schemas + agent dispatch + mcp_gui_server registration

- **What ships:** the *seams*. The 12 new tools become visible to the model. `gui_agent/protocol.py` gets 12 new `_PARAM_SCHEMAS` entries covering parse/click/type/drag/scroll/hover/press_key/key_sequence + 4 `grounded_*` convenience wrappers. `gui_agent/agent.py::handle_request` grows 12 new `_handle_*` branches. `controller/mcp_gui_server.py` bumps its self-test count from 12 to 24 and adds 12 tool descriptions.
- **How it's built:** schemas auto-flow through the existing plumbing — `mcp_gui_server.py::_build_tools_list()` reads `_PARAM_SCHEMAS` directly, and `incremental/build/gen-oc-config.sh` harvests them at ISO build time to synthesize the opencode `permission.mcp` map. Adding new tools requires *no* changes to those files — schema-driven wins.
- **Grounded orchestration:** `_handle_grounded_click(prompt)` → screenshot → `VisionGrounder.parse_screen` (cache-aware) → prompt Gemini "which element ID matches this prompt?" (returns `{element_id: int}`) → `input_synth.click` at that element's centroid (HiDPI-translated) → post-check via `rpa_bridge.image_match` NCC delta on the target region → emit `gui.no_effect` notification if the screen didn't change.
- **Where in code:** `dual-brain/gui_agent/protocol.py`, `dual-brain/gui_agent/agent.py`, `dual-brain/controller/mcp_gui_server.py`.
- **Tests:** unit tests per handler + a `test_agent_integration.py` extension verifying `handle_request("gui.parse_screen")` end-to-end with mocked VLM/xdotool.

### V.5 — `annotated_screenshot` HITL presenter (current edition GTK)

- **What ships:** the polished half of the preview UX. Current-edition users get a GTK4 modal with the annotated PNG rendered inline, "Approve" / "Deny" buttons with 3s Fitts-compliant lockout (BP-4), keyboard shortcuts. OC edition already got `notify-send` from V.3; current edition uses the presenter registry that already exists.
- **How it's built:** new `dual-brain/controller/presenters/annotated_screenshot.py` implements the existing `HitlPresenter` ABC (`hitl.py:165`), decorated `@register_presenter("annotated_screenshot")`. Extends `HitlDisplayData` with an optional `preview_image_path` field. Reuses `presenters/gtk.py::GtkPresenter`'s Approve/Deny scaffolding — inherits the lockout timer, keyboard shortcuts, and screen-reader hooks for free.
- **Selection:** users pick this presenter via `[hitl] presenter = "annotated_screenshot"` in `controller.toml`. Old presenters (terminal, gtk, screen_reader) still work — this is an *addition*, not a replacement.
- **Where in code:** `dual-brain/controller/presenters/annotated_screenshot.py`, extension of `hitl.py::HitlDisplayData`.
- **Tests:** 2+ unit tests — presenter renders with preview when field is set, degrades gracefully to text-only when absent.

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
| **V.3** annotate + trust_store + ib-trust | pending | — | — |
| **V.4** protocol + agent dispatch + mcp_gui_server | pending | — | — |
| **V.5** annotated_screenshot presenter | pending | — | — |
| **V.6** sandbox + packages + smoke-gate + docs | pending | — | — |
| **V.7** live tests + Xvfb fixtures | pending | — | — |
| **V.8** hot-patch + rebuild + UTM verify | pending | — | — |

---

## Related documents

- Full technical plan: `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
- Baseline architecture: `AI_Native_OS_Whitepaper.md`
- v6.14 privacy note: `docs/OC_edition_privacy.md`
- Ground-truth failure log (per BP-13): `incremental/GROUND_TRUTH.md` (F-103..F-106 rows in V.6)
