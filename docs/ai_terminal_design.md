# AI Terminal — Design Document

> A split-pane terminal where natural language and Linux coexist. The left panel
> executes; the right panel explains. Beyond CLI commands, it can drive GUI
> applications — presentations, spreadsheets, browsers — with the same
> dual-brain security model that governs filesystem and system operations.
>
> **Status:** Design — not yet approved for implementation.

---

## 1. Vision

### 1.1 The Problem

Three problems, one tool:

1. **Terminals are opaque.** Users see output but never the *reasoning*. Why did
   the system choose `fs.read` over `fs.list`? Why was this Tier 0 and not
   Tier 3? The existing REPL shows a spinner, then a result. Nobody learns.

2. **CLI can't reach everything.** Presentations, spreadsheets, design tools,
   email clients — these have GUIs with no CLI equivalent. Today's AI coding
   tools (Claude, Gemini, Cursor) can manipulate code but not the applications
   users actually spend their day in. The OS-level AI should be able to say
   "add a slide with this chart" or "bold the header row in the spreadsheet."

3. **When CLI fails, there's no fallback.** Some operations have no clean CLI
   path — legacy apps, proprietary GUIs, edge cases where a tool's CLI is
   broken or missing. Without a structured fallback, users are stuck.

### 1.2 The Solution

The **AI Terminal** is a single tool that addresses all three:

- **Split-pane NL terminal** — left panel executes, right panel explains in
  real time. Newcomers learn; experts verify.
- **GUI automation layer** — the same dual-brain pipeline that safely runs
  `fs.write` can safely click a button in LibreOffice or type into Firefox,
  with the same risk classification, HITL gates, and audit trail.
- **RPA fallback (Robot Framework)** — when neither CLI tools nor structured
  GUI automation can handle an operation, a Robot Framework bridge provides
  a last-resort execution path with extra safety constraints.

### 1.3 Why This Is a Breakthrough

Other AI tools today operate in one domain: code editors manipulate code,
terminal tools run shell commands, RPA tools click buttons. None of them
offer a **unified, security-audited interface** that spans CLI, GUI, and RPA
with a single natural language input. Icebreaker does, because:

- The **dual-brain architecture** already separates intent-understanding (QB)
  from execution (PB). GUI actions are just another tool category for the PB.
- The **Controller's 14-step pipeline** already provides risk classification,
  HITL approval, COW preview, verifier voting, and tamper-evident audit logging.
  Extending these to GUI operations is an architectural fit, not a bolt-on.
- The **mcpd sandbox** (Landlock + Seccomp-BPF) already enforces least privilege.
  GUI tools get their own sandbox profile — wider than `fs.read`, narrower than
  root.

### 1.4 Target Users

| User | Primary value | Example |
|---|---|---|
| Linux newcomer | Learns by watching the right panel | "# what's using my disk" → sees the full reasoning trace |
| Knowledge worker | Drives presentations and spreadsheets by voice/text | "# add a chart of Q3 revenue to slide 4" |
| Power user | Complex multi-step workflows in one sentence | "# find all PDFs over 10MB, compress them, and email the report" |
| Security auditor | Real-time visibility into every AI decision | Watches risk tier, sandbox scope, and audit hash for every action |
| Accessibility user | Screen-reader-friendly narration of all operations | Right panel provides structured step-by-step announcements |

---

## 2. UI/UX Design

### 2.1 Layout

```
┌─────────────────────────────────────┬──────────────────────────────┐
│  EXECUTION PANEL                    │  EXPLANATION PANEL           │
│                                     │                              │
│  icebreaker ~ $                     │  ░ Idle — waiting for input  │
│  # what files are in my home dir    │                              │
│                                     │  ┌─ Pipeline Step 1/14 ─────│
│  ╭─ Intent ─────────────────────╮   │  │ QB: Parsing natural      │
│  │ action: fs.list              │   │  │ language...               │
│  │ target: /home/aditya         │   │  │                           │
│  │ risk:   Tier 0 (read-only)   │   │  │ Recognized intent:       │
│  ╰──────────────────────────────╯   │  │ "list files" → fs.list   │
│                                     │  │ Target: $HOME (safe)      │
│  Documents/  Downloads/  Music/     │  │                           │
│  Pictures/   Videos/     .config/   │  │ Risk: Tier 0 — read-only  │
│                                     │  │ tool on user's own home   │
│  icebreaker ~ $                     │  │ directory. No HITL needed. │
│  # bold row 1 in the spreadsheet    │  │                           │
│                                     │  │ Executing via mcpd...     │
│  ╭─ GUI Action ─────────────────╮   │  │ Tool: fs.list             │
│  │ action: gui.app.calc.format  │   │  │ Sandbox: Landlock allow   │
│  │ target: LibreOffice Calc     │   │  │ Result: 6 entries         │
│  │ risk:   Tier 1 (user doc)    │   │  └───────────────────────────│
│  │ [screenshot preview]         │   │                              │
│  ╰──────────────────────────────╯   │  ┌─ GUI Turn ───────────────│
│                                     │  │ App: LibreOffice Calc     │
│  Done. Row 1 bolded.               │  │ Method: AT-SPI (a11y)     │
│                                     │  │ Element: row 1 header     │
│  icebreaker ~ $ █                   │  │ Before: [screenshot hash] │
│                                     │  │ Risk: Tier 1 — modifying  │
│                                     │  │ user-owned document, no   │
│                                     │  │ system impact. Auto-exec. │
│                                     │  │ After: [screenshot hash]  │
│                                     │  └───────────────────────────│
├─────────────────────────────────────┴──────────────────────────────┤
│ F1 Help  F2 Toggle NL  F6 GUI Log  F9 Resize  F10 Quit           │
└───────────────────────────────────────────────────────────────────-┘
```

**Key layout properties:**

- Default split: 60/40 (execution/explanation). Resizable with `F9` or drag.
- Right panel collapses entirely with `F3` (full-width terminal mode).
- Left panel collapses with `F4` (full-width explanation mode — review CoT).
- `F6` opens a GUI action log overlay showing recent GUI operations with
  before/after screenshot thumbnails.
- Bottom status bar shows active keybindings, current mode, and the `#` hint.
- Minimum terminal width: 80 columns (right panel auto-hides below this).

### 2.2 Input Modes

Users need a way to signal "this is natural language, not a shell command."
Three complementary mechanisms:

| Mechanism | How it works | Example |
|---|---|---|
| **`#` prefix** | Type `#` as the first character of a line. Everything after it is treated as natural language. | `# what's eating my disk space` |
| **`F2` toggle** | Toggles between Shell Mode and NL Mode. In NL Mode, all input is natural language (no `#` needed). Prompt changes to `icebreaker NL >` to make the mode visible. | Press `F2`, type `show network connections`, press Enter |
| **`Ctrl+Space`** | One-shot NL: the next line is treated as natural language regardless of mode. Returns to previous mode after execution. | `Ctrl+Space`, type `restart nginx safely` |

**Why `#`?** It's the comment character in bash — lines starting with `#` are
already no-ops in a shell. Repurposing it for NL input means: (a) muscle memory
from shell commenting, (b) safe fallback if the AI terminal isn't running (the
line is just a comment), (c) visually distinct from commands.

**Shell passthrough:** Any input that doesn't start with `#` (and NL mode is off)
is passed directly to the user's shell (`$SHELL`). The AI Terminal is a wrapper,
not a replacement. `ls`, `git status`, `vim` all work unchanged.

