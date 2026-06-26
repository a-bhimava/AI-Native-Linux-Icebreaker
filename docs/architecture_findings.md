# Architecture Findings — Icebreaker AI-Native OS

> Comprehensive audit of the full codebase, with deep dives into the Privileged Brain
> training pipeline and the dual-column terminal TUI. Every finding is verified against
> source code with file paths and line numbers. Nothing is assumed — everything is read.
>
> **Audit date**: June 2025  
> **Scope**: All modules — mcpd, controller, privileged-brain, terminal, GUI, cx-distro  
> **Method**: Exhaustive file-by-file read + cross-module dependency tracing

---

## Severity Definitions

| Level | Meaning |
|---|---|
| **CRITICAL** | Blocks core functionality. Demo-breaking. Must fix before any stakeholder review. |
| **MAJOR** | Significantly degrades quality or breaks a documented feature. Fix before release. |
| **MODERATE** | Reduces polish or creates latent risk. Fix during hardening. |
| **MINOR** | Cosmetic, doc-only, or edge-case. Fix when convenient. |

---

## CRITICAL Findings

### C1: Privileged Brain Training–Runtime Format Mismatch

**The single most important finding in this audit.**

The locally fine-tuned PB model (`run7_cot_q4km.gguf`) was trained on **40,180 NL-to-Bash pairs** where the assistant output is a raw shell command:

```
User: "Show disk usage"
Assistant: "df -h"
```

The training system prompt (`privileged-brain/scripts/process_datasets.py:14-23`) says:
> *"output ONLY the corresponding Bash command or shell pipeline"*

But the Controller at runtime sends a completely different system prompt (`dual-brain/controller/prompts/pb.txt:1-12`):
> *"Output format: {"tool":"<allowed_tool>","params":<params matching tool_schema>}"*

And `_validate_tool_call()` (`dual-brain/controller/main.py:1497-1507`) expects:
```json
{"tool": "fs.write", "params": {"path": "/tmp/x", "content": "hello"}}
```

**What happens**: The fine-tuned model outputs `df -h`. The Controller calls `json.loads("df -h")`, gets `JSONDecodeError`, retries with sampling decay, and eventually raises `PB_SCHEMA_ERROR`. **Every local PB turn fails.**

**Why API backends work**: Gemini/OpenAI/Anthropic are general-purpose models that follow the pb.txt system prompt at inference time without needing SFT training on MCP format.

**The 20 MCP examples are insufficient**: `generate_synthetic_local.py:363-383` contains 20 MCP-format training pairs (0.05% of dataset). These use artificial "via MCP" phrasing and are statistically invisible against 40,160 Bash-format examples.

**The GBNF grammar is not applied**: `mcp_tool_call.gbnf` exists and correctly constrains output to `{"tool":"...","params":{...}}`, but:
- `06_start_inference.sh:93-102` does NOT pass `--grammar-file` to llama-server (variable `$GRAMMAR` is set at line 64 but never used)
- `LlamaCppLocalBackend` (backends.py) comments at line 71: *"PB relies on post-hoc tool-call validation instead"* — grammar deliberately skipped for PB

**Impact**: The entire local PB execution pipeline is non-functional. The ISO cannot demonstrate local AI command execution.

**Fix options** (in order of effort):
1. **Quick**: Enable GBNF grammar for PB at inference time + add ~500 MCP-format training pairs + retrain (2-3 days)
2. **Medium**: Add a "bash-to-MCP adapter" in the Controller that wraps raw bash output into `{"tool":"shell.exec","params":{"command":"df -h"}}` (hours, but reduces security model)
3. **Full**: Retrain PB entirely on MCP-format data with tool schemas in system prompt (1 week)

| Detail | Value |
|---|---|
| Files | `process_datasets.py:14-23`, `prompts/pb.txt:1-12`, `main.py:1497-1507`, `06_start_inference.sh:93-102` |
| Training data | 40,180 Bash / 20 MCP (99.95% / 0.05%) |
| Grammars | `bash_cot.gbnf` (training), `mcp_tool_call.gbnf` (runtime — unused) |

