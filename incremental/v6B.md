# V6B — Context-Aware UX + Complete Execution Path

> Continuation of V6. Same version marker on the ISO — this document is the
> plan for the second wave of V6 code changes that finish the "list files"
> demo end-to-end and make the AI context-aware.

## Context

V6 automated gates all pass (29/29) and the pipeline runs end-to-end on real
Apple Silicon UTM boots. Live testing surfaced three real design gaps that
block a clean demo:

| # | Gap | Where it bites | Live-patch status |
|---|-----|-----|-----|
| 1 | **PB target-blindness (F-27)** | PB was invoked with only `intent_id + allowed_tool + tool_schema` (session.py:69–74). No target → PB invented `/tmp` from training bias → verifier refused | Live-patched on the running guest ✓; not yet in source |
| 2 | **mcpd path denial (F-28)** | With the F-27 fix applied, `fs.list /home/icebreaker` reaches mcpd, which returns `Permission denied (os error 13)`. Cause: `default_roots()` in `src/mcpd/src/tools/fs.rs:32,64-79` = `[/proc, /sys, /tmp, /var/log, /etc]` + `$HOME`. mcpd is spawned with the wrong `HOME`, or the scrubbed env drops it | Not patched |
| 3 | **QB context-blindness (UX)** | QB receives only raw user text. No cwd, no shell history, no active window. Every ambiguous phrase ("here", "this folder", "the file I was editing") fails or requires a concrete path | Not patched |

**Goal:** a user in `/home/icebreaker` typing `# list files here` gets a real
listing. Every layer contributes correctly: Terminal collects context →
daemon forwards → QB resolves ambiguity → PB uses target → mcpd permits →
result.

## Phase 1 findings

- **mcpd (Rust):** `src/mcpd/src/tools/fs.rs:64-79` `default_roots()` builds
  from `STATIC_ROOTS` (`/proc /sys /tmp /var/log /etc`) + `home()` (reads
  `$HOME`). Test-only `MCPD_FS_TEST_ROOTS` env is compiled out of production.
  No CLI args (`main.rs`).
- **mcpd spawn env:** `dual-brain/controller/mcpd_client.py:56-81`
  `_scrubbed_env()` inherits only `{PATH, HOME, USER, LOGNAME, SHELL, LC_*,
  LANG, TZ, TERM, COLORTERM}` from the controller. `HOME` DOES pass through —
  needs verification that it's `/home/icebreaker` at runtime.
- **QB call sites:** `dual-brain/controller/main.py:238-242` (streaming) and
  `1109-1114` (non-streaming) both call `qb.complete(system=qb_system,
  user=user_input)` — raw text only, no environmental context.
- **QB prompts:** `dual-brain/controller/prompts/qb_{gemini,anthropic,openai,
  local}.txt` — all stateless, 18–21 lines, describe JSON schema + action
  catalogue + security rules. Zero context directives.
- **Intent schema:** `dual-brain/controller/schemas/intent.json:1-59` —
  `target` is a free string (max 512 chars) with pattern
  `[^;&|` `` ` ``$<>\x00-\x1f]*`. Empty string allowed. No format constraint.
- **Session history:** `SessionState._qb_messages` tracks prior turns (user
  + assistant JSON + tool result summaries). No cwd/window fields.
- **Terminal:** `dual-brain/terminal/execution.py:46-88` spawns `$SHELL -c
  command` in a scrubbed env. Does NOT capture cwd across commands.

## The seven changes

### A. Fix mcpd path allowlist so `/home/icebreaker` reads work (F-28)

Two layers:

- **A1 fast — verify + set HOME correctly.** Confirm the running controller
  daemon's `HOME` is `/home/icebreaker`. If not, set
  `Environment=HOME=/home/icebreaker` in `icebreaker-controller.service`.
- **A2 structural — config-driven read roots.** Even with HOME right, some
  ops (browse `/etc/icebreaker`, read `/opt/icebreaker`) will hit the same
  wall. Add:
  - `MCPD_FS_READ_ROOTS` env var (**not** feature-gated — production-safe,
    colon-separated).
  - `src/mcpd/src/tools/fs.rs` `default_roots()`: merge STATIC + `$HOME` +
    `MCPD_FS_READ_ROOTS`.
  - `dual-brain/controller/mcpd_client.py`: add `MCPD_FS_READ_ROOTS` to
    `_SAFE_ENV_VARS`, populate from `controller.toml [mcpd.fs]
    read_roots = [...]`.
  - `cx-distro/distro/controller.toml`: default
    `read_roots = ["/home/icebreaker"]`.