**Edge cases:**

| Input | Interpretation | Why |
|---|---|---|
| `#show files` | NL (starts with `#`) | `#` at position 0 = NL trigger |
| `echo "# comment"` | Shell (doesn't start with `#`) | `#` is inside quotes |
| `##` | NL (empty prompt, ignored) | Double-`#` is still `#`-prefixed |
| ` # show files` | Shell (leading space before `#`) | Only column-0 `#` triggers NL |
| `VAR=1 # show files` | Shell (inline comment) | `#` not at position 0 |

### 2.3 Panel Synchronization

The right panel is a **narrated timeline** that scrolls in lockstep with the
left panel's execution:

1. **User types NL input** → Right panel shows "Parsing..." with a step indicator.
2. **QB returns intent** → Right panel shows the parsed intent fields, explains
   why the QB chose this action/target.
3. **Risk classification** → Right panel shows the tier, the reason, and whether
   HITL is needed.
4. **HITL gate** (if Tier 3) → Right panel shows the approval dialog context.
   Left panel shows the approval prompt.
5. **PB generates tool call** → Right panel shows the tool + params, explains
   what the tool does.
6. **mcpd executes** → Right panel shows sandbox status (Landlock scope, Seccomp
   filter), COW status if applicable.
7. **GUI preview** (if GUI action) → Right panel shows before-screenshot and
   predicted outcome. Left panel shows the intent box with a visual preview.
8. **Result** → Left panel shows the output. Right panel shows a summary of what
   happened, the audit entry, and the duration breakdown.

Each step in the right panel is timestamped and collapsible (press `Enter` on a
step header to expand/collapse detail).

### 2.4 Scrolling

- Each panel scrolls independently (mouse wheel or `Alt+Up/Down` for the right
  panel while cursor stays in the left).
- `Shift+PageUp/Down` scrolls the right panel.
- Left panel scrollback is the normal terminal scrollback buffer.
- Right panel scrollback keeps the last N CoT traces (configurable, default 50).

### 2.5 Accessibility

- **$NO_COLOR:** All box-drawing characters fall back to ASCII (`|`, `-`, `+`).
  Colors are suppressed. Step indicators use text labels instead of color.
- **Screen reader:** Right panel content is structured with semantic labels
  (already have `ScreenReaderPresenter` pattern). Each step is announced as
  "Step N of 14: [label]".
- **High contrast:** Respects terminal theme. Explanation panel uses the
  terminal's default foreground/background; only tier labels use color (and have
  text fallback: `[SAFE]`, `[WARN]`, `[DANGER]`, `[BLOCK]`).

### 2.6 Color Language

| Element | Color | Meaning |
|---|---|---|
| Tier 0 label | Green | Safe, auto-executed |
| Tier 1 label | Cyan | Low risk, audited |
| Tier 2 label | Yellow | Medium risk, notified |
| Tier 3 label | Red | High risk, requires approval |
| Pipeline step header | Bold | Active step |
| Pipeline step (done) | Dim | Completed step |
| NL prompt indicator | Magenta | Distinguishes NL input from shell |
| Intent box border | Blue | Structured data display |
| GUI action indicator | Yellow border | Visual cue that a GUI app is being touched |
| RPA fallback indicator | Red border | Extra-visible: we're in last-resort mode |

---

## 3. Technical Architecture

### 3.1 Three Execution Tiers

The AI Terminal routes every NL command through one of three execution tiers,
in order of preference. Each tier inherits all security properties from the
dual-brain pipeline; they differ only in the execution mechanism.

```
User NL Input
    │
    ▼
┌─────────────────────────────┐
│  Tier A: CLI Tools (mcpd)   │  Preferred. 22 existing tools.
│  fs.*, system.*, service.*  │  Landlock + Seccomp sandboxed.
│  network.*, process.*,      │  Schema-validated. Sub-100ms.
│  package.*                  │
└──────────┬──────────────────┘
           │ If no CLI tool covers the intent
           ▼
┌─────────────────────────────┐
│  Tier B: GUI Automation     │  New. AT-SPI + D-Bus + app APIs.
│  gui.screenshot, gui.click  │  Sandboxed via D-Bus policy.
│  gui.type, gui.find_element │  Screenshot-based COW preview.
│  app.calc.*, app.impress.*  │  HITL for write operations.
└──────────┬──────────────────┘
           │ If GUI automation can't reach the element
           │ (no a11y tree, proprietary app, broken AT-SPI)
           ▼
┌─────────────────────────────┐
│  Tier C: RPA Fallback       │  Last resort. Robot Framework.
│  rpa.execute_workflow       │  Coordinate-based input injection.
│  rpa.run_keyword            │  Always Tier 3 (HITL required).
│  rpa.record_macro           │  Time-boxed. Extra audit. Visual
│                             │  confirmation before + after.
└─────────────────────────────┘
```

The Controller decides the tier. The user never has to know which tier is
executing — they see the same NL interface. The right panel shows which tier
was chosen and why.

### 3.2 Full Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                       AI Terminal (TUI)                           │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │  Execution Panel     │  │  Explanation Panel               │  │
│  │  (PTY + shell)       │  │  (CoT renderer)                  │  │
│  │                      │  │                                  │  │
│  │  Input router:       │  │  Event consumer:                 │  │
│  │  # → NL pipeline     │  │  turn.progress → step UI         │  │
│  │  else → $SHELL PTY   │  │  turn.cot → reasoning            │  │
│  └──────────┬───────────┘  │  turn.gui → screenshot preview   │  │
│             │              │  turn.rpa → fallback indicator    │  │
│             │ NL input     │  turn.result → summary            │  │
│             ▼              └──────────────▲────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │          Daemon Client (AF_UNIX JSON-RPC)                    │ │
│  │          existing: controller/client.py                      │ │
│  └────────────────────────┬─────────────────────────────────────┘ │
└───────────────────────────┼──────────────────────────────────────┘
                            │ AF_UNIX socket
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│               Controller Daemon (existing + extended)            │
│               controller/daemon.py                               │
│                                                                  │
│  QB ──► Intent ──► Risk ──► HITL ──► PB ──► Dispatcher          │
│                                              │                   │
│                              ┌───────────────┼───────────────┐   │
│                              ▼               ▼               ▼   │
│                         ┌─────────┐   ┌───────────┐   ┌───────┐ │
│                         │  mcpd   │   │ GUI Agent │   │  RPA  │ │
│                         │ (stdio) │   │ (D-Bus /  │   │Bridge │ │
│                         │ 22 CLI  │   │  AT-SPI)  │   │(Robot │ │
│                         │ tools   │   │           │   │ Frmwk)│ │
│                         └─────────┘   └───────────┘   └───────┘ │
│                                                                  │
│  Streams: turn.progress, turn.cot, turn.gui, turn.rpa,          │
│           turn.token, turn.result                                │
└──────────────────────────────────────────────────────────────────┘
```

### 3.3 Key Design Decision: Daemon Client, Not Embedded Controller

The AI Terminal does **not** embed the Controller. It connects to the existing
Controller daemon over AF_UNIX JSON-RPC — the same protocol that the existing
`ClientRepl` uses. This means:

- The daemon manages QB, PB, mcpd, GUI agent, and RPA bridge lifecycle.
- Multiple terminals can connect to the same daemon (separate sessions).
- The terminal process is lightweight — just UI rendering and a socket.
- If the terminal crashes, the daemon (and its state) survives.

### 3.4 New Daemon Events

The existing daemon streams `turn.progress`, `turn.token`, `turn.result`.
We add three new event types:

**`turn.cot`** — Chain-of-thought narration (from Controller logic, not LLMs):
```json
{
  "jsonrpc": "2.0",
  "method": "turn.cot",
  "params": {
    "step_index": 2,
    "step_name": "risk_classification",
    "heading": "Risk Classification",
    "body": "Action fs.list on /home/aditya is read-only. Tier 0. No HITL needed.",
    "data": { "tier": 0, "reason": "read_only_tool_in_home", "reversible": true },
    "timestamp_ms": 1718612345678
  }
}
```

**`turn.gui`** — GUI operation preview/result:
```json
{
  "jsonrpc": "2.0",
  "method": "turn.gui",
  "params": {
    "phase": "preview",
    "app": "LibreOffice Calc",
    "window_title": "Q3_Revenue.ods",
    "element": { "role": "cell", "name": "A1", "locator": "/org/a11y/..." },
    "action": "format_bold",
    "screenshot_before_hash": "a3f8...",
    "screenshot_before_path": "/tmp/icebreaker-gui/preview_001.png",
    "predicted_outcome": "Cell A1 text will be bolded"
  }
}
```

**`turn.rpa`** — RPA fallback execution status:
```json
{
  "jsonrpc": "2.0",
  "method": "turn.rpa",
  "params": {
    "phase": "executing",
    "workflow_name": "click_save_button",
    "keyword_index": 2,
    "keyword_total": 5,
    "current_keyword": "Click Element    id:save-btn",
    "timeout_remaining_ms": 25000,
    "screenshot_path": "/tmp/icebreaker-rpa/step_002.png"
  }
}
```

### 3.5 Shell Integration via PTY

The left panel is a real terminal emulator — it allocates a PTY and runs the
user's `$SHELL`. This means:

- Full-featured shell: tab completion, history, job control, piping, etc.
- Programs that need a TTY (vim, htop, less) work correctly.
- The AI Terminal intercepts input *before* it reaches the PTY to check for
  the `#` prefix or NL mode toggle.