---

### C2: `_check_rpa_progress` Calls Nonexistent Method

`dual-brain/controller/main.py:1757` calls `self._qb.generate(...)`. The `BrainBackend` ABC (`backends/base.py`) defines only `complete()` and `stream_complete()`. There is no `generate()` method on any backend.

**Impact**: Any RPA workflow that reaches the QB-monitored progress check crashes with `AttributeError`. This breaks the entire GUI automation → RPA escalation flow.

**Fix**: Replace `self._qb.generate(...)` with `self._qb.complete(...)` using the same arguments.

| Detail | Value |
|---|---|
| File | `dual-brain/controller/main.py:1757` |
| ABC | `dual-brain/controller/backends/base.py` |

---

### C3: Settings Window Corrupts Config File

`dual-brain/gui/settings/window.py:48-82` serializes config back to TOML using Python's `tomllib` (read-only) and manual string building. The serializer produces flat section headers like `[gemini]` instead of the required `[qb.gemini]` (dotted-key tables).

**Impact**: Saving settings from the GUI overwrites `controller.toml` with syntactically valid but semantically wrong TOML. The daemon fails to parse it on next restart. User loses their configuration.

**Fix**: Use `tomli_w` or `tomlkit` for round-trip TOML serialization that preserves dotted-key structure.

| Detail | Value |
|---|---|
| File | `dual-brain/gui/settings/window.py:48-82` |

---

## MAJOR Findings

### M1: Terminal TUI Receives No Streaming Events

The daemon sends 5 types of streaming notifications during pipeline execution: `turn.cot`, `turn.progress`, `turn.token`, `turn.gui`, `turn.rpa` (`dual-brain/controller/daemon.py:342-396`).

The `DaemonClient` base class receives these via its reader thread (`dual-brain/controller/client.py:127-167`) and dispatches to handler methods. But all handlers are no-ops:
- `_on_cot()` = `pass` (client.py:180)
- `_on_progress()` = `pass` (client.py:171)
- `_on_token()` = `pass` (client.py:174)
- `_on_gui()` = `pass` (client.py:183)
- `_on_rpa()` = `pass` (client.py:186)

`GtkDaemonClient` overrides these for GTK (using `GLib.idle_add`). **No equivalent `TextualDaemonClient` exists** for the terminal TUI.

The `CompanionPanel` (`dual-brain/terminal/companion.py`, 318 lines) has fully implemented handlers for all event types — `handle_cot()`, `handle_gui()`, `handle_rpa()`, `handle_result()` — but `handle_cot`, `handle_gui`, and `handle_rpa` are **never called** from `app.py`.

**Impact**: During NL turns, the companion panel stays completely blank. The user sees no chain-of-thought, no progress indicators, no streaming tokens. Only the final result appears.

**Fix**: Create `TextualDaemonClient(DaemonClient)` (~60 lines) that overrides callbacks to use `app.call_from_thread()`. Pattern directly follows `GtkDaemonClient`.

| Detail | Value |
|---|---|
| Files | `terminal/app.py:138-182`, `terminal/companion.py:101-308`, `controller/client.py:171-186`, `gui/daemon_client.py` (reference implementation) |

---

### M2: Terminal `__main__.py` Has No Socket Connection

`dual-brain/terminal/__main__.py` is 5 lines:
```python
from .app import run
run()
```

No argparse, no `--sock` argument. The `run()` function accepts `daemon_client=None` but it's always called with `None`. The terminal TUI can only be launched without a daemon connection, making every NL command show *"No daemon connection"*.

**Contrast**: The GUI chatbot (`dual-brain/gui/__main__.py`) accepts `--sock` via argparse and constructs `GtkDaemonClient(sock_path)`.

**Fix**: Add argparse with `--sock` and `--config` arguments (~15 lines). Construct `TextualDaemonClient` (from M1 fix) and pass to `run()`.

