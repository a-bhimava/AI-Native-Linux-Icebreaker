# Icebreaker Vision-Grounded UI Automation — Privacy + Trust Doctrine

**Product:** Icebreaker v6.15
**Ships:** Fix V — 12 new `iceui` MCP tools, cloud VLM (Gemini 2.5 Flash), trust store, annotated preview UX
**Baseline it supersedes:** v6.14 (AT-SPI-tree only — worked for gnome-calculator, broke on Slack/Firefox/games)
**Date:** 2026-08-02

---

## What this feature does (in one sentence)

The AI Terminal can now see any pixel of any window and drive click / type / drag / scroll / keyboard actions on it — natural-language input, screenshot preview before every write, per-app trust so users approve once not every click.

## Why we shipped it

v6.14 promised "natural language → any GUI action" but delivered "natural language → AT-SPI-visible action". Every Electron app (Slack, Discord, VS Code), every browser web page, LibreOffice canvas, every game, and every cross-arch emulated app under UTM returned `element_not_found`. Users saw the AI refuse to click things it could clearly see, and gave up. v6.15 closes that gap.

---

## Privacy: what leaves your machine

**Cloud VLM calls are on the write path.** Every `gui.parse_screen` and `grounded_*` tool call sends the screenshot as a base64-encoded PNG to the configured VLM backend (default: Google Gemini 2.5 Flash via LiteLLM). That means:

- **The screenshot bytes leave your machine** on every VLM call.
- Google's data-use policy for Gemini API applies — verify at
  <https://ai.google.dev/gemini-api/terms> whether your account is on
  a plan that trains models on your prompts.
- The VLM's response (an element list of `{id, box, caption, kind}`)
  comes back and is used to pick the target element.
- The AI's decision to CLICK is made by the QB (also cloud in OC
  edition — Gemini/Claude/OpenAI), so the picked element ID + user
  intent also flow to the QB backend.

**What stays local:**
- Screenshot files themselves (0o600 in `/tmp/icebreaker-gui/`, 50-file retention, then deleted).
- The trust store (`/var/lib/icebreaker/gui_trust.jsonl` + `/etc/icebreaker/gui_trust.d/`).
- Annotated preview PNGs (`/tmp/icebreaker-gui/preview-<sha>.png`, same 0o600 + retention).
- Audit log entries in `/var/log/icebreaker/controller-audit.log` (SHA-256 hash of the screenshot, not pixels).

**To disable cloud vision entirely:**

```toml
# ~/.config/icebreaker/controller.toml
[gui.vision]
enabled = false
```

With `enabled = false`, every `gui.parse_screen` / `grounded_*` tool call returns `{"error": "vision disabled", "reason": "vision_disabled"}`. The AI falls back to the AT-SPI-only surface (v6.14 baseline behavior).

**Local-model alternative** — not shipped in v6.15. Deferred to v7 pending a consumer-GPU story for Apple Silicon UTM guests. Candidates surveyed for v7: OmniParser v2 (Microsoft, YOLOv9 + Florence-2), ShowUI-2B, UI-TARS. All work but none run acceptably fast on CPU-only guests today.

## Cost governance

Every VLM call bumps a per-turn cost counter. A call that would push the running sum past `[gui.vision] cost_ceiling_usd_per_turn` (default $0.01) is **denied before touching the network** and audited with reason `vision_cost_ceiling_exceeded`. This is BP-10 — prevents an injected loop from becoming a runaway $ bill.

Real cost per call at Gemini 2.5 Flash rates (verify at deploy time): ~$0.0001 for a 1MP screenshot. A typical grounded_click costs ~$0.0002 (one parse + one text-pick). $0.01 ceiling = ~50 grounded actions per user turn.

Cost is surfaced in every audit row + tool result:

```json
{
  "elements": [...],
  "vlm_cost_usd": 0.00013,
  "cost_this_turn_usd": 0.00098,
  "backend_used": "gemini/gemini-2.5-flash"
}
```

## Trust doctrine

opencode's "ask on every write action" is safe by construction but painful as UX — a 5-step workflow fires 5 modals. The v6.15 trust store makes this tolerable without dropping safety.

**Four tiers of grant** (iOS 14+ tri-state, plus deny):

| Tier | Behavior | Use case |
|---|---|---|
| `once` | Approves exactly one check | "Yes, do this one action, then ask again" |
| `session` | Valid until the session_id changes (logout/reboot) | "Trust Slack while I'm working, forget on next login" |
| `persistent` | TTL-bounded, survives restarts (default TTL = 1h) | "Trust the calculator for 24h" |
| `deny_always` | Hard block; overrides any grant | Terminals, sudo, keyring |

**Wildcards** are deliberately narrow — only `*` and `prefix*`. No general globs. This keeps a `gnome-*` grant from being crafted to match `gnome-terminal` (which is separately hard-denied).

**Never-silent expiry.** A matching-but-expired persistent grant returns denied with an explicit reason: `"trust expired at 2026-08-02 14:23:11 — ask again"`. Users see WHY the ask prompt returned instead of assuming the system is buggy.