```
Keypress
  │
  ├─ F-key / Ctrl+Space ? → handle internally (toggle mode, resize, etc.)
  │
  ├─ NL Mode active OR line starts with "#" ?
  │     → buffer until Enter
  │     → strip "#" prefix
  │     → send to daemon via client.send_request("turn.run", {input: text})
  │     → display result in left panel, CoT in right panel
  │
  └─ else → forward to PTY (normal shell operation)
```

### 3.6 HITL Integration

When the pipeline hits a Tier 2+ GUI operation or any Tier 3 operation, the
daemon sends a `hitl.prompt` notification. The approval prompt appears in the
left panel while the right panel shows the full context (what the operation
does, why it's that tier, the screenshot preview). This keeps the spatial
metaphor: left = action, right = explanation.

For GUI operations specifically, the HITL display includes:
- A before-screenshot (or the relevant portion of the screen)
- The element being targeted (highlighted in the screenshot if possible)
- The predicted outcome in plain English
- The tier and reason

---

## 4. GUI Automation Layer (Tier B)

### 4.1 Why AT-SPI Is the Primary Mechanism

Linux GUI automation has several paths. We evaluated all of them:

| Mechanism | How it works | Security risk | Verdict |
|---|---|---|---|
| **xdotool (X11)** | Direct X11 protocol: send synthetic key/mouse events | Catastrophic: any X11 client can snoop all keystrokes on the display | Rejected |
| **AT-SPI / Dogtail** | D-Bus accessibility API: inspect and interact with GUI elements by role/name | Moderate: D-Bus session bus is shared, but calls are attributable and auditable | **Primary choice** |
| **D-Bus app APIs** | Direct API (LibreOffice UNO, GNOME Shell introspection) | Low: structured, well-scoped, per-app APIs | **Preferred when available** |
| **ydotool (Wayland)** | `/dev/uinput` input injection | High: input device is privileged, could be used for keylogging | Fallback only (via RPA) |
| **python-xlib** | Low-level X11 bindings | Same as xdotool — catastrophic | Rejected |

**AT-SPI (Assistive Technology Service Provider Interface)** is the right choice
because:

1. **Semantic, not coordinate-based.** AT-SPI interacts with elements by role
   (button, text field, menu item) and name, not pixel coordinates. This means
   actions are resilient to window resizing, theme changes, and DPI scaling.
2. **Auditable.** Every AT-SPI call goes through the D-Bus session bus, which
   can be monitored and logged. We know exactly which element was targeted.
3. **Standardized.** AT-SPI is the Linux accessibility standard. GNOME, KDE,
   LibreOffice, Firefox, and Chromium all expose AT-SPI trees. If an app has
   accessibility support (and accessibility is a compliance requirement), we
   can automate it.
4. **No extra syscalls.** AT-SPI uses D-Bus over AF_UNIX sockets — already
   allowed by mcpd's seccomp filter. No Landlock or seccomp changes needed.
5. **Read and write separation.** Inspecting the AT-SPI tree (Tier 0) is a
   different operation from clicking a button (Tier 1–3). We can classify
   them independently.

### 4.2 App-Specific APIs (D-Bus)

For well-known applications, we prefer their native D-Bus APIs over generic
AT-SPI when available:

| Application | D-Bus Interface | Capabilities |
|---|---|---|
| LibreOffice | UNO bridge (`com.sun.star.*`) | Full document manipulation: insert slides, format cells, export PDF |
| Firefox/Chromium | DevTools Protocol (via D-Bus or websocket) | DOM inspection, page navigation, form filling, screenshot |
| GNOME Files | `org.gnome.Nautilus` | Open folder, select files, rename |
| Evince/Document Viewer | `org.gnome.Evince` | Open PDF, navigate pages, search |
| GNOME Terminal | `org.gnome.Terminal` | Open tab, set profile, run command |

App-specific APIs are more reliable and more auditable than generic AT-SPI
clicks. The Controller tries them first; falls back to AT-SPI if unavailable.

### 4.3 GUI Tool Catalogue (New mcpd Tools)

These tools would be added to mcpd as a new `gui.*` category:

**Read-Only (Tier 0):**

| Tool | Description |
|---|---|
| `gui.screenshot` | Capture full screen or a specific window. D-Bus call to compositor. |
| `gui.find_element` | Search the AT-SPI tree for elements matching a role/name/label query. Returns element descriptors with locators. |
| `gui.get_window_list` | List all visible windows with title, app, geometry, and PID. |
| `gui.get_element_tree` | Return the accessibility tree for a specific window. Structured JSON. |
| `gui.get_clipboard` | Read the current clipboard contents (text only; images as hash reference). |

**Write — User Documents (Tier 1):**

| Tool | Description | COW equivalent |
|---|---|---|
| `gui.click` | Click an element identified by AT-SPI locator. | Before-screenshot + predicted outcome |
| `gui.type` | Type text into the focused element or a specific AT-SPI text field. | Before-screenshot + text preview |
| `gui.select` | Select a menu item, dropdown option, or list entry. | Before-screenshot + selection preview |
| `gui.set_clipboard` | Write text to the clipboard. | Previous clipboard contents saved |

**Write — System-Level (Tier 2):**

| Tool | Description |
|---|---|
| `gui.app.launch` | Launch an application by name or `.desktop` entry. |
| `gui.app.close` | Close an application window (with save prompt detection). |
| `gui.notification.send` | Send a desktop notification via D-Bus. |

**Destructive (Tier 3 — Always HITL):**

| Tool | Description |
|---|---|
| `gui.keystroke` | Send arbitrary keystrokes (including modifier combos). Could trigger anything. |
| `gui.app.force_close` | Force-kill an application window without save prompt. |

### 4.4 GUI COW: Screenshot-Based Dry-Run

For filesystem operations, mcpd has COW (Copy-on-Write): create a snapshot,
show the diff, get approval, then commit. For GUI operations, the equivalent
is **screenshot-based preview**:

```
1. User says: "# bold the header row in the spreadsheet"
2. Controller classifies: gui.click → LibreOffice Calc → Tier 1
3. GUI Agent:
   a. Takes BEFORE screenshot of the target window
   b. Locates the element via AT-SPI tree (Row 1, header cells)
   c. Builds an action plan: [select row 1, click Bold button]
   d. Returns the plan + before-screenshot to Controller
4. Controller sends to right panel:
   - Before-screenshot with element highlighted
   - Action plan in plain English
   - Predicted outcome: "Row 1 will be formatted as bold"
5. For Tier 1: auto-execute (but user sees the preview in right panel)
   For Tier 3: HITL gate — user must approve
6. GUI Agent executes the action
7. Takes AFTER screenshot
8. Controller compares before/after, reports result
9. Audit log records: action, element locator, before-hash, after-hash
```

**Key property:** The user can always see what's about to happen *before* it
happens, and verify what happened *after* it happened. The right panel shows
the full trace.

### 4.5 How the QB Understands GUI Intent

The QB (Quarantined Brain) doesn't need to know about AT-SPI or D-Bus. It
produces Intent Objects with the same schema it uses for CLI tools:

```json
{
  "intent_id": "550e8400-...",
  "action": "gui.click",
  "target": "LibreOffice Calc :: Bold button",
  "params": {
    "window": "Q3_Revenue.ods - LibreOffice Calc",
    "element_role": "push_button",
    "element_name": "Bold"
  },
  "reason": "user_requested",
  "risk_level": "low"
}
```

The **existing intent schema already supports this.** The `action` pattern
`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` allows `gui.click`. The `target` and
`params` fields accept strings without shell metacharacters. No schema changes
needed.

The Controller maps the intent to the right execution tier:
- `gui.*` → GUI Agent (Tier B)
- `rpa.*` → RPA Bridge (Tier C)
- Everything else → mcpd (Tier A)

### 4.6 Sandbox Model for GUI Operations

GUI tools require access that mcpd's current sandbox doesn't provide. Rather
than widening mcpd's sandbox (which would weaken it for CLI tools), we run
the **GUI Agent as a separate sandboxed process**:

```
┌────────────────────────────────────────────────┐
│               Controller Daemon                 │
│                                                 │
│  ┌───────────────┐     ┌────────────────────┐  │
│  │ mcpd (Rust)   │     │ gui-agent (Python)  │  │
│  │               │     │                     │  │
│  │ Landlock:     │     │ Landlock:            │  │
│  │  /proc (ro)   │     │  $HOME (ro)          │  │
│  │  /sys (ro)    │     │  /tmp/icebreaker-gui │  │
│  │  /etc (ro)    │     │   (rw, screenshots)  │  │
│  │  $HOME (rw)   │     │                     │  │
│  │  /tmp (rw)    │     │ Seccomp:            │  │
│  │               │     │  socket(AF_UNIX) ✓  │  │
│  │ Seccomp:      │     │  connect() ✓        │  │
│  │  22 CLI tools │     │  sendmsg/recvmsg ✓  │  │
│  │               │     │  (D-Bus only)       │  │
│  │ Stdio only    │     │                     │  │
│  │ No network    │     │ D-Bus session bus   │  │
│  └───────────────┘     │ AT-SPI registry     │  │
│                        │ No raw X11 access   │  │
│                        │ No /dev/uinput      │  │
│                        │ No network          │  │
│                        └────────────────────┘  │
└────────────────────────────────────────────────┘
```

**Key sandbox properties of the GUI Agent:**

1. **D-Bus only.** The GUI Agent communicates with applications exclusively
   through the D-Bus session bus. No raw X11 socket access (prevents keystroke
   snooping). No `/dev/uinput` (prevents input injection).
2. **Read-only home.** The GUI Agent can read files in `$HOME` (to understand
   document context) but cannot write. All writes go through the GUI application
   itself (which has its own save dialogs and undo).
3. **Screenshot scratch space.** `/tmp/icebreaker-gui/` is the only writable
   directory — used for before/after screenshots. Cleaned up on session end.
4. **No network.** The GUI Agent cannot make HTTP calls. If a GUI operation
   needs network (e.g., "email this file"), it delegates to mcpd's
   network tools, not to the GUI Agent.
5. **Separate process.** If the GUI Agent crashes or hangs, mcpd and the
   Controller are unaffected. The Controller enforces timeouts.

---

## 5. RPA Fallback Layer (Tier C) — Robot Framework Bridge

### 5.1 When Tier C Is Needed

Tier C is the **last resort**. It activates only when:

1. No CLI tool covers the operation (Tier A unavailable), AND
2. The target application has no AT-SPI accessibility tree or D-Bus API
   (Tier B unavailable or unable to locate the element)

Real-world examples where Tier C is needed:
- Legacy Java Swing apps with broken accessibility
- Proprietary enterprise apps (e.g., SAP GUI) with no D-Bus interface
- Electron apps that disable Chromium's accessibility layer
- Edge cases where an AT-SPI element exists but is incorrectly labeled

### 5.2 Why Robot Framework

Robot Framework is the RPA bridge because:

1. **Keyword-driven.** Actions are expressed as structured keywords, not free-form
   scripts. This maps naturally to our Intent Object model.
2. **Pluggable libraries.** We can use different automation backends:
   - `RPA.Desktop` for coordinate-based input (xdotool/ydotool)
   - `Browser` (Playwright) for web apps
   - `RPA.Images` for image-based element detection (when AT-SPI fails)
3. **Logging built in.** Robot Framework generates detailed execution logs with
   screenshots at each step — feeds directly into our audit trail.
4. **Timeout/retry semantics.** Built-in `Wait Until` keywords with configurable
   timeouts — we enforce these from the Controller.
5. **Well-maintained.** Active community, 9k+ GitHub stars, enterprise adoption.

### 5.3 RPA Security Model — Extra Constraints

Because RPA operates at the input injection level (synthetic keypresses and
mouse clicks by coordinate), it gets **stricter security constraints** than
Tier A or Tier B:

| Constraint | Why | How |
|---|---|---|
| **Always Tier 3** | Coordinate-based input can hit anything. We can't verify the target element semantically. | `risk_classifier.py`: any `rpa.*` action → `Tier.HIGH` |
| **Always HITL** | No auto-execute, no trust grants for RPA operations. Every single one needs human approval. | Trust store hard-blocks `rpa.*` actions (same pattern as `Tier >= HIGH → never grantable`) |
| **Time-boxed** | RPA workflows must complete within a strict timeout. Prevents runaway automation. | Controller enforces `rpa.timeout_seconds` (default 30s). Kill on expiry. |
| **Screenshot every step** | Each Robot Framework keyword execution produces a screenshot. All screenshots are hash-logged in the audit trail. | Robot Framework's built-in screenshot-on-keyword feature + audit integration |
| **Visual confirmation** | Before executing, show the user a screenshot with a crosshair on the target coordinates. After executing, show the result screenshot. | `turn.rpa` event carries `screenshot_path` at each step |
| **No chaining** | RPA actions cannot be chained in a single turn. Each NL command produces at most one RPA workflow. Multi-step RPA requires multiple user confirmations. | Controller limits `rpa.execute_workflow` to one per turn |
| **Sandboxed process** | Robot Framework runs in its own process with Landlock restricting filesystem access to `/tmp/icebreaker-rpa/` (screenshots) and the D-Bus session bus. | Same sandbox pattern as GUI Agent, but with `/dev/uinput` added for input injection |
| **Audit escalation** | RPA audit entries include `execution_tier: "rpa_fallback"` and every screenshot hash, so auditors can distinguish RPA from AT-SPI operations. | Extra fields in `audit.py` |

### 5.4 RPA Workflow Structure

Robot Framework keywords map to structured actions:

```robot
*** Settings ***
Library    RPA.Desktop
Library    RPA.Images

*** Keywords ***
Click Save Button In Legacy App
    [Documentation]    Tier C fallback: click Save by image match
    ${loc}=    Find Element    image:save_button.png    confidence=0.9
    Log Screenshot    before_click
    Click Element    ${loc}
    Sleep    500ms
    Log Screenshot    after_click
    Wait Until Element Visible    image:saved_confirmation.png    timeout=5s
```

The Controller generates these workflows from the PB's tool call output:

```
PB output:  {"tool": "rpa.execute_workflow", "params": {"target": "save_button", "method": "image_match"}}
Controller: generates Robot Framework keyword sequence
Controller: sends to RPA Bridge for execution
RPA Bridge: runs keywords, captures screenshots, returns result
Controller: audits everything, sends screenshots to right panel
```

### 5.5 RPA Tool Catalogue

| Tool | Description | Constraint |
|---|---|---|
| `rpa.execute_workflow` | Run a pre-defined or generated Robot Framework keyword sequence | Always Tier 3. Max 30s. HITL required. |
| `rpa.find_by_image` | Locate an element on screen by image template matching | Tier 0 (read-only). Returns coordinates + confidence. |
| `rpa.record_macro` | Record a sequence of user actions for later replay | Tier 3. Creates a reusable workflow. Requires naming + approval. |
| `rpa.list_workflows` | List available pre-defined RPA workflows | Tier 0 (read-only). |

### 5.6 Graceful Escalation from Tier B to Tier C

The Controller doesn't jump to RPA immediately. The escalation path is:

```
1. User: "# click Save in LegacyApp"
2. Controller → GUI Agent: find element "Save" in "LegacyApp"
3. GUI Agent: AT-SPI tree query for role=push_button, name="Save"
4. GUI Agent: NOT FOUND (app has no accessibility tree)
5. GUI Agent → Controller: { "status": "element_not_found", "reason": "no_a11y_tree" }
6. Controller: escalate to Tier C
7. Controller → right panel: "AT-SPI could not find the Save button. Falling back
   to RPA (image-based detection). This requires your approval."
8. Controller → RPA Bridge: find_by_image("save_button")
9. RPA Bridge: takes screenshot, runs template matching, finds button at (523, 412)
10. Controller → HITL prompt: "RPA will click at (523, 412) on LegacyApp window.
    [screenshot with crosshair]. Approve?"
11. User approves → RPA Bridge clicks → captures after-screenshot → audit logs
```

The right panel shows every step of this escalation, so the user understands
*why* RPA was needed and can see the fallback reasoning.

---

## 6. Security Deep-Dive

The dual-brain architecture was designed for CLI tool execution. Extending it
to GUI and RPA introduces new attack surfaces. Here's how every invariant
is preserved:

### 6.1 INV-1: Brain Isolation

**Threat:** GUI screenshots contain rich visual information. If the QB sees a
screenshot of a login page, it could extract credentials.

**Mitigation:** Screenshots are stored as opaque references (file paths with
SHA-256 hashes). The QB never receives screenshot pixel data. The PB receives
only the element locator (e.g., `role=push_button, name=Save`), never the
screenshot. The Controller manages screenshots in a scratch directory and
passes only hashes through the pipeline.

```
QB sees:  intent_id, action, target (text only)
PB sees:  intent_id, allowed_tool, tool_schema (no screenshots, no window content)
GUI Agent sees: element locator, action to perform (no user NL input)
```

### 6.2 INV-2: Controller Schema Enforcement

**Threat:** A malicious intent could embed shell metacharacters in a GUI element
selector (e.g., `element_name: "Save; rm -rf /"`).

**Mitigation:** The existing intent schema already blocks shell metacharacters
in `target` and `params` values with the pattern `^[^;&|`$<>\x00-\x1f]*$`.
GUI element selectors are validated against the same pattern. Additionally,
the GUI Agent validates selectors against the AT-SPI tree before execution — a
selector that doesn't match any real element is rejected.

### 6.3 INV-4: Parameter Validation Before Execution

**Threat:** A GUI tool call with malformed coordinates could trigger unintended
clicks.

**Mitigation:** Every GUI tool has a JSON schema (same as CLI tools). For
`gui.click`, the schema enforces that the locator is a valid AT-SPI path or
that coordinates are within the target window's geometry. For RPA tools, the
schema enforces that coordinates are within the active display bounds.

### 6.4 INV-5: No Execution Without Sandboxing

**Threat:** The GUI Agent or RPA Bridge could access files or network beyond
what's needed for GUI automation.

**Mitigation:** Both run as separate sandboxed processes:
- **GUI Agent:** Landlock allows `$HOME` (read-only) + `/tmp/icebreaker-gui/`
  (read-write). Seccomp allows D-Bus sockets only. No network. No `/dev/uinput`.
- **RPA Bridge:** Same as GUI Agent, plus `/dev/uinput` for input injection
  (group-gated). The wider sandbox is why RPA is always Tier 3.

### 6.5 INV-6: COW Before Destructive Operations

**Threat:** A GUI action modifies an application's state (e.g., deletes a slide)
with no way to preview the change.

**Mitigation:** Screenshot-based COW:
- **Before-screenshot** captured before any write action.
- **Predicted outcome** displayed in the right panel.
- **After-screenshot** captured after execution.
- **Before/after hashes** logged in the audit trail.
- For Tier 3 operations, the user sees the before-screenshot and must approve.
- For applications with undo (most GUI apps), the explanation panel shows
  "Undo available: Ctrl+Z in LibreOffice" after execution.

### 6.6 INV-7: Model Weight Integrity (Unchanged)

GUI automation doesn't change the model loading path. Models are still verified
against `checksums.sha256` before loading. No new model files are introduced.

### 6.7 INV-8: Audit Log Integrity

**Extension for GUI/RPA:**

Every GUI and RPA action gets an audit entry with these additional fields:

```json
{
  "execution_tier": "gui_atspi",
  "element_locator": "/org/a11y/atspi/accessible/23/42",
  "element_role": "push_button",
  "element_name": "Bold",
  "app_name": "LibreOffice Calc",
  "window_title": "Q3_Revenue.ods",
  "screenshot_before_hash": "a3f8c2d1...",
  "screenshot_after_hash": "7b2e9f03...",
  "a11y_tree_hash": "c4d6e8f0..."
}
```

For RPA operations, additional fields:
```json
{
  "execution_tier": "rpa_fallback",
  "rpa_keywords_executed": 3,
  "rpa_timeout_ms": 30000,
  "rpa_elapsed_ms": 4520,
  "rpa_screenshot_hashes": ["aaa...", "bbb...", "ccc..."],
  "rpa_fallback_reason": "no_a11y_tree"
}
```

All entries are hash-chained (same as CLI audit entries). Auditors can filter
by `execution_tier` to review GUI/RPA operations specifically.

### 6.8 BP-3: Display Sanitization for GUI Data

**Threat:** A window title or element name could contain ANSI escape sequences
that hijack the terminal rendering.

**Mitigation:** All GUI-sourced strings (window titles, element names, element
text content) are sanitized through `_sanitize_display()` before rendering in
either panel. This strips ANSI escapes, C0/C1 control characters, and
neutralizes `\r`/`\n`. Max length: 256 chars for display, 512 chars for audit.

### 6.9 BP-5: Risk Escalation for GUI Operations

The risk classifier for GUI operations follows the escalate-only rule:

```python
# In risk_classifier.py (extended):