| Detail | Value |
|---|---|
| File | `dual-brain/terminal/__main__.py:1-5` |
| Reference | `dual-brain/gui/__main__.py` |

---

### M3: `_validate_tool_call` Does Not Validate Params Against Schema

`dual-brain/controller/main.py:1497-1507` checks only:
1. `tool_call.get("tool") == expected_action` (tool name matches)
2. `isinstance(params, dict)` (params is a dict)

It does NOT validate params against the tool's JSON Schema (loaded by `_get_tool_schema()` at line 1509-1517 and sent to PB in the prompt). A PB response with wrong parameter names or types passes the Controller and crashes at mcpd.

**Impact**: Invalid tool params reach mcpd, which either rejects them (wasting a turn) or worse, executes them with missing fields (undefined behavior).

**Fix**: Add `jsonschema.validate(params, tool_schema)` after the existing checks. The `jsonschema` package is already a dependency.

| Detail | Value |
|---|---|
| File | `dual-brain/controller/main.py:1497-1507` |

---

### M4: `fuse_lora.py` Does Not Verify Base Model Hash

`privileged-brain/scripts/fuse_lora.py:44` calls `AutoModelForCausalLM.from_pretrained()` without verifying the base model's SHA-256 against `models/checksums.sha256`. This violates INV-7 (Model Weight Integrity).

**Impact**: If the base model is swapped (accidentally or maliciously), the LoRA adapter merges into a different model silently. The resulting GGUF could have unpredictable behavior.

**Fix**: Add SHA-256 verification of the base model directory before `from_pretrained()`.

| Detail | Value |
|---|---|
| File | `privileged-brain/scripts/fuse_lora.py:44` |
| Invariant | INV-7 |

---

### M5: Ollama Modelfile System Prompt Diverges From Runtime

`privileged-brain/05_convert_and_import.sh:70-83` embeds a Bash-output system prompt in the Ollama Modelfile:
> *"output ONLY the corresponding Bash command"*

When the Controller's `LlamaCppLocalBackend` sends `pb.txt` as the system prompt at inference time, the model has conflicting instructions: training-time system prompt says Bash, runtime system prompt says JSON. The training-time prompt typically wins due to SFT weight dominance.

**Impact**: Even if an adapter layer attempted to reconcile formats, the dual system prompts create confusion. This compounds C1.

| Detail | Value |
|---|---|
| File | `privileged-brain/05_convert_and_import.sh:70-83` |

---

### M6: GUI Daemon Client Has No Reconnection Logic

`dual-brain/gui/daemon_client.py` connects to the daemon socket once at construction. If the daemon restarts (systemd restart, crash recovery), the GUI permanently shows *"Not connected to daemon"* with no retry.

**Impact**: Users must manually restart the GUI app after any daemon restart. In a systemd-managed environment where the daemon might restart on failure, this creates a poor experience.

**Fix**: Add exponential-backoff reconnection in the reader thread when the socket disconnects.

| Detail | Value |
|---|---|
| File | `dual-brain/gui/daemon_client.py` |

---

### M7: CoT Notification Field Name Mismatch (GUI)

`dual-brain/gui/chatbot/window.py:284-288` reads:
```python
step_text = params.get("step", params.get("text", ""))
state = params.get("state", "pending")
```

But the daemon sends notifications with fields `heading` and `step_state` (per `daemon.py:355-360`). The GUI chatbot always gets empty CoT text.

**Impact**: Chain-of-thought steps appear in the chatbot but with blank text.

**Fix**: Update field names to match daemon notification format.

| Detail | Value |
|---|---|
| File | `dual-brain/gui/chatbot/window.py:284-288` |

---

### M8: Audit Log Sidebar Button Is Dead

`dual-brain/gui/chatbot/window.py:122-125` creates an "Audit Log" button in the sidebar but connects no click handler:
```python
audit_btn = Gtk.Button(icon_name="document-open-recent-symbolic")
audit_btn.add_css_class("flat")
audit_btn.set_tooltip_text("Audit Log")
sidebar.append(audit_btn)  # no .connect()
```