Files:
- `src/mcpd/src/tools/fs.rs:64-79`
- `dual-brain/controller/mcpd_client.py:56-81`
- `dual-brain/controller/config.py`
- `cx-distro/distro/controller.toml`
- `dual-brain/controller/schemas/controller_config.json`

### B. Bake the F-27 PB target fix into source

Already live-patched on the guest; needs to land as source so future builds
carry it:

- `dual-brain/controller/session.py:69-74` — extend
  `build_pb_user_turn(intent_id, allowed_tool, tool_schema, target: str = "")`
  → include target in JSON when non-empty.
- `dual-brain/controller/main.py:449` — call site:
  `session.build_pb_user_turn(intent_id, intent["action"], tool_schema,
  target=intent.get("target",""))`.

Update the docstring: per **INV-1**, the validated `target` (schema-filtered,
no metacharacters) is part of the structured Intent Object that flows to PB.
Only raw user text is forbidden.

### C. Terminal captures shell state → sends to daemon (context source)

Terminal is where cwd lives. It has to capture it and forward.

- `dual-brain/terminal/execution.py`: after every shell command, capture
  `pwd` via a lightweight helper or by tracking `cd` mutations. Maintain a
  bounded ring of last 5 commands. Store in a `ShellContext` instance.
- `dual-brain/terminal/app.py`: on `run_turn`, include `ShellContext` in the
  RPC params.
- `dual-brain/controller/daemon.py` `_handle_turn_run`: accept optional
  `context` param; pass into `SessionState` for the current turn.
- `dual-brain/controller/session.py`: new `ShellContext` dataclass:

```python
@dataclass
class ShellContext:
    cwd: str = ""
    recent_commands: list[str] = field(default_factory=list)  # last 5
    user: str = ""
    active_window: str = ""    # optional, may be empty
    hostname: str = ""
```

### D. Controller injects context into QB prompt (preamble pattern)

Preamble over separate JSON field: no schema break, no retraining, matches
existing `[Tool output summary]:` pattern (session.py:63).

At the QB call site (`main.py:238-242`), build a preamble:

```
<context>
cwd: /home/icebreaker
user: icebreaker
recent_commands:
  - cd
  - ls
  - vim notes.txt
active_window: GNOME Terminal
</context>

<query>
list files here
</query>
```

Then `qb.complete(system=qb_system, user=<preamble+query>)`.

Guardrails: preamble is built by the Controller from validated
`ShellContext` — user cannot inject `<context>` because the Terminal wraps
their raw input in `<query>` after context. XML-tag style deters models from
confusing it with instructions.

### E. Redraft QB system prompts to use context

`dual-brain/controller/prompts/qb_{gemini,anthropic,openai,local}.txt` —
add a **Context Usage** section. Draft (Gemini variant):

```
# CONTEXT

Before each user query, you will receive a <context> block with the user's
current shell state:

  cwd:             the user's current working directory
  user:            the user's Linux username
  recent_commands: the last few shell commands they ran
  active_window:   the currently focused application title (best-effort)

USE THIS CONTEXT to resolve ambiguous references in the query:

  "here", "this folder", "current dir"       → target = cwd
  "home", "my home directory", "~"           → target = /home/{user}
  "the file I was editing"                   → look at recent_commands for
                                               editor invocations (vim, nano)
                                               and extract the filename
  relative paths (e.g. "notes.txt")          → resolve against cwd

RULES:

- The target field MUST be a concrete absolute path when the query implies one.
- Never emit "-", "?", "TBD", or any placeholder in target. If genuinely
  ambiguous, use risk_level="read_only", action="fs.list", target=cwd — the
  user probably wants to see what's around.
- Prefer read-only actions (fs.list, fs.read) when the query is exploratory.
- Never emit shell metacharacters in target.

# EXAMPLES

<context>
cwd: /home/icebreaker/Documents
user: icebreaker
recent_commands: [ls, cd Documents]
</context>
<query>list files here</query>

→ {"action":"fs.list","target":"/home/icebreaker/Documents",
   "reason":"user_requested","risk_level":"read_only", ...}

<context>
cwd: /home/icebreaker
user: icebreaker
recent_commands: [vim notes.txt, cat notes.txt]
</context>
<query>show me the file I was editing</query>

→ {"action":"fs.read","target":"/home/icebreaker/notes.txt",
   "reason":"user_requested","risk_level":"read_only", ...}

<context>
cwd: /home/icebreaker
user: icebreaker
recent_commands: [ps aux]
</context>
<query>what processes are running</query>

→ {"action":"process.list","target":"","reason":"user_requested",
   "risk_level":"read_only", ...}
```

Same content adapted to each variant's style (XML tags for Anthropic/OpenAI,
condensed inline for local).

### F. (Optional this pass) Active window title collection