**Defense in depth.** The code has a hard-coded `_HARD_DENY` frozenset for gnome-terminal / xterm / konsole / terminator / tilix / kitty / alacritty / sudo / gnome-keyring / seahorse / polkit-\*. Even if the shipped defaults JSONL is deleted or corrupted, those apps still can't be auto-clicked.

**Shipped defaults** (see `cx-distro/distro/gui_trust_defaults.jsonl`):

- 24h auto-approve for all read-only tools everywhere (hover, screenshot, parse_screen, get_window_list, get_element_tree, find_element, ping, scroll)
- gnome-calculator pre-approved for grounded_click, click_at_coords, press_key (contained side-effects)
- 7 terminal emulators + sudo + gnome-keyring + seahorse + polkit-\* hard-denied (belt-and-braces with the code-level `_HARD_DENY`)

**CLI** — `/usr/local/bin/ib-trust`:

```
$ ib-trust                                 # no-args: help + current active grants
$ ib-trust list [--all] [--json]
$ ib-trust add slack click --ttl 3600      # fuzzy match: 'click' → gui.grounded_click
$ ib-trust revoke slack click --dry-run
$ ib-trust why slack gui.grounded_click    # exit 0 if allowed, 1 if denied — scripts
$ ib-trust undo                            # revert last add/revoke
$ ib-trust export > backup.jsonl
$ ib-trust import backup.jsonl
```

## Visual preview UX

Before any grounded write, the AI produces an annotated screenshot: kind-colored bounding boxes (iOS palette — button red, input blue, link green, icon gray), numbered ID chips, thick red arrow at the target, 40% dim on non-target regions so the eye goes to the target.

- **OC edition:** the annotated PNG is fired via `notify-send --icon=/tmp/icebreaker-gui/preview-<sha>.png` — a desktop notification appears on your desktop ~500ms BEFORE the opencode "ask" text prompt lands.
- **Current edition:** the `annotated_screenshot` HITL presenter renders the PNG inline in a GTK4 LibAdwaita modal with the standard Approve/Deny buttons + 3s Fitts lockout + full keyboard shortcuts. Enable via `[hitl] presenter = "annotated_screenshot"` in `controller.toml`.

## HiDPI / multi-monitor

Handled automatically via `gui_agent/geometry.py::MonitorLayout`. Parses `xrandr --query` output, infers scale factor (Retina 2x, external monitor 1x etc), translates every VLM-provided physical coord to the logical coord xdotool needs. No per-user setup on Apple Silicon UTM.

If xrandr misreports your setup (some VM guests):

```toml
[gui.geometry]
hidpi_scale_override = 2   # force 2x on every monitor
```

## Security posture summary

- **INV-1 brain isolation**: VLM call is quarantined-brain — screenshot content flows QB→VLM→QB, never touches the Privileged Brain.
- **INV-2 schema validation**: every tool param validated at the JSON Schema boundary. Coords bounded 0..32768. Key combos allowlisted (`ctrl+s` OK, `$(rm -rf /)` rejected structurally).
- **INV-6 dry-run**: the parsed element list IS the dry-run — user sees it via `notify-send` or GTK modal before any grounded write.
- **INV-8 audit**: two audit entries per grounded action (parse + click), both with screenshot SHA-256, VLM cost, LLM pick confidence, trust decision source.
- **BP-6 structured facts**: model captions are DISPLAYED but never DECIDE — decision uses element_id (integer) + coord bounds.
- **BP-10 governance**: cost ceiling denies before the network call. Trust store limits blast radius per-app.
- **F-107 sandbox fix**: pre-existing Landlock bit-value bug (`READ_FILE=1<<0` when kernel says `EXECUTE=1<<0`) fixed. Sandbox now correctly restricts what it claims to restrict.
- **F-103 exec allowlist**: xdotool spawn is Landlock-gated to `/usr/bin` only. Scratch dir + $HOME are non-executable — dropped binaries can't run.
- **F-105 hard-deny**: terminals, sudo, keyring, polkit — never auto-clickable regardless of trust grants.

## Rollback

Any part of Fix V misbehaves:

```toml
# ~/.config/icebreaker/controller.toml
[gui.vision]
enabled = false   # disables all 12 Fix V tools; AT-SPI (v6.14) surface still works
```

Or selectively deny a specific tool:

```bash
# /etc/icebreaker/qb_oc.json — override the auto-generated permission
"iceui_gui.grounded_drag": "deny"   # or "ask" to keep prompting
```

Last-resort: keep the v6.14_OC-arm64.iso (SHA `d2c931b4d83c2efa613c7e355f1e38c55954716bbb82eb7669962df1d1a358bd`) as a fallback boot image.

## Related documents

- Full technical rollup: `docs/Icebreaker_v6.15_Vision-Grounded-UI-Automation_2026-08-01.md`
- Architecture: `AI_Native_OS_Whitepaper.md`
- Failure log with F-103..F-107: `incremental/GROUND_TRUTH.md`
- Plan: `/Users/aditya/.claude/plans/users-aditya-pictures-screenshots-scree-starry-cerf.md`