**Impact**: Users click the Audit Log button and nothing happens. The audit viewer window (`dual-brain/gui/audit/`) exists but is unreachable from the chatbot sidebar.

**Fix**: Add `.connect("clicked", self._on_open_audit)` and implement the handler (same pattern as settings button at line 119).

| Detail | Value |
|---|---|
| File | `dual-brain/gui/chatbot/window.py:122-125` |

---

## MODERATE Findings

### D1: Two Desynchronized InputBar Instances

`dual-brain/gui/chatbot/window.py:65-68` creates `self._input_bar` for the welcome view, and line 194 creates `self._chat_input_bar` for the chat view. Both have independent `set_busy()` state. If the welcome bar sends a message and the chat view appears, the welcome bar's busy state is set but invisible. On clear, both are reset (line 308-309), but during a turn, switching views could show stale state.

**Impact**: Minor UX inconsistency. Busy indicator might not appear in edge cases.

| Detail | Value |
|---|---|
| File | `dual-brain/gui/chatbot/window.py:65-68, 194` |

---

### D2: Eval Pipeline Only Measures Bash Accuracy

`privileged-brain/07_evaluate.sh` and `eval_feh.py` evaluate the model's ability to generate correct Bash commands. There is no automated test for MCP tool call generation accuracy.

**Impact**: There's no way to measure whether C1 fixes actually work without manual testing.

**Fix**: Add an MCP-format evaluation dataset and a corresponding eval script that tests JSON structure, tool name validity, and param schema compliance.

| Detail | Value |
|---|---|
| Files | `privileged-brain/07_evaluate.sh`, `privileged-brain/scripts/eval_feh.py` |

---

### D3: AiTerminalPresenter Callbacks Never Set

`dual-brain/terminal/presenter.py:40-46` has `set_callbacks()` for HITL prompt rendering (show approval dialog, apply lockout timer). This method is never called from `app.py`. The presenter is registered in the presenter registry but never wired to the TUI widgets.

**Impact**: HITL approval prompts in the terminal TUI fall back to raw terminal I/O instead of rendering in the Textual UI.

| Detail | Value |
|---|---|
| File | `dual-brain/terminal/presenter.py:40-46`, `terminal/app.py` |

---

### D4: Distro Config Defaults to Gemini (Cloud)

`cx-distro/distro/controller.toml:5` sets `backend = "gemini"` for the QB. This means the ISO requires internet access and a Gemini API key for basic operation.

**Impact**: Contradicts the whitepaper's "locally-running LLM" story. True offline operation requires changing QB backend to `local` and running a local llama-server.

**Mitigation**: This is intentional for Phase 5-6 demos (Gemini provides better QB quality). Document the tradeoff. Phase 7 should add a local QB option.

| Detail | Value |
|---|---|
| File | `cx-distro/distro/controller.toml:5` |

---

### D5: GBNF Grammar Does Not Constrain Tool Names

`privileged-brain/inference/grammar/mcp_tool_call.gbnf` constrains output to valid JSON with `"tool"` and `"params"` keys, but the tool name value accepts any string. A model could output `{"tool":"rm_rf_everything","params":{}}` and pass grammar validation.

**Impact**: Grammar-constrained decoding alone is insufficient to guarantee valid tool calls. Post-hoc validation (M3) must also check tool name against the allowed list.

| Detail | Value |
|---|---|
| File | `privileged-brain/inference/grammar/mcp_tool_call.gbnf:1-26` |

---

## MINOR Findings

### E1: MCP Training Examples Use Artificial Phrasing

The 20 MCP-format training examples in `generate_synthetic_local.py:363-383` use prompts like *"Restart the nginx service via MCP tool"*. Real users would never say "via MCP tool." The model learns to associate MCP format with this unnatural marker.