The user asked for open-apps context. Two options:

- **Fast:** `wmctrl -l` from `execution.py` — dependency `wmctrl` (~20 KB).
  Grep the focused window. Best-effort; empty string on failure.
- **Right:** reuse existing `dual-brain/gui_agent/*` AT-SPI wiring which
  already tracks the focused window for the RPA path. Add a small
  `get_focused_window_title()` helper.

Recommend **fast (wmctrl)** for V6B — the GUI Agent hookup adds cross-module
surface. AT-SPI can come in V7 alongside the chatbot.

### G. Gate additions

- **Smoke L6:** assert `[mcpd.fs]` and `read_roots = [...]` present in
  shipped `/etc/icebreaker/controller.toml`.
- **QEMU L6 behavioral** (with-key half moves from UTM-only to CI once a
  key is provisioned as a build secret): currently no live key in CI, so
  this stays a UTM checklist item — but the L6 gate can add:
  `sudo -u icebreaker HOME=/home/icebreaker /opt/icebreaker/venv/bin/python3
  -m controller.mcpd_smoke fs.list /home/icebreaker` → success.

## Build order

1. **Stage 1 — Unblock V6 (A + B).** Bake F-27 target-passing; fix mcpd
   `/home/icebreaker` permission. Rebuild v6.iso, gate, UTM verify: `# list
   files in /home/icebreaker` produces a real file listing. Mark V6 GREEN.
2. **Stage 2 — Context-aware UX (C + D + E).** Add ShellContext, wire
   terminal → daemon → controller → QB preamble; redraft prompts. Rebuild,
   gate, UTM verify: `# list files here` (no path) works.
3. **Stage 3 — Polish (F + G).** Active window via wmctrl; gate additions;
   final rebuild.

## Files touched

| File | Deliverable | Nature |
|------|-------------|--------|
| `incremental/v6B.md` | — | NEW (this doc) |
| `src/mcpd/src/tools/fs.rs` | A | env-var root merge (~10 lines) |
| `dual-brain/controller/mcpd_client.py` | A | add `MCPD_FS_READ_ROOTS` to safe env; populate from config |
| `dual-brain/controller/config.py` | A | `[mcpd.fs] read_roots` parser |
| `dual-brain/controller/schemas/controller_config.json` | A | schema key |
| `cx-distro/distro/controller.toml` | A | default `read_roots = ["/home/icebreaker"]` |
| `dual-brain/controller/session.py` | B, C, D | `build_pb_user_turn(target=…)`; `ShellContext`; `build_qb_user_turn(context, query)` |
| `dual-brain/controller/main.py` | B, D | PB call site passes target; QB call site builds preamble |
| `dual-brain/controller/daemon.py` | C | accept `context` in `turn.run` params |
| `dual-brain/terminal/execution.py` | C, F | capture cwd + recent cmds + (optional) wmctrl window title |
| `dual-brain/terminal/app.py` | C | forward `ShellContext` on `run_turn` |
| `dual-brain/controller/prompts/qb_gemini.txt` | E | Context section + 3 examples |
| `dual-brain/controller/prompts/qb_anthropic.txt` | E | same, XML-tag style |
| `dual-brain/controller/prompts/qb_openai.txt` | E | same |
| `dual-brain/controller/prompts/qb_local.txt` | E | same, condensed |
| `incremental/build/smoke-gate.sh` | G | `read_roots` assertion |
| `incremental/tests/qemu-gate.sh` | G | (deferred until CI has a key) |

## Verification (acceptance criteria)

**Stage 1 done when:**
- Live UTM `# list files in /home/icebreaker` → Tool Execution ✓ → file
  list printed
- CoT ends with a real Result card, not tool_error
- Adversarial `# delete everything` still refused at classifier tier
- V6 GREEN on the status board

**Stage 2 done when, in a fresh boot:**
- `# list files here` → CoT shows `target=/home/icebreaker` (resolved from
  cwd), fs.list succeeds
- `# what's in this folder` → same
- After `cd Documents` in shell, `# list files here` →
  target=/home/icebreaker/Documents
- Ambiguous `# show me the file I was editing` after `vim notes.txt` →
  target=/home/icebreaker/notes.txt

**Stage 3 done when:**
- Smoke gate fails a build that ships an ISO without `read_roots` configured
- (Optional) `# open the current window's PDF` sort of query resolves via
  active_window title

## Non-goals

- No PB retraining (E-1..E-5 items) — accepting current model quirks
- No chatbot GUI — V7 territory
- No arm64 llama-server — separate track, unblocks Rosetta speed;
  suggest V6.5
- No mcpd write-roots expansion — read only for V6B; write paths
  (`fs.write`, `fs.delete`) stay narrow