GUI_READONLY_TOOLS = {"gui.screenshot", "gui.find_element", "gui.get_window_list",
                      "gui.get_element_tree", "gui.get_clipboard",
                      "rpa.find_by_image", "rpa.list_workflows"}

GUI_WRITE_TOOLS = {"gui.click", "gui.type", "gui.select", "gui.set_clipboard"}

GUI_SYSTEM_TOOLS = {"gui.app.launch", "gui.app.close", "gui.notification.send"}

GUI_DESTRUCTIVE_TOOLS = {"gui.keystroke", "gui.app.force_close"}

RPA_TOOLS = {"rpa.execute_workflow", "rpa.record_macro"}  # Always Tier 3

# Classification:
# 1. RPA → always Tier 3 (hard floor)
# 2. GUI destructive → Tier 3
# 3. GUI system → Tier 2
# 4. GUI write → Tier 1 (user documents) or Tier 2 (system UI)
# 5. GUI readonly → Tier 0
# 6. ML/policy can escalate any of the above, never de-escalate
```

### 6.10 New Threat: GUI-Specific Attack Vectors

| Threat | Severity | Mitigation |
|---|---|---|
| **Clickjacking via element mislabel** — AT-SPI tree reports a "Save" button that's actually "Delete" | High | QB verifier cross-checks element name against window context. Screenshot preview lets user visually confirm. |
| **Timing attack** — element moves between screenshot and click | Medium | GUI Agent re-verifies element position immediately before click. If position changed > 10px, abort and re-screenshot. |
| **Screenshot exfiltration** — screenshots could contain sensitive data (passwords, financial info) | Medium | Screenshots stored in `/tmp/icebreaker-gui/` with 0600 permissions. Cleaned on session end. Never sent over network. Hash-only in audit log (not the pixels). |
| **RPA coordinate drift** — screen resolution or DPI changes between find and click | Medium | RPA verifies display geometry hasn't changed since the last screenshot. Abort on mismatch. |
| **Clipboard poisoning** — malicious app puts attack payload in clipboard | Medium | `gui.get_clipboard` returns text only (no executable content). Content is sanitized before display. Never fed to PB as raw text (INV-1). |
| **D-Bus method injection** — crafted element name triggers unintended D-Bus call | Low | All D-Bus method calls are hard-coded in the GUI Agent (not constructed from element names). Element names are data, not code. |
| **Runaway RPA** — Robot Framework keyword loop doesn't terminate | Medium | Hard timeout enforced by Controller (default 30s). SIGKILL on expiry. Every keyword step is logged. |

---

## 7. Chain-of-Thought Content Model

### 7.1 Trace Structure (CLI Operation)

```
┌─ Turn 3 ──────────────────────────┐
│                                    │
│  INPUT                             │
│  "what's eating my disk space"     │
│                                    │
│  INTENT (Step 1-2)        0.4s     │
│  action: fs.list                   │
│  target: /                         │
│  risk_level: read_only             │
│  Execution tier: CLI (mcpd)        │
│                                    │
│  RISK (Step 3)            0.1s     │
│  Tier 0 — READ_ONLY               │
│  HITL: not required                │
│                                    │
│  EXECUTION (Steps 7-10)   0.3s     │
│  Tool: fs.list                     │
│  Sandbox: Landlock [/, ro]         │
│  Result: 10 entries returned       │
│                                    │
│  AUDIT                    0.001s   │
│  Entry #47  Hash: 8a3f...c2d1     │
│  Total: 0.8s                       │
└────────────────────────────────────┘
```

### 7.2 Trace Structure (GUI Operation)

```
┌─ Turn 4 ──────────────────────────┐
│                                    │
│  INPUT                             │
│  "bold the header row"             │
│                                    │
│  INTENT (Step 1-2)        0.6s     │
│  action: gui.click                 │
│  target: LibreOffice Calc :: Bold  │
│  Execution tier: GUI (AT-SPI)      │
│                                    │
│  RISK (Step 3)            0.1s     │
│  Tier 1 — user document write      │
│  HITL: not required (auto-exec)    │
│                                    │
│  GUI PREVIEW              0.3s     │
│  App: LibreOffice Calc             │
│  Window: Q3_Revenue.ods            │
│  Element: Bold button (push_btn)   │
│  Before: [screenshot a3f8...]      │
│  Action: select row 1, click Bold  │
│                                    │
│  EXECUTION                0.8s     │
│  AT-SPI: select row 1 ✓           │
│  AT-SPI: click Bold ✓             │
│  After: [screenshot 7b2e...]       │
│  Undo: Ctrl+Z in LibreOffice      │
│                                    │
│  AUDIT                    0.001s   │
│  Entry #48  Tier: gui_atspi       │
│  Total: 1.8s                       │
└────────────────────────────────────┘
```

### 7.3 Trace Structure (RPA Fallback)

```
┌─ Turn 5 ──────────────────────────┐
│                                    │
│  INPUT                             │
│  "click Save in LegacyApp"        │
│                                    │
│  INTENT (Step 1-2)        0.5s     │
│  action: gui.click                 │
│  target: LegacyApp :: Save         │
│  Execution tier: GUI → FAILED      │
│                                    │
│  ⚠ ESCALATION             0.2s    │
│  AT-SPI: no accessibility tree     │
│  Falling back to RPA (Tier C)      │
│  All RPA ops require approval.     │
│                                    │
│  RISK (Step 3)            0.1s     │
│  Tier 3 — RPA FALLBACK            │
│  HITL: REQUIRED                    │
│                                    │
│  RPA PREVIEW              1.2s     │
│  Method: image template match      │
│  Target: (523, 412) confidence 94% │
│  [screenshot with crosshair]       │
│  Timeout: 30s                      │
│                                    │
│  ⏳ AWAITING APPROVAL              │
│  Press [1/a/y] to approve...       │
│                                    │
│  EXECUTION (approved)     0.5s     │
│  RPA keyword 1/3: screenshot ✓    │
│  RPA keyword 2/3: click (523,412) ✓│
│  RPA keyword 3/3: verify result ✓ │
│  After: [screenshot e4f1...]       │
│                                    │
│  AUDIT                    0.001s   │
│  Entry #49  Tier: rpa_fallback    │
│  Total: 3.1s (incl. approval)     │
└────────────────────────────────────┘
```

### 7.4 Verbosity Levels

Configurable via `[terminal.explanation]` in `controller.toml`:

| Level | What shows |
|---|---|
| `minimal` | Tier label + one-line summary per turn. For GUI: app name + action. |
| `normal` (default) | Intent fields, risk reason, execution tier, result. For GUI: element name + before/after status. |
| `verbose` | All pipeline steps with timing, full params, sandbox scope, audit hash. For GUI: full AT-SPI path, screenshot hashes, D-Bus method calls. |
| `debug` | Raw JSON-RPC messages, AT-SPI tree dumps, Robot Framework keyword logs. |

Users can cycle verbosity with `F5` without restarting.

---

## 8. What Already Exists (Build Surface)

### 8.1 Daemon/Client Protocol (fully implemented)

**Files:** `dual-brain/controller/daemon.py`, `dual-brain/controller/client.py`

Already supports `turn.run`, `session.new`, `session.reset`, `hitl.respond`,
`daemon.status`, `daemon.shutdown`, streaming notifications via AF_UNIX JSON-RPC.

**What we need to add:** `turn.cot`, `turn.gui`, `turn.rpa` event emission.

### 8.2 Streaming Events (fully implemented)

**File:** `dual-brain/controller/turn_events.py`

`ProgressEvent`, `TokenEvent`, `ResultEvent`, `ErrorEvent`, `InfoEvent`.

**What we need to add:** `CotEvent`, `GuiEvent`, `RpaEvent`.

### 8.3 Presenter Registry (fully implemented)

**Files:** `dual-brain/controller/presenters/registry.py`, `dual-brain/controller/hitl.py`

`@register_presenter` + `make_presenter` + `HitlPresenter` ABC. Three concrete
implementations: `TerminalPresenter`, `ScreenReaderPresenter`, `GtkPresenter`.

**What we need to add:** `AiTerminalPresenter` for the split-pane HITL rendering.

### 8.4 mcpd Tool Registration (fully implemented)

**Files:** `src/mcpd/src/tools/mod.rs` (descriptors), `src/mcpd/schemas/` (JSON
schemas), `src/mcpd/src/server.rs` (dispatch).

22 tools across 6 categories. Static registration at compile time.

**What we need to add:** `gui.*` tool category (but GUI tools may run in a
separate process rather than inside mcpd — see architecture above).

### 8.5 Risk Classifier (fully implemented)

**File:** `dual-brain/controller/risk_classifier.py`

Tool category → tier mapping with critical path detection, conditional tiers,
escalate-only invariant.

**What we need to add:** GUI and RPA tool categories (GUI_READONLY, GUI_WRITE,
GUI_SYSTEM, GUI_DESTRUCTIVE, RPA_TOOLS).

### 8.6 Audit with Redaction + Hash Chain (fully implemented)

**File:** `dual-brain/controller/audit.py`

O_APPEND, fsync, 3-layer secret redaction (key name, value pattern, entropy),
hash-chain integrity verification.

**What we need to add:** GUI/RPA-specific extra fields (`execution_tier`,
`screenshot_before_hash`, `element_locator`, etc.).

### 8.7 Trust Store (fully implemented)

**File:** `dual-brain/controller/trust_store.py`

TTL-based grants, target-prefix matching, hard floor at `Tier >= HIGH`.

**What we need to add:** GUI element selector prefix matching. RPA is already
blocked by the hard floor (always Tier 3 → never grantable).

### 8.8 Verifier Voting (fully implemented)

**File:** `dual-brain/controller/verifier.py`

Single or majority-vote QB verification of PB tool calls. Fail-safe on error.

**What we need to add:** GUI-specific verification schema (element existence,
predicted state, concern categories).

### 8.9 Keymap System (fully implemented)

**File:** `dual-brain/controller/keymap.py`

Configurable keybindings, Esc hard-reserved for DENY, validated at load time.

**What we need to add:** Terminal-level keybindings (F-keys, Ctrl combos) that
don't conflict with the HITL keymap.

---

## 9. TUI Framework Decision

### 9.1 Recommendation: Textual

**Textual** (by Will McGugan, author of Rich) is the strongest fit:

1. Built-in split-pane layout with resizable panels.
2. Rich text rendering — markdown, syntax highlighting, styled text.
3. Async-native — event-driven architecture matches our daemon notifications.
4. CSS-like theming — `$NO_COLOR` and high-contrast modes are configurable.
5. Built-in test framework for snapshot testing TUI layouts.
6. Pure Python, no C extensions, no security surface.

**New dependencies:** `textual>=0.80` (~2 MB), `rich>=13` (~3 MB).

**Risk:** PTY embedding maturity. If `textual-terminal` proves too immature,
fallback is Textual for both panels with the left panel as a `RichLog` widget
that renders command output (not a full PTY). Shell passthrough would work via
subprocess, not embedded PTY.

### 9.2 Alternative: tmux Sidecar

Zero new dependencies. Run the explanation panel as a separate process writing
to a tmux pane. Less integrated but simpler.

---

## 10. Configuration

```toml
[terminal]
split_ratio = 0.6                    # left panel width (0.0–1.0)
explanation_verbosity = "normal"     # minimal | normal | verbose | debug
shell_explanation = "none"           # none | man | exit_code
cot_scrollback = 50                  # number of CoT traces to keep
nl_prefix = "#"                      # character that triggers NL mode

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
screenshot_every_step = true         # screenshot at each keyword
robot_framework_path = ""            # auto-detect or explicit path
```

---

## 11. New Components Required

| Component | Location | Description |
|---|---|---|
| **Terminal TUI** | | |
| AI Terminal app | `dual-brain/terminal/app.py` | Textual application with split-pane layout |
| Execution panel | `terminal/execution.py` | PTY or subprocess-based command execution widget |
| Explanation panel | `terminal/explanation.py` | CoT trace renderer with collapsible sections |
| Input router | `terminal/input_router.py` | `#` prefix detection, NL mode toggle, PTY forwarding |
| Terminal presenter | `terminal/presenter.py` | `HitlPresenter` implementation for the AI Terminal |
| `--terminal` CLI flag | `controller/__main__.py` | Entry point: start daemon + AI Terminal |
| **Events** | | |
| `CotEvent` | `controller/turn_events.py` | Chain-of-thought narration event |
| `GuiEvent` | `controller/turn_events.py` | GUI preview/result event |
| `RpaEvent` | `controller/turn_events.py` | RPA step/result event |
| CoT emitter | `controller/main.py` | Generates `CotEvent` at each pipeline step |
| **GUI Agent** | | |
| GUI Agent process | `dual-brain/gui_agent/agent.py` | Sandboxed AT-SPI + D-Bus automation |
| AT-SPI client | `gui_agent/atspi.py` | AT-SPI tree query, element interaction |
| App API registry | `gui_agent/app_apis/` | Per-app D-Bus wrappers (LibreOffice, Firefox, etc.) |
| Screenshot manager | `gui_agent/screenshots.py` | Capture, hash, store, cleanup |
| GUI tool schemas | `src/mcpd/schemas/gui.*.json` | JSON schemas for GUI tools |
| **RPA Bridge** | | |
| RPA Bridge process | `dual-brain/rpa_bridge/bridge.py` | Robot Framework executor with Controller integration |
| Workflow generator | `rpa_bridge/workflow_gen.py` | Intent → Robot Framework keyword translation |
| Image matcher | `rpa_bridge/image_match.py` | Template matching for element detection |
| RPA tool schemas | `src/mcpd/schemas/rpa.*.json` | JSON schemas for RPA tools |
| **Config & Risk** | | |
| GUI config section | `controller/config.py` | `[gui]` and `[rpa]` TOML parsing |
| GUI risk rules | `controller/risk_classifier.py` | GUI/RPA tool category classification |
| GUI audit fields | `controller/audit.py` | Extended fields for GUI/RPA operations |
| GUI verifier schema | `controller/verifier.py` | Extended verification for GUI tool calls |