**Fix**: If adding more MCP training data (per C1 fix), use natural phrasing identical to the Bash-format prompts.

---

### E2: No `REFUSE:` Prefix Handling in Controller

The PB model was trained to output `REFUSE: <reason>` for dangerous commands. The Controller's `_validate_tool_call()` does not check for this prefix. A REFUSE response would fail JSON parsing and be treated as a schema error rather than an intentional refusal.

**Fix**: Add a `REFUSE:` prefix check before JSON parsing in the PB response handler.

---

### E3: Terminal execution.py Has No PTY

`dual-brain/terminal/execution.py` uses `asyncio.create_subprocess_exec` without a PTY. Interactive programs (`vim`, `top`, `htop`) won't render correctly.

**Mitigation**: Acceptable for MVP. The whitepaper doesn't promise full interactive terminal emulation. Document as a known limitation.

---

### E4: `generate_synthetic_local.py` MCP Pairs Have Wrong Tool Names

The 20 MCP-format pairs reference tools like `service.restart`, `system.disk_usage`, `package.update` — tool names that may not match actual mcpd tool schemas. If these tools don't exist in mcpd's catalogue, the training data teaches the model to generate invalid tool calls.

**Fix**: Verify MCP training tool names against `mcpd tools/list` output. Use only real tool names.

---

### E5: `05_convert_and_import.sh` Has Two System Prompts

The script embeds a Bash-output system prompt in the Ollama Modelfile (line 70-83) and also references the `SYSTEM` prompt from training data. If the model is served via Ollama, it receives the Modelfile system prompt; if served via llama.cpp, it receives whatever the Controller sends. These can diverge silently.

---

## Security Invariant Verification

| Invariant | Status | Evidence |
|---|---|---|
| **INV-1: Brain Isolation** | IMPLEMENTED | QB has zero MCP connections. PB receives only opaque intent IDs via Controller. No raw user text reaches PB. |
| **INV-2: Controller Schema Enforcement** | IMPLEMENTED | `_validate_intent()` rejects unknown fields, checks for shell metacharacters, passes only UUIDs to PB. |
| **INV-3: mcpd Network Isolation** | IMPLEMENTED | mcpd uses stdio pipes only. CI gate G1 asserts `ss -tlnp` shows zero mcpd listeners. |
| **INV-4: Parameter Validation** | PARTIAL | JSON Schema validation occurs for intent objects but NOT for PB-generated tool call params (see M3). |
| **INV-5: No Execution Without Sandboxing** | IMPLEMENTED | Landlock applied before fork. Seccomp-BPF applied in child before execve. Kernel < 5.13 check exits cleanly. |
| **INV-6: COW Before Destructive Ops** | PARTIAL | COW dry-run implemented in mcpd. 3-second lockout implemented in HITL presenter. GUI HITL lockout not independently verified. |
| **INV-7: Model Weight Integrity** | PARTIAL | `start-pbd` and `start-qbd` verify checksums. `build.sh` verifies. `fuse_lora.py` does NOT verify base model hash (M4). |
| **INV-8: Audit Log Integrity** | IMPLEMENTED | O_APPEND, fsync per line, hash-chain entries. Audit log not writable by model processes. |

---

## What Works Well

These areas are production-quality and should be preserved:

1. **Training data pipeline engineering** — `process_datasets.py` has 20+ quality filters in `is_valid()` catching prose contamination, unicode dashes, dangerous commands, echo-wrapped artifacts, and encoding issues. This is thorough.

2. **DPO preference data** — 95+ hand-crafted pairs teaching safe vs dangerous command preference. High quality.

3. **Terminal TCSS styling** — 210 lines with the project's design tokens (`#e78952` primary, `#5e8787` secondary, `#111112` bg), CoT card states, `$NO_COLOR` fallback, responsive companion auto-hide. Production-quality.

4. **CompanionPanel implementation** — 318 lines of real, functional code: CoT cards with state transitions, GUI event rendering, RPA progress tracking, result interpretation. Needs wiring, not rewriting.

