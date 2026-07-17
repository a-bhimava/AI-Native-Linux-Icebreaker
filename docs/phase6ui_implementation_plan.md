# Phase 6UI — Desktop GUI: Implementation Plan
> GTK4 + LibAdwaita desktop GUI layer for the Icebreaker AI-Native OS.
> Six components: Settings Panel, Main Chatbot, System Tray, First-Boot Wizard,
> HITL Dialog upgrade, Audit Log Viewer.
>
> **Prerequisites:** Phases 0–6T complete (1749 tests, PRs #1–#30 merged to `main`).
> ISO build pipeline working (manual debootstrap + mksquashfs + xorriso).

---

## §0 — Status snapshot (update on every PR)

| Milestone | Status | PR | Notes |
|---|---|---|---|
| M6UI.0 GUI scaffold + theme | Not started | #31 | `dual-brain/gui/` package, LibAdwaita theme, shared widgets, `.desktop` files |
| M6UI.1 Settings Panel | Not started | #32 | Visual controller.toml editor, backend selector, model manager, keymap editor |
| M6UI.2 Main Chatbot | Not started | #33 | NL chat window, conversation history, inline CoT, daemon IPC |
| M6UI.3 System Tray | Not started | #34 | GNOME status indicator, health polling, quick actions menu |
| M6UI.4 First-Boot Wizard | Not started | #35 | Multi-step onboarding, API key setup, PB verification, test command |
| M6UI.5 HITL Dialog upgrade | Not started | #36 | LibAdwaita dialog, visual lockout timer, COW diff preview, tier coloring |
| M6UI.6 Audit Log Viewer | Not started | #37 | Filterable table, hash-chain indicator, detail view, export |
| M6UI.7 CI gates + integration | Not started | #38 | G23–G28 gates, `.desktop` file validation, screenshot tests, doc updates |

---

## §1 — Invariants we must not break

All eight invariants from `CLAUDE.md` remain in force. The GUI layer is a **client** of
the Controller daemon — it never instantiates QB, PB, or mcpd directly. This is the
critical architectural constraint: GUI components are pure presentation.

- **INV-1 (brain isolation):** GUI displays model output but never routes raw text
  between brains. The daemon handles all brain communication.
- **INV-2 (schema enforcement):** Settings Panel validates config against JSON Schema
  before saving. Config editor never constructs intent objects.
- **INV-3 (mcpd network isolation):** GUI has no relationship to mcpd. All system
  operations flow through the daemon.
- **INV-4 (parameter validation):** Chatbot sends NL text to daemon via JSON-RPC; the
  daemon validates intent schemas. GUI never constructs MCP tool calls.
- **INV-5 (sandbox before fork):** Unchanged — GUI does not fork processes.
- **INV-6 (COW + 3s lockout):** HITL Dialog enforces lockout with monotonic clock
  timer and `GLib.timeout_add()`. Approve button insensitive during lockout.
- **INV-7 (model checksums):** Settings Panel model manager shows checksum status;
  First-Boot Wizard verifies PB model. Neither modifies model files.
- **INV-8 (audit integrity):** Audit Viewer is read-only. It never writes to the
  audit log.

Engineering best practices:
- **BP-1 (plug-and-play):** Each GUI component registers via the existing presenter
  registry or new `gui.registry` module. Config-driven activation.
- **BP-3 (sanitize display):** All model/daemon text sanitized via `sanitize_for_gtk()`
  before GTK label/buffer insertion. Strips ANSI, C0/C1, neutralizes control chars.
- **BP-4 (human-gate integrity):** HITL Dialog flushes pending GTK events before
  reading decision. Default deny on window close or timeout.
- **BP-8 (secret hygiene):** Settings Panel shows env var **names** only, never
  resolves or displays secret values. Config file stores `api_key_env`, not `api_key`.

---

## §2 — Architecture decisions

| ADR | Decision | Rationale |
|---|---|---|
| ADR-20 | **GTK4 + LibAdwaita** | Native GNOME look-and-feel on the Ubuntu ISO. PyGObject already used by GTK presenter stub and GUI Agent. LibAdwaita provides `AdwPreferencesWindow`, `AdwNavigationView`, responsive layouts, dark mode, accessibility — all for free. No Electron/web runtime. |
| ADR-21 | **Daemon-first architecture** | Every GUI component talks to the Controller daemon via `DaemonClient` over AF_UNIX JSON-RPC. The GUI process never instantiates QB, PB, or mcpd. This preserves brain isolation (INV-1) and means the GUI can be killed/restarted without affecting running operations. |
| ADR-22 | **Single GTK application process** | All six components (settings, chatbot, tray, wizard, hitl, audit) live in one `Adw.Application`. Navigation between them uses `Adw.NavigationView` or separate windows. Single process avoids D-Bus registration conflicts and shares the GLib main loop. |
| ADR-23 | **CSS-based theming — oklch design tokens translated to GTK4 CSS** | Authoritative palette defined in oklch (shadcn/Tailwind conventions), converted to hex for GTK4 `CssProvider`. Supports both dark (default) and light modes. Dark primary `#e78952` (amber), secondary `#5e8787` (teal), bg `#111112`. Light primary `#d77843`, secondary `#517575`, bg `#ffffff`. Consistent with the Textual TUI palette. Reference design tokens in `docs/design-tokens.css`. |
| ADR-24 | **New `dual-brain/gui/` package** | Separate from `controller/` (daemon logic) and `terminal/` (TUI). Follows WF-7 (one module per PR). Sub-packages: `settings/`, `chatbot/`, `tray/`, `wizard/`, `hitl/`, `audit/`, plus shared `theme.py` and `widgets.py`. |
| ADR-25 | **Thread-safe daemon communication** | `DaemonClient` reader thread delivers responses via `GLib.idle_add()` to the GTK main thread. All widget mutations happen on the GTK main thread only. Blocking operations (config save, model verify) run in `Gio.Task` threads. |
| ADR-26 | **`.desktop` files for app menu integration** | `icebreaker-chatbot.desktop`, `icebreaker-settings.desktop` installed to `/usr/share/applications/`. Users can launch from GNOME Activities or dock. CLI flags `--chatbot`, `--settings`, `--wizard`, `--audit` also available. |

---

## §3 — Directory structure

```
dual-brain/gui/
├── __init__.py
├── __main__.py              # Entry point: python -m gui [--chatbot|--settings|...]
├── app.py                   # Adw.Application subclass, window management
├── theme.py                 # IcebreakerTheme: CSS provider, color tokens, icon set
├── widgets.py               # Shared widgets: TierBadge, StatusDot, SanitizedLabel
├── daemon_client.py         # GtkDaemonClient: GLib.idle_add wrapper over DaemonClient
├── settings/
│   ├── __init__.py
│   ├── window.py            # AdwPreferencesWindow subclass
│   ├── backend_page.py      # QB backend selector + per-backend config
│   ├── model_page.py        # Model manager: list, verify, checksum status
│   ├── keymap_page.py       # Visual keymap editor
│   ├── session_page.py      # Session preferences (color, history, spinner)
│   └── daemon_page.py       # Daemon + HITL config
├── chatbot/
│   ├── __init__.py
│   ├── window.py            # AdwApplicationWindow subclass
│   ├── message_list.py      # Scrollable conversation history
│   ├── message_row.py       # Single message bubble (user / assistant / system)
│   ├── input_bar.py         # Text entry + send button
│   └── cot_expander.py      # Expandable chain-of-thought card
├── tray/
│   ├── __init__.py
│   └── indicator.py         # AppIndicator3 or GLib status polling
├── wizard/
│   ├── __init__.py
│   ├── window.py            # AdwWindow with AdwNavigationView
│   ├── welcome_page.py      # Step 1: Welcome + architecture overview
│   ├── apikey_page.py       # Step 2: QB API key env var setup
│   ├── model_page.py        # Step 3: PB model verification
│   ├── test_page.py         # Step 4: Run test NL command
│   └── done_page.py         # Step 5: Success + launch chatbot
├── hitl/
│   ├── __init__.py
│   └── dialog.py            # Upgraded AdwWindow HITL dialog (replaces gtk.py stub)
├── audit/
│   ├── __init__.py
│   ├── window.py            # AdwApplicationWindow subclass
│   ├── log_model.py         # Gio.ListModel adapter for audit entries
│   ├── row_widget.py        # ColumnView row: timestamp, intent, tier, outcome
│   └── detail_panel.py      # Expanded detail view for single entry
└── tests/
    ├── test_theme.py
    ├── test_widgets.py
    ├── test_settings.py
    ├── test_chatbot.py
    ├── test_wizard.py
    ├── test_hitl_dialog.py
    └── test_audit_viewer.py
```

---

## §4 — Shared infrastructure (M6UI.0 — PR #31)

### 4.1 Theme (`gui/theme.py`)

The authoritative design tokens are defined in oklch (shadcn/Tailwind conventions) and
stored as a reference in `docs/design-tokens.css`. The GTK4 theme translates them to hex
for `Gtk.CssProvider`. Both light and dark modes are supported; dark is the default.

**Fonts:** Geist Mono (sans/UI), JetBrains Mono (monospace/code). Border radius: 0.75rem.

#### Dark mode (default)

| Token | oklch | Hex | Usage |
|---|---|---|---|
| `background` | `oklch(0.1797 0.0043 308.19)` | `#111112` | Window/app background |
| `foreground` | `oklch(0.8109 0 0)` | `#c0c0c0` | Primary text |
| `card` | `oklch(0.1822 0 0)` | `#111111` | Card/panel background |
| `primary` | `oklch(0.7214 0.1337 49.98)` | `#e78952` | Amber — active, NL mode, ring |
| `primary-foreground` | `oklch(0.1797 0.0043 308.19)` | `#111112` | Text on primary |
| `secondary` | `oklch(0.5940 0.0443 196.02)` | `#5e8787` | Teal — success, shell mode |
| `secondary-foreground` | `oklch(0.1797 0.0043 308.19)` | `#111112` | Text on secondary |
| `muted` | `oklch(0.2520 0 0)` | `#222222` | Borders, dividers, input bg |
| `muted-foreground` | `oklch(0.6268 0 0)` | `#888888` | Secondary text, placeholders |
| `accent` | `oklch(0.3211 0 0)` | `#333333` | Hover state, subtle highlights |
| `accent-foreground` | `oklch(0.8109 0 0)` | `#c0c0c0` | Text on accent |
| `destructive` | `oklch(0.5940 0.0443 196.02)` | `#5e8787` | Destructive actions (dark: teal) |
| `border` | `oklch(0.2520 0 0)` | `#222222` | Borders |
| `input` | `oklch(0.2520 0 0)` | `#222222` | Input field background |
| `ring` | `oklch(0.7214 0.1337 49.98)` | `#e78952` | Focus ring |

#### Light mode

| Token | oklch | Hex | Usage |
|---|---|---|---|
| `background` | `oklch(1.0000 0 0)` | `#ffffff` | Window/app background |
| `foreground` | `oklch(0.2101 0.0318 264.66)` | `#101827` | Primary text |
| `card` | `oklch(1.0000 0 0)` | `#ffffff` | Card/panel background |
| `primary` | `oklch(0.6716 0.1368 48.51)` | `#d77843` | Amber — active, NL mode |
| `primary-foreground` | `oklch(1.0000 0 0)` | `#ffffff` | Text on primary |
| `secondary` | `oklch(0.5360 0.0398 196.03)` | `#517575` | Teal — success |
| `secondary-foreground` | `oklch(1.0000 0 0)` | `#ffffff` | Text on secondary |
| `muted` | `oklch(0.9670 0.0029 264.54)` | `#f3f4f6` | Subtle backgrounds |
| `muted-foreground` | `oklch(0.5510 0.0234 264.36)` | `#6a7180` | Secondary text |
| `accent` | `oklch(0.9491 0 0)` | `#ededed` | Hover state |
| `destructive` | `oklch(0.6368 0.2078 25.33)` | `#ee4444` | Destructive actions (light: red) |
| `border` | `oklch(0.9276 0.0058 264.53)` | `#e5e7ea` | Borders |
| `ring` | `oklch(0.6716 0.1368 48.51)` | `#d77843` | Focus ring |

#### Tier colors (both modes)

| Tier | Color | Hex |
|---|---|---|
| Tier 0 | Teal (secondary) | `#5e8787` / `#517575` |
| Tier 1 | Foreground | `#c0c0c0` / `#101827` |
| Tier 2 | Warning yellow | `#fbbf24` |
| Tier 3 | Error red | `#f87171` / `#ee4444` |

#### GTK4 CSS implementation

```python
COLOR_TOKENS_DARK = {
    "background":          "#111112",
    "foreground":          "#c0c0c0",
    "card":                "#111111",
    "primary":             "#e78952",
    "primary_foreground":  "#111112",
    "secondary":           "#5e8787",
    "secondary_foreground":"#111112",
    "muted":               "#222222",
    "muted_foreground":    "#888888",
    "accent":              "#333333",
    "accent_foreground":   "#c0c0c0",
    "destructive":         "#5e8787",
    "border":              "#222222",
    "input":               "#222222",
    "ring":                "#e78952",
}

COLOR_TOKENS_LIGHT = {
    "background":          "#ffffff",
    "foreground":          "#101827",
    "card":                "#ffffff",
    "primary":             "#d77843",
    "primary_foreground":  "#ffffff",
    "secondary":           "#517575",
    "secondary_foreground":"#ffffff",
    "muted":               "#f3f4f6",
    "muted_foreground":    "#6a7180",
    "accent":              "#ededed",
    "accent_foreground":   "#101827",
    "destructive":         "#ee4444",
    "border":              "#e5e7ea",
    "input":               "#e5e7ea",
    "ring":                "#d77843",
}

FONTS = {
    "sans":  "Geist Mono, ui-monospace, monospace",
    "mono":  "JetBrains Mono, monospace",
}

RADIUS = "12px"  # 0.75rem
```

`IcebreakerTheme.apply(app, dark=True)` loads a `Gtk.CssProvider` with these tokens
mapped to GTK CSS classes (`.ib-bg`, `.ib-primary`, `.tier-0` … `.tier-3`, etc.).
Respects `Adw.StyleManager.get_default().get_dark()` for automatic light/dark switching.
Sidebar tokens are used for the Settings Panel navigation pane.

### 4.2 Shared widgets (`gui/widgets.py`)

| Widget | Base class | Purpose |
|---|---|---|
| `TierBadge` | `Gtk.Label` | Pill-shaped label: "Tier 0" in teal, "Tier 3" in red |
| `StatusDot` | `Gtk.DrawingArea` | 8px circle: green/amber/red with optional pulse animation |
| `SanitizedLabel` | `Gtk.Label` | Sets text via `sanitize_for_gtk()` — strips ANSI/C0/C1 |
| `LockoutButton` | `Adw.Bin` | Button with countdown overlay; insensitive until timer expires |
| `KeyValueRow` | `Gtk.Box` | Horizontal label: value pair for detail views |

### 4.3 GtkDaemonClient (`gui/daemon_client.py`)

Subclasses `controller.client.DaemonClient`. Overrides the reader thread to marshal
all callbacks through `GLib.idle_add()`:

```python
class GtkDaemonClient(DaemonClient):
    def _dispatch_notification(self, method, params):
        GLib.idle_add(self._on_notification, method, params)

    def _dispatch_response(self, id, result):
        GLib.idle_add(self._on_response, id, result)
```

### 4.4 Entry points

**CLI flags** added to `controller/__main__.py`:

```
python -m controller --chatbot     # Launch chatbot window
python -m controller --settings    # Launch settings panel
python -m controller --wizard      # Launch first-boot wizard
python -m controller --audit       # Launch audit log viewer
```

Or directly: `python -m gui --chatbot`

**`.desktop` files** installed to `/usr/share/applications/`:

```ini
[Desktop Entry]
Name=Icebreaker
Comment=AI-Native OS Assistant
Exec=/opt/icebreaker/venv/bin/python3 -m gui --chatbot
Icon=icebreaker
Terminal=false
Type=Application
Categories=System;Utility;
```

### 4.5 Dependencies

Add to `dual-brain/pyproject.toml` under `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
gui = [
    "PyGObject>=3.46",
    "websockets>=12.0",
]
```

System packages (already on the ISO): `python3-gi`, `gir1.2-adw-1`,
`gir1.2-gtk-4.0`, `libadwaita-1-dev`.

### 4.6 Config schema extension

New `[desktop]` section in `controller.toml` (backward-compatible — defaults applied
when absent):

```toml
[desktop]
start_tray        = true       # Launch tray indicator on login
chatbot_on_tray   = true       # Click tray → open chatbot
default_window    = "chatbot"  # Which window opens by default
```

Add `DesktopConfig` dataclass to `controller/config.py` and `"desktop"` property to
`controller/schemas/controller_config.json`.

### 4.7 Files created/modified (M6UI.0)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/__init__.py` |
| CREATE | `dual-brain/gui/__main__.py` |
| CREATE | `dual-brain/gui/app.py` |
| CREATE | `dual-brain/gui/theme.py` |
| CREATE | `dual-brain/gui/widgets.py` |
| CREATE | `dual-brain/gui/daemon_client.py` |
| MOD | `dual-brain/controller/__main__.py` — add `--chatbot`, `--settings`, `--wizard`, `--audit` flags |
| MOD | `dual-brain/controller/config.py` — add `DesktopConfig` |
| MOD | `dual-brain/controller/schemas/controller_config.json` — add `"desktop"` |
| MOD | `dual-brain/pyproject.toml` — add `gui` optional deps |
| CREATE | `dual-brain/gui/tests/test_theme.py` |
| CREATE | `dual-brain/gui/tests/test_widgets.py` |

**Estimated:** ~450 lines production, ~120 lines test. Target: 1769 tests.

---

## §5 — Part 1: Settings Panel (M6UI.1 — PR #32)

### 5.1 Window structure

`AdwPreferencesWindow` with five pages:

| Page | Tab label | Content |
|---|---|---|
| Backend | "AI Backend" | QB backend dropdown, per-backend config rows |
| Models | "Models" | GGUF list from `model_search_dirs`, checksum verify button |
| Keymap | "Keybindings" | Action → key grid, validation feedback |
| Session | "Session" | Color mode, history, spinner, streaming toggles |
| System | "System" | HITL lockout/timeout, daemon socket, PB endpoint |

### 5.2 Backend page (`settings/backend_page.py`)

- `AdwComboRow` for backend selection: Gemini / OpenAI / Anthropic / Local
- On selection change, dynamically show/hide the per-backend `AdwPreferencesGroup`:
  - **Gemini:** model (text entry, default "gemini-2.5-flash"), api_key_env (text entry,
    placeholder "GEMINI_API_KEY"), max_tokens (spin button), timeout_seconds (spin button)
  - **OpenAI:** model, api_key_env, endpoint (for custom base URL), max_tokens, timeout
  - **Anthropic:** model, api_key_env, max_tokens, timeout
  - **Local:** model_id (combo from catalogue), endpoint (UNIX socket path)
- "Test Connection" button: sends a lightweight health-check to the daemon, shows
  success/failure toast (`Adw.Toast`)
- **BP-8 compliance:** `api_key_env` field has placeholder "ENV_VAR_NAME" and tooltip
  "Name of the environment variable holding your API key (value is never stored in config)"

### 5.3 Model page (`settings/model_page.py`)

- Scans `model_search_dirs` from config for `*.gguf` files
- `Gtk.ColumnView` or `Adw.PreferencesGroup` listing:
  - Filename, size (human-readable), checksum status (✓ verified / ✗ mismatch / ? unchecked)
- "Verify All" button: computes SHA-256 for each file against `checksums.sha256`
  - Runs in `Gio.Task` to avoid blocking UI
  - Progress bar during verification
- "Open Models Directory" button: `Gio.AppInfo.launch_default_for_uri()`

### 5.4 Keymap page (`settings/keymap_page.py`)

- Grid: one row per `Action` (APPROVE, DENY, MODIFY, EXPLAIN, TRUST, HELP)
- Each row: action name, current binding(s), "Change" button
- "Change" button opens a key-capture dialog (`Gtk.EventControllerKey`)
- Validation (reuses `keymap.py` logic):
  - No duplicate bindings across actions
  - Esc reserved for DENY (shown as locked)
  - ? reserved for HELP (shown as locked)
  - Only single printable characters
- Error shown as `Adw.Banner` if validation fails

### 5.5 Save flow

1. User edits any field → "Apply" button becomes sensitive (unsaved indicator)
2. On "Apply":
   a. Build TOML dict from all page states
   b. Validate against `controller_config.json` schema
   c. If valid: write to `~/.config/icebreaker/controller.toml`, show success toast
   d. If invalid: show error banner with specific validation message
   e. Optionally: "Restart Daemon" button to apply runtime changes
3. On window close with unsaved changes: `Adw.AlertDialog` confirmation

### 5.6 Files created/modified (M6UI.1)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/settings/__init__.py` |
| CREATE | `dual-brain/gui/settings/window.py` |
| CREATE | `dual-brain/gui/settings/backend_page.py` |
| CREATE | `dual-brain/gui/settings/model_page.py` |
| CREATE | `dual-brain/gui/settings/keymap_page.py` |
| CREATE | `dual-brain/gui/settings/session_page.py` |
| CREATE | `dual-brain/gui/settings/daemon_page.py` |
| CREATE | `dual-brain/gui/tests/test_settings.py` |

**Estimated:** ~900 lines production, ~150 lines test. Target: ~1790 tests.

---

## §6 — Part 2: Main Chatbot (M6UI.2 — PR #33)

### 6.1 Window layout

```
┌──────────────────────────────────────────────┐
│  Icebreaker                    [≡] [⚙] [─]  │  ← AdwHeaderBar
├──────────────────────────────────────────────┤
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │ Welcome to Icebreaker.               │    │  ← system message
│  │ Type a natural-language command.      │    │
│  └──────────────────────────────────────┘    │
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │ 🧑 show me disk usage               │    │  ← user message
│  └──────────────────────────────────────┘    │
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │ ▸ Chain of Thought (3 steps)         │    │  ← expandable CoT
│  │                                      │    │
│  │ ✓ Tier 0 · local · 142ms · $0.00    │    │  ← status line
│  │                                      │    │
│  │ Disk usage for /:                    │    │  ← assistant response
│  │ 45% used (22G / 49G)                │    │
│  │ Largest: /var/lib (8.2G)            │    │
│  └──────────────────────────────────────┘    │
│                                              │
├──────────────────────────────────────────────┤
│  [Type a command...]              [Send ↵]   │  ← input bar
└──────────────────────────────────────────────┘
```

### 6.2 Key components

**MessageList** (`chatbot/message_list.py`):
- `Gtk.ScrolledWindow` containing a `Gtk.ListBox`
- Auto-scrolls to bottom on new messages
- Three message types: user (right-aligned, primary color bg), assistant
  (left-aligned, card bg), system (centered, muted)

**MessageRow** (`chatbot/message_row.py`):
- `Gtk.Box` with avatar icon, `SanitizedLabel` for text
- Assistant messages include:
  - `CotExpander`: collapsible `Adw.ExpanderRow` showing CoT steps with state icons
  - `TierBadge`: inline tier indicator
  - Status line: backend, latency, cost
- Monospace font for code blocks (detect ``` fences)

**InputBar** (`chatbot/input_bar.py`):
- `Gtk.Box` with `Gtk.TextView` (multi-line, auto-grow up to 4 lines) + Send button
- Enter sends (Shift+Enter for newline)
- Send button disabled while awaiting response (shows spinner)
- Ctrl+L clears conversation history
- Esc cancels in-flight request

### 6.3 Daemon communication

Uses `GtkDaemonClient` from §4.3:

1. User types message → `client.submit("nl.turn", {"text": message})`
2. Daemon streams `turn.cot` notifications → `CotExpander` updates live
3. If HITL required → daemon sends `hitl.prompt` → HITL Dialog opens (§9)
4. Daemon sends `turn.result` → `MessageRow` rendered with response
5. On error → system message with error detail

Conversation history is local to the GUI (ephemeral by default, configurable).
The daemon maintains its own session state.

### 6.4 Header bar actions

- Hamburger menu (≡): New conversation, Export chat, About
- Settings gear (⚙): Opens Settings Panel
- Window controls: minimize, close

### 6.5 Files created/modified (M6UI.2)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/chatbot/__init__.py` |
| CREATE | `dual-brain/gui/chatbot/window.py` |
| CREATE | `dual-brain/gui/chatbot/message_list.py` |
| CREATE | `dual-brain/gui/chatbot/message_row.py` |
| CREATE | `dual-brain/gui/chatbot/input_bar.py` |
| CREATE | `dual-brain/gui/chatbot/cot_expander.py` |
| CREATE | `dual-brain/gui/tests/test_chatbot.py` |

**Estimated:** ~800 lines production, ~130 lines test. Target: ~1810 tests.

---

## §7 — Part 3: System Tray (M6UI.3 — PR #34)

### 7.1 Implementation

Uses `GLib.timeout_add_seconds()` to poll daemon health every 10 seconds via
`GtkDaemonClient`. Status mapped to indicator:

| Daemon state | Dot color | Tooltip |
|---|---|---|
| Running, PB healthy | Green | "Icebreaker: Ready" |
| Running, PB loading | Amber | "Icebreaker: PB warming up" |
| Not running / unreachable | Red | "Icebreaker: Daemon offline" |

### 7.2 Tray menu (right-click)

```
Icebreaker
──────────────
● Ready (PB: run7_cot_q4km)
──────────────
Open Chatbot
Open Settings
View Audit Log
──────────────
Restart Daemon
──────────────
Quit
```

### 7.3 Platform approach

**Primary:** `Gio.Notification` + GNOME Shell status via `Adw.Application` built-in
notification support. Tray icon via `Gtk.StatusIcon` (deprecated in GTK4 but still
works) or `AppIndicator3` if available.

**Fallback:** If no tray support (Wayland-only GNOME without AppIndicator), the
chatbot window itself shows the status dot in its header bar. The tray indicator
is best-effort, not required.

### 7.4 Auto-start

`.desktop` file with `X-GNOME-Autostart-enabled=true` installed to
`/etc/xdg/autostart/icebreaker-tray.desktop`. Only starts the tray indicator;
chatbot opens on click.

### 7.5 Files created/modified (M6UI.3)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/tray/__init__.py` |
| CREATE | `dual-brain/gui/tray/indicator.py` |
| CREATE | `cx-distro/distro/icebreaker-tray.desktop` |

**Estimated:** ~200 lines production, ~40 lines test. Target: ~1830 tests.

---

## §8 — Part 4: First-Boot Wizard (M6UI.4 — PR #35)

### 8.1 Flow

Five pages in an `Adw.NavigationView`:

```
Welcome → API Key → Model Check → Test Command → Done
```

| Page | Content | Validation |
|---|---|---|
| Welcome | Logo, 2-sentence explanation of dual-brain architecture, "Get Started" button | None |
| API Key | "Which QB backend?" dropdown (Gemini/OpenAI/Anthropic), env var name field, "Already set in shell" checkbox, "Test Connection" button | Test button sends health check to daemon; green check or red error |
| Model Check | PB model file name, size, SHA-256 verification with progress bar, llama-server status | Verification must pass (INV-7) |
| Test Command | Pre-filled NL command ("What processes are using the most CPU?"), "Run" button, shows response inline | Command must complete successfully |
| Done | "Setup complete!" with options: "Open Chatbot" / "Open Settings" / "Close" | None |

### 8.2 Config writes

On completion, writes `~/.config/icebreaker/controller.toml` with the user's QB
backend selection and env var name. Marks first-boot complete via a sentinel file
at `~/.local/share/icebreaker/.first-boot-done`.

### 8.3 Integration with existing `first-boot` script

The existing `cx-distro/distro/first-boot` script (CLI-based) runs model verification
at boot. The wizard replaces the **interactive** portion (API key setup, test command).
Model verification is shared — wizard calls the same verification function.

### 8.4 Files created/modified (M6UI.4)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/wizard/__init__.py` |
| CREATE | `dual-brain/gui/wizard/window.py` |
| CREATE | `dual-brain/gui/wizard/welcome_page.py` |
| CREATE | `dual-brain/gui/wizard/apikey_page.py` |
| CREATE | `dual-brain/gui/wizard/model_page.py` |
| CREATE | `dual-brain/gui/wizard/test_page.py` |
| CREATE | `dual-brain/gui/wizard/done_page.py` |
| CREATE | `dual-brain/gui/tests/test_wizard.py` |

**Estimated:** ~550 lines production, ~100 lines test. Target: ~1850 tests.

---

## §9 — Part 5: HITL Dialog Upgrade (M6UI.5 — PR #36)

### 9.1 Current state

`dual-brain/controller/presenters/gtk.py` has a working `GtkPresenter` registered as
`"gtk"`. It creates a basic `Gtk.ApplicationWindow` with label rows and buttons. It
works but lacks:
- LibAdwaita styling
- Visual lockout countdown
- COW diff with syntax highlighting
- Tier coloring
- Keyboard shortcut hints on buttons

### 9.2 Upgraded dialog layout

```
┌──────────────────────────────────────────────┐
│  ⚠ Action Approval Required      Tier 2     │  ← tier-colored header
├──────────────────────────────────────────────┤
│                                              │
│  Action:      fs.write                       │
│  Target:      /etc/hosts                     │
│  Risk:        medium                         │
│  Reversible:  Yes (COW snapshot)             │
│  Backend:     local (run7_cot_q4km)          │
│                                              │
│  ┌─ Dry-run preview ──────────────────────┐  │
│  │ - 127.0.0.1  localhost                 │  │
│  │ + 127.0.0.1  localhost myhost          │  │
│  └────────────────────────────────────────┘  │
│                                              │
│        ━━━━━━━━━━━━  2s remaining            │  ← lockout progress bar
│                                              │
│  [Approve (a)] [Deny (d)] [Modify (m)] [?]   │  ← buttons with key hints
│                                              │
└──────────────────────────────────────────────┘
```

### 9.3 Lockout implementation (INV-6)

```python
def _start_lockout(self, seconds: int):
    self._lockout_start = time.monotonic()
    self._lockout_total = seconds
    self._approve_btn.set_sensitive(False)
    self._progress_bar.set_fraction(0.0)
    GLib.timeout_add(100, self._lockout_tick)  # 10 Hz update

def _lockout_tick(self):
    elapsed = time.monotonic() - self._lockout_start
    fraction = min(elapsed / self._lockout_total, 1.0)
    self._progress_bar.set_fraction(fraction)
    remaining = max(0, self._lockout_total - elapsed)
    self._countdown_label.set_text(f"{remaining:.0f}s remaining")
    if elapsed >= self._lockout_total:
        self._approve_btn.set_sensitive(True)
        return GLib.SOURCE_REMOVE
    return GLib.SOURCE_CONTINUE
```

### 9.4 COW diff rendering

If `HitlDisplayData.cow_summary` is present, render in a `Gtk.TextView` with
`Gtk.TextTag`s for diff coloring:
- Lines starting with `+` → green text
- Lines starting with `-` → red text
- Context lines → muted text
- Monospace font (`JetBrains Mono` or `monospace` fallback)

### 9.5 Migration from existing stub

The new `gui/hitl/dialog.py` replaces the logic in `controller/presenters/gtk.py`.
The old file is updated to import from the new location:

```python
# controller/presenters/gtk.py — thin wrapper for backward compatibility
from ...gui.hitl.dialog import LibAdwaitaHitlPresenter as GtkPresenter
```

### 9.6 Files created/modified (M6UI.5)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/hitl/__init__.py` |
| CREATE | `dual-brain/gui/hitl/dialog.py` |
| MOD | `dual-brain/controller/presenters/gtk.py` — redirect to new impl |
| CREATE | `dual-brain/gui/tests/test_hitl_dialog.py` |

**Estimated:** ~400 lines production, ~80 lines test. Target: ~1870 tests.

---

## §10 — Part 6: Audit Log Viewer (M6UI.6 — PR #37)

### 10.1 Window layout

```
┌──────────────────────────────────────────────────────────────────┐
│  Audit Log                                    [Export ▾] [─]     │
├──────────────────────────────────────────────────────────────────┤
│  [Filter: All ▾]  [Search...                        ]  [🔍]     │
├──────────────────────────────────────────────────────────────────┤
│  Timestamp          │ Intent        │ Tier │ Outcome  │ Hash OK  │
│─────────────────────┼───────────────┼──────┼──────────┼──────────│
│  06-21 19:42:03     │ fs.read       │  0   │ approved │    ✓     │
│  06-21 19:42:18     │ fs.write      │  2   │ approved │    ✓     │
│  06-21 19:43:01     │ process.kill  │  3   │ denied   │    ✓     │
│  06-21 19:43:15     │ pkg.install   │  1   │ approved │    ✓     │
│  ...                                                             │
├──────────────────────────────────────────────────────────────────┤
│  ▸ Detail: fs.write /etc/hosts                                   │
│    Risk: medium  │  User: icebreaker  │  Duration: 142ms         │
│    Intent ID: a3f7...  │  Session: 8b2c...                       │
│    COW snapshot: /tmp/icebreaker-cow/snap-20260621-194218.tar    │
│    Hash chain: ✓ valid (entry 847 of 847)                        │
└──────────────────────────────────────────────────────────────────┘
```

### 10.2 Data source

Reads the audit log file directly (path from config: `[run] audit_log`).
Parses JSON lines. Does **not** go through the daemon — the audit log is a
plain file (INV-8: append-only, O_APPEND).

### 10.3 Filter & search

- **Tier filter:** All / Tier 0 / Tier 1 / Tier 2 / Tier 3
- **Outcome filter:** All / Approved / Denied / Timeout
- **Text search:** Matches against intent action, target, intent ID
- `Gtk.FilterListModel` wrapping a `Gio.ListStore` of audit entries

### 10.4 Hash-chain verification

- "Verify Integrity" button in header bar
- Runs hash-chain validation in `Gio.Task` thread
- Shows progress bar, then result:
  - ✓ "All 847 entries verified — chain intact"
  - ✗ "Chain broken at entry 423 — possible tampering"

### 10.5 Export

- Header bar "Export" button with dropdown: JSON / CSV
- Respects current filters (exports only visible entries)
- `Gtk.FileDialog` for save location

### 10.6 Files created/modified (M6UI.6)

| Action | File |
|---|---|
| CREATE | `dual-brain/gui/audit/__init__.py` |
| CREATE | `dual-brain/gui/audit/window.py` |
| CREATE | `dual-brain/gui/audit/log_model.py` |
| CREATE | `dual-brain/gui/audit/row_widget.py` |
| CREATE | `dual-brain/gui/audit/detail_panel.py` |
| CREATE | `dual-brain/gui/tests/test_audit_viewer.py` |

**Estimated:** ~600 lines production, ~100 lines test. Target: ~1890 tests.

---

## §11 — CI gates + integration (M6UI.7 — PR #38)

### 11.1 New CI gates

| Gate | What it checks |
|---|---|
| G23 | `dual-brain/gui/` imports without error (`python -c "import gui"`) |
| G24 | `.desktop` files pass `desktop-file-validate` |
| G25 | Config schema accepts new `[desktop]` section |
| G26 | No `SecretRef.resolve()` calls in `gui/` — static grep (BP-8) |
| G27 | All `Gtk.Label.set_text()` calls in `gui/` go through `sanitize_for_gtk()` — static grep (BP-3) |
| G28 | HITL lockout timer uses monotonic clock — grep for `time.monotonic` in `gui/hitl/` (INV-6) |

### 11.2 Test strategy

| Layer | Approach |
|---|---|
| Unit (widgets, theme) | Headless: test CSS class application, color token values, sanitization logic. No display needed. |
| Config (settings) | Validate TOML round-trip: build config dict → write → re-read → assert equal. Schema validation with valid/invalid inputs. |
| Integration (daemon IPC) | Mock `DaemonClient` responses. Verify `GtkDaemonClient` marshals correctly. |
| Visual (manual) | Checklist: boot ISO in VirtualBox, open each window, verify layout/colors match spec. Screenshot comparison against reference images. |
| HITL (lockout) | Unit test: assert button insensitive for exactly `lockout_seconds`, using mocked `time.monotonic()`. |
| Audit (hash-chain) | Create synthetic audit log with known hash chain. Verify viewer detects intact and broken chains. |

### 11.3 Documentation updates

| File | Change |
|---|---|
| `CLAUDE.md` | Add Phase 6UI to status table. Add `dual-brain/gui/` to directory map. Add `gui/hitl/dialog.py` to security-critical files. |
| `docs/IMPLEMENTATION_PLAN.md` | Phase 6UI row in status table, closeout paragraph. |
| `cx-distro/build.sh` | Stage 4 chroot: install `.desktop` files and icon. |

### 11.4 Files created/modified (M6UI.7)

| Action | File |
|---|---|
| MOD | `CLAUDE.md` — Phase 6UI status, directory map, security files |
| MOD | `docs/IMPLEMENTATION_PLAN.md` — Phase 6UI closeout |
| MOD | `cx-distro/build.sh` — Stage 4 `.desktop` file installation |
| MOD | `dual-brain/ci.sh` — G23–G28 gates |
| CREATE | Reference screenshots for visual verification |

**Estimated:** ~150 lines production, ~120 lines test. Target: ~1900 tests.

---

## §12 — Risk register

| ID | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R-6UI-1 | LibAdwaita version skew (ISO ships 1.4, dev has 1.5) | Widgets missing or API differences | Medium | Pin `gir1.2-adw-1 >= 1.4` in build deps. Test on Noble's exact version. |
| R-6UI-2 | GTK4 on Wayland: no system tray standard | Tray indicator invisible | Medium | Tray is best-effort. Chatbot header bar shows status dot as fallback. |
| R-6UI-3 | Daemon not running when GUI launches | Chatbot/settings show errors | High | GUI shows "Daemon offline" banner with "Start Daemon" button. Wizard starts daemon automatically. |
| R-6UI-4 | HITL lockout bypass via GTK event injection | Approve during lockout | Low | Button uses `set_sensitive(False)` + check `time.monotonic() >= deadline` in click handler. Belt and suspenders. |
| R-6UI-5 | Settings Panel writes invalid TOML | Daemon refuses to start | Medium | Validate against JSON Schema before write. "Test Config" button that dry-runs daemon startup. |
| R-6UI-6 | Secret leakage in Settings Panel | API key shown in UI | Low | Never resolve `SecretRef`. Field shows env var name only. CI gate G26 greps for `resolve()` calls. |
| R-6UI-7 | Large audit log (>100k entries) causes OOM | Audit Viewer crashes | Medium | Lazy loading: parse only visible window + buffer. `Gtk.ColumnView` with `Gio.ListModel` virtualizes rows. |
| R-6UI-8 | Thread safety: daemon callback mutates widget from wrong thread | GTK crashes | High | All callbacks via `GLib.idle_add()`. CI gate: grep for widget mutations outside `idle_add`. |
| R-6UI-9 | GTK CSS conflicts with system theme | Colors wrong | Low | Use `Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION` (higher than theme). Test with both light and dark system themes. |
| R-6UI-10 | Chatbot message rendering XSS-equivalent (Pango markup injection) | Spoofed UI | Medium | `sanitize_for_gtk()` escapes `<>&` before any label with `use_markup=True`. Prefer `set_text()` over `set_markup()`. |
| R-6UI-11 | First-Boot Wizard skipped by power user | Config not written | Low | CLI still works without GUI config. Wizard is optional — detects `.first-boot-done` sentinel. |

---

## §13 — PR sequence and build order

```
PR #31  M6UI.0  GUI scaffold + theme           ← foundation, no visible UI yet
PR #32  M6UI.1  Settings Panel                 ← first visible window
PR #33  M6UI.2  Main Chatbot                   ← primary daily-use surface
PR #34  M6UI.3  System Tray                    ← always-on status
PR #35  M6UI.4  First-Boot Wizard              ← first-time experience
PR #36  M6UI.5  HITL Dialog upgrade            ← security-critical, needs review
PR #37  M6UI.6  Audit Log Viewer               ← trust/transparency tool
PR #38  M6UI.7  CI gates + integration         ← final validation
```

Each PR is independently mergeable. Later PRs depend on #31 (scaffold) but are
otherwise independent of each other. The order above optimizes for:
1. Scaffold first (everything depends on it)
2. Settings next (highest daily-use value — unblocks users from TOML editing)
3. Chatbot next (primary interaction surface)
4. Remaining components in decreasing daily-use frequency

---

## §14 — Exit criteria

Phase 6UI is complete when:

- [ ] All 8 PRs (#31–#38) merged to `main`
- [ ] CI gates G23–G28 green
- [ ] ~1900 tests passing
- [ ] All six GUI components launch on the ISO (VirtualBox boot test)
- [ ] Settings Panel reads and writes valid `controller.toml`
- [ ] Chatbot successfully completes an NL command via daemon
- [ ] First-Boot Wizard runs to completion on fresh ISO boot
- [ ] HITL Dialog enforces 3-second lockout (timed test)
- [ ] Audit Viewer loads and filters the audit log
- [ ] System Tray shows correct daemon status
- [ ] No `SecretRef.resolve()` calls in `gui/` (G26)
- [ ] All model output sanitized before display (G27)
- [ ] `.desktop` files valid and visible in GNOME Activities

---

## §15 — Estimated effort

| Milestone | Production LOC | Test LOC | Cumulative tests |
|---|---|---|---|
| M6UI.0 Scaffold | ~450 | ~120 | 1769 |
| M6UI.1 Settings | ~900 | ~150 | ~1790 |
| M6UI.2 Chatbot | ~800 | ~130 | ~1810 |
| M6UI.3 Tray | ~200 | ~40 | ~1830 |
| M6UI.4 Wizard | ~550 | ~100 | ~1850 |
| M6UI.5 HITL | ~400 | ~80 | ~1870 |
| M6UI.6 Audit | ~600 | ~100 | ~1890 |
| M6UI.7 CI/docs | ~150 | ~120 | ~1900 |
| **Total** | **~4,050** | **~840** | **~1900** |