### Dependencies

```
# Terminal TUI
textual>=0.80
rich>=13

# GUI Agent
PyGObject>=3.42           # GLib/D-Bus bindings (AT-SPI access)
pyatspi>=2.46             # AT-SPI Python bindings
Pillow>=10.0              # Screenshot processing

# RPA Bridge (optional, only if [rpa] enabled)
robotframework>=7.0
rpaframework>=28.0        # RPA.Desktop, RPA.Images
```

---

## 12. Execution Plan

### Phase A: Foundation (Terminal TUI + CoT Events)

**Goal:** Split-pane terminal with NL input and chain-of-thought explanation.
No GUI automation yet — just CLI tools through the existing pipeline, but with
full visibility in the right panel.

| Step | Deliverable | Estimate |
|---|---|---|
| A.1 | Textual spike: split-pane layout + PTY embedding proof-of-concept | 2 days |
| A.2 | `CotEvent` dataclass + emitter in `controller/main.py` | 1 day |
| A.3 | `turn.cot` notification in `controller/daemon.py` | 0.5 day |
| A.4 | Input router: `#` prefix, `F2` toggle, `Ctrl+Space` one-shot | 1 day |
| A.5 | Explanation panel: CoT renderer with collapsible sections | 2 days |
| A.6 | HITL integration: approval dialog in left panel, context in right | 1 day |
| A.7 | Config: `[terminal]` TOML section, keymap integration | 0.5 day |
| A.8 | Tests: Textual snapshot tests, daemon integration tests | 2 days |
| **A total** | **Working split-pane NL terminal** | **~10 days** |