5. **Controller streaming pipeline** — `daemon.py` correctly serializes all 8 event types as JSON-RPC notifications. End-to-end wiring exists from pipeline through daemon to socket. Only the terminal client endpoint is missing.

6. **mcpd Rust security** — Landlock, seccomp-BPF, COW, stdio-only transport, path validation. Solid kernel-level sandboxing.

7. **Audit system** — O_APPEND + fsync + hash-chain. Records all intents including rejected ones. Tamper-evident.

8. **GtkDaemonClient** — Clean thread-safety pattern using `GLib.idle_add`. Direct template for the missing `TextualDaemonClient`.

---

## Priority Fix Order

For a stakeholder demo or review, fix in this order:

| Priority | Finding | Effort | Impact |
|---|---|---|---|
| 1 | **C1**: PB format mismatch | 2-3 days (retrain) or hours (adapter) | Unblocks local PB demo |
| 2 | **C2**: `generate()` → `complete()` | 5 minutes | Unblocks RPA flows |
| 3 | **M1 + M2**: Terminal streaming + socket | 4-6 hours | CompanionPanel comes alive |
| 4 | **M7**: CoT field names | 15 minutes | GUI shows CoT text |
| 5 | **M3**: Param schema validation | 1 hour | Prevents invalid tool calls reaching mcpd |
| 6 | **M8**: Audit Log button | 10 minutes | Dead button fixed |
| 7 | **C3**: Settings TOML corruption | 2-3 hours | Settings save works |
| 8 | **M6**: Reconnection logic | 2 hours | GUI survives daemon restart |
| 9 | **D2**: MCP eval pipeline | 1 day | Automated quality gate for C1 fix |
| 10 | **M4**: fuse_lora checksum | 30 minutes | INV-7 compliance |

---

## Cross-Module Dependency Map

```
User Input
    │
    ▼
┌─────────────────────┐
│  Quarantined Brain   │◄── Gemini API (cloud) or local llama-server
│  (intent parsing)    │    Config: controller.toml [qb] section
└─────────┬───────────┘
          │ Intent Object (validated JSON)
          ▼
┌─────────────────────┐
│  Controller          │◄── prompts/pb.txt (system prompt)
│  (orchestration)     │◄── schemas/ (tool JSON schemas)
│                      │◄── risk classifier, trust store
└─────────┬───────────┘
          │ Tool call JSON {"tool":"...","params":{...}}
          ▼                           ▲
┌─────────────────────┐    ┌─────────┴──────────┐
│  Privileged Brain    │    │  ⚠ FORMAT MISMATCH │
│  (command gen)       │───▶│  Model outputs Bash│
│                      │    │  Controller wants  │
│  Local: run7 GGUF   │    │  JSON tool calls   │
│  API: Gemini/OpenAI  │    └────────────────────┘
└─────────┬───────────┘
          │ MCP JSON-RPC
          ▼
┌─────────────────────┐
│  mcpd                │◄── Landlock + seccomp + COW
│  (execution)         │    stdio pipes only (INV-3)
└─────────┬───────────┘
          │ Result
          ▼
┌─────────────────────┐    ┌──────────────────────┐
│  Daemon              │───▶│  Streaming events     │
│  (AF_UNIX socket)    │    │  cot/progress/token/  │
│                      │    │  gui/rpa              │
└─────────┬───────────┘    └──────────┬───────────┘
          │                           │
    ┌─────┴─────┐              ┌──────┴──────┐
    ▼           ▼              ▼             ▼
┌────────┐ ┌────────┐   ┌──────────┐  ┌──────────┐
│GTK GUI │ │Terminal │   │GtkDaemon │  │⚠ MISSING │
│Chatbot │ │TUI     │   │Client    │  │TextualDmn│
│        │ │        │   │(works)   │  │Client    │
└────────┘ └────────┘   └──────────┘  └──────────┘
```

---

*This document should be updated as findings are resolved. Reference commit hashes when fixes land.*