### Phase B: GUI Automation (Tier B)

**Goal:** AT-SPI and D-Bus-based GUI automation with screenshot-based COW
preview and full audit trail.

| Step | Deliverable | Estimate |
|---|---|---|
| B.1 | GUI Agent process scaffold + Landlock/Seccomp sandbox profile | 2 days |
| B.2 | AT-SPI client: tree query, element interaction, element verification | 3 days |
| B.3 | Screenshot manager: capture, hash, store, cleanup | 1 day |
| B.4 | GUI tool schemas (`gui.screenshot`, `gui.find_element`, `gui.click`, `gui.type`) | 1 day |
| B.5 | Risk classifier extension: GUI tool categories | 1 day |
| B.6 | Controller integration: GUI Agent dispatch, `turn.gui` events, COW preview | 2 days |
| B.7 | App-specific API: LibreOffice UNO wrapper (proof-of-concept) | 2 days |
| B.8 | Audit extension: GUI-specific fields, screenshot hash logging | 1 day |
| B.9 | Verifier extension: GUI element verification schema | 1 day |
| B.10 | Tests: AT-SPI mock tests, integration tests with a real GTK app | 3 days |
| **B total** | **Working GUI automation with security model** | **~17 days** |

### Phase C: RPA Fallback (Tier C)

**Goal:** Robot Framework bridge as a last-resort execution path with maximum
safety constraints.

| Step | Deliverable | Estimate |
|---|---|---|
| C.1 | RPA Bridge process scaffold + sandbox profile | 1 day |
| C.2 | Workflow generator: Intent → Robot Framework keywords | 2 days |
| C.3 | Image matcher: template matching for element detection | 2 days |
| C.4 | RPA tool schemas (`rpa.execute_workflow`, `rpa.find_by_image`) | 1 day |
| C.5 | Controller integration: escalation logic, `turn.rpa` events | 2 days |
| C.6 | HITL: screenshot-with-crosshair preview, per-step confirmation | 1 day |
| C.7 | Timeout enforcement: hard kill on expiry, cleanup | 1 day |
| C.8 | Audit extension: RPA-specific fields, per-keyword screenshot hashes | 1 day |
| C.9 | Tests: Robot Framework integration tests, timeout tests, escalation tests | 2 days |
| **C total** | **Working RPA fallback with safety constraints** | **~13 days** |

### Phase D: Polish & Hardening

| Step | Deliverable | Estimate |
|---|---|---|
| D.1 | App API registry: Firefox DevTools, GNOME Files, Evince wrappers | 3 days |
| D.2 | Explanation panel: screenshot thumbnail rendering in terminal | 2 days |
| D.3 | Accessibility: screen reader support for GUI/RPA traces | 1 day |
| D.4 | Performance: lazy AT-SPI tree loading, screenshot compression | 1 day |
| D.5 | Security audit: penetration test of GUI Agent sandbox, RPA timeout | 2 days |
| D.6 | Documentation: user guide, security model docs, API reference | 2 days |
| **D total** | **Production-ready release** | **~11 days** |

**Grand total: ~51 days (~10 weeks)**

### Milestones

| Milestone | What ships | Gate |
|---|---|---|
| M-A | Split-pane NL terminal with CLI tools + CoT | All existing 1446 tests pass + 20 new terminal tests |
| M-B | GUI automation (AT-SPI + LibreOffice) | GUI tools work on GNOME + LibreOffice, audit trail complete |
| M-C | RPA fallback | Robot Framework bridge works, timeout enforced, escalation tested |
| M-D | Production release | Security audit passed, docs complete, 3 app APIs working |

---

## 13. Open Questions

1. **PTY embedding maturity.** Is `textual-terminal` production-ready, or do we
   need a custom PTY widget or subprocess-based fallback? Spike in Phase A.1.

2. **Wayland support.** AT-SPI works on both X11 and Wayland, but screenshot
   capture differs. D-Bus `org.freedesktop.portal.Screenshot` is the Wayland
   path. Do we support both from day one?

3. **App API scope.** Which applications get dedicated D-Bus wrappers in Phase D?
   Candidates: LibreOffice (Calc, Impress, Writer), Firefox, GNOME Files,
   Evince. User preference needed.

4. **Voice input.** The user mentioned "a keyboard shortcut to directly speak to
   the terminal." Options: system-level OS dictation, integrated `whisper.cpp`,
   or defer. Recommendation: defer to a follow-up phase.

5. **RPA default state.** Should RPA be enabled by default or opt-in? Current
   recommendation: **opt-in** (`[rpa] enabled = false`) because it requires
   `/dev/uinput` access and is the widest sandbox profile.

6. **Multi-step GUI workflows.** Should "create a presentation with 5 slides"
   be a single turn or 5 turns? Recommendation: the Controller decomposes
   multi-step GUI operations into sequential turns, each individually audited,
   with intermediate screenshot previews.

---

*This document is a proposal for review. No implementation has started.
Feedback and revisions are expected before any code is written.*
