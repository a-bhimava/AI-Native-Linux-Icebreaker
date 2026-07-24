# v6.x_OC — Implementation Plan

**Status**: Revision 2 (2026-07-24) — see §0 for deltas from Revision 1 (2026-07-22). Authoritative in-tree doc.

**Naming**: `_OC` suffix means OpenCode-powered. Every filename, config key, systemd unit, GROUND_TRUTH row, and failure ID with an OC-specific meaning carries the suffix so `git grep _OC` returns exactly the OpenCode edition's surface.

---

## 0. Revision 2 deltas (2026-07-24)

Applied after the v6.12 hotfix cycle surfaced new lessons, opencode research corrected an assumption, and an agent council extended the plan with UX + update + extensibility perspectives. **Read this section first**; the body of the doc below is the original Revision 1 design with these deltas overlaid.

**D-R2-1 opencode is TypeScript on Bun, not Go** (revises §6 L.2). Cross-compile via `bun build --compile --target=bun-linux-{amd64,arm64}` producing single-file executables (~100-150MB each). No Bun runtime install needed on the guest. Fallback: ship Bun runtime (~50MB) + opencode source if compile misbehaves under Landlock. Preflight (D-R2-3) determines A vs B.

**D-R2-2 Post-v6.12-hotfix defenses** (new §2.6). Four patterns learned from the v6.12 F-92_OC → F-97_OC cycle must apply to every OC change:
- **Config-chain integrity** (F-95_OC precedent): every new `[qb.opencode]` field gets a boot-time self-test that reads the config value and threads it to the leaf function. Missing = fail boot.
- **File permission self-heal** (F-96_OC precedent): `_hdiag`-style loggers chmod 0666 on first-write, fall back to `/tmp/<name>-<uid>.log` per-user on PermissionError.
- **Every knob needs a schema entry** (F-97_OC precedent): every field in `[qb.opencode]`, `[qb.opencode.provider.*]`, `[audit_summarizer]` lands with a JSON-Schema entry with `type`, `minimum`, `maximum`. Config validator refuses invalid values loudly.
- **HITL-modal key routing works as-is** (v8 code — on_key handles keys directly, bypassing Textual BINDINGS; AUTO_FOCUS on invisible anchor Button). No changes for OC.

**D-R2-3 Preflight before design-lock**. Before writing OpencodeBackend, run a 60-min local prototype of `opencode serve` HTTP mode + `/session/x/message` POST + `/event` SSE + per-request model override. Decision gate: HTTP mode green → proceed as designed. HTTP mode broken → pivot Fix K to CLI-subprocess pattern (fork per turn, stdin/stdout JSON). Per-request override broken → hot-swap becomes per-session (respawn opencode with `--model`).

**D-R2-4 Rollout ordering revised** (revises §11). Correct sequence:
1. v6.13 (current) baking on GCP VM now (`v613arm` tmux session).
2. When v6.13 ISOs land + UTM-verify → tag `v6.13`.
3. Branch `feat/v6.13_OC` from v6.13 tag.
4. Fix M (§2.5) is first commit on that branch; backport to `hotfix/v6.13.1` for current-edition point release.
5. Delta 3 preflight validates opencode HTTP mode.
6. Then Fix K + L + Delta 6 UX v1 + Delta 7 update-check foundation.

**D-R2-5 Ollama deferred entirely to v6.14_OC — supersedes D2**. v6.13_OC ships cloud-only (Anthropic / OpenAI / Google). Keeps ISO lean (~4GB same as current), keeps scope focused on validating opencode as a planner. D3 first-boot card loses "Skip → local-only via Ollama" language.

**D-R2-6 UX layer (new §4.4)** — from user-flow agent council. v6.13_OC v1 ships:
- **6.1** Terminal status bar with model + cost badge (updates per ResultEvent, `Ctrl+M` opens model-picker overlay).
- **6.2** QbPrewarmingEvent handling — "Warming up opencode + Claude Opus … ready in ~7s" spinner in CoT panel.
- **6.3a** OC Models tab (reuses existing `models_page.py`, reads `qb_oc.json` whitelist).
- **6.3b** API Keys tab split from `keys_page.py` — per-provider paste + Verify.
- **6.4** First-boot onboarding card (one-shot, marker `~/.local/state/icebreaker/first-run-oc.done`).

Deferred to v6.13_OC.1 point-release:
- **6.3c** Costs tab (daily/weekly/monthly rollup + per-model bar chart).
- **6.5** Prescriptive error-card mapping expansion (opencode_prewarming, provider_auth_failed, etc.).

**D-R2-7 Update mechanism foundation (new §7.5)** — SoTA research picked a hybrid:
- **Minisign** for component signing (ed25519, single public key in ISO at `/usr/share/icebreaker/minisign.pub`, verified at boot via GRUB-baked SHA).
- **systemd-sysupdate** for immutable squashfs root (A/B partitions, native rollback) — deferred to v6.14_OC.
- **Component overlay updates** for `/opt/icebreaker/` + `/etc/icebreaker/` (Chrome-style binary deltas for mcpd/opencode, full replacement for Python).
- **HITL-gated apply** — Control Center Updates tab with 3s INV-6 lockout on Install button, atomic symlink swap, auto-rollback on daemon restart failure.
- **TLS cert pinning** to `releases.icebreaker.sh`.

v6.13_OC ships the CHECK+DISPLAY layer only (`update_checker.py`, weekly systemd timer, Updates tab with disabled Install button). The APPLY happy path lands in v6.14_OC.

**D-R2-8 Explicit non-goals for v6.13_OC** (revises §2):
- No external (user-installed) MCP server integration. Icebreaker's mcpd stays the closed set of Rust-native tools. External MCP allowlist infra is Phase 7 M7.1–M7.2.
- No RPA workflow authoring UI. `rpa.execute_workflow` continues to invoke pre-defined Robot Framework workflows only. Authoring is Phase 8 territory.
- No actual "Install v6.14" path in v6.13_OC. v6.13_OC ships CHECK only.
- No Ollama in-ISO (D-R2-5 supersedes D2).

---

## 1. Context

v6.12 is built and shipping (arm64 + amd64 ISOs downloaded, UTM verification in progress). The user wants a **more capable variant** shipped alongside v6.12's proven shape:

- **Current edition** (v6.13-*): the Gemini-Flash one-shot QB shape from v6.12. One backend, one code path, predictable behavior. Bug-fix track.
- **OC edition** (v6.13_OC-*): the Quarantined Brain runs as an autonomous agent (sst/opencode) that plans, reflects, retries, and picks its own LLM at runtime (Cursor-style hot swap between Claude / GPT / local Ollama). Adds capability without touching v6.12's proven envelope.

Both editions preserve every existing security invariant (INV-1..INV-8). Research (in `RESEARCH_NOTES_2026-07-22.md`) confirmed sst/opencode can be locked to "plan-only, zero tool execution" so the Icebreaker Controller stays authoritative and mcpd remains the sole tool dispatcher.

---

## 2. Non-goals

- Not touching PB (Privileged Brain / local Qwen). PB stays as it is in both editions.
- Not changing mcpd, the sandbox, HITL, or audit — Icebreaker's safety envelope is unchanged.
- Not shipping opencode's built-in tool execution or bash — opencode is a **planner** only, not an executor.
- Not integrating opencode's MCP — Icebreaker's Controller + mcpd own tool dispatch, opencode's MCP layer is disabled.
- **No external (user-installed) MCP server integration** (D-R2-8). Icebreaker's mcpd remains the closed set of Rust-native tools it is in v6.12. External MCP allowlist infrastructure is Phase 7 M7.1–M7.2 territory.
- **No RPA workflow authoring UI** (D-R2-8). `rpa.execute_workflow` continues to invoke pre-defined Robot Framework workflows only. Authoring surface is Phase 8.
- **No actual "Install v6.14" apply path in v6.13_OC** (D-R2-7 scope). v6.13_OC ships the update CHECK layer only; the full staging + delta-apply + rollback happy path lands in v6.14_OC.
- **No Ollama in-ISO in v6.13_OC** (D-R2-5 supersedes D2). Cloud APIs only. Ollama offline path deferred to v6.14_OC.
- **No Delta 6 v2 items** (Costs tab, expanded prescriptive-error-card mapping) — deferred to v6.13_OC.1 point-release.

## 2.5 In-scope carryover from v6.12 findings

Live UTM Stage E on v6.12 surfaced a defect that both editions must fix (v6.12's Fix I+ mitigated the daemon-death symptom but not the underlying subprocess crash — verified via apport dump 2026-07-22): every `# take a screenshot`, `# list my open windows`, etc. spawns a subprocess that crashes with SIGTRAP inside `libglib-2.0` because it inherits the systemd unit's root-only environment (no `DBUS_SESSION_BUS_ADDRESS`, no `DISPLAY`, no `XDG_RUNTIME_DIR`). pyatspi tries to reach the session AT-SPI registry, fails to find the session bus, aborts.

**Fix M — persistent GUI worker daemon under user session (v6.13_OC Step 1)**. Replaces the per-call subprocess pattern (v6.12 Fix I+) with a long-running unit that owns its own GLib mainloop and inherits the user's session env:

- **New user-scope systemd unit**: `~/.config/systemd/user/icebreaker-gui-worker.service` (NOT system-scope — user-scope so it inherits `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` automatically from the login session). systemd `Type=notify` + `ExecStart=/opt/icebreaker/venv/bin/python3 -m controller.gui_worker_daemon`.
- **New IPC**: `/run/user/$UID/icebreaker-gui-worker.sock` — UNIX socket, JSON request/reply protocol identical in shape to the current `controller.gui_worker` stdin/stdout envelope. Root daemon (mcpd_dispatcher_node) connects via socket + `SO_PEERCRED` check on connect (only the icebreaker user + the icebreaker-controller group can call in).
- **New module `dual-brain/controller/gui_worker_daemon.py`** (~120 LOC): initializes GLib mainloop once on start, listens on the socket, dispatches each request through `GuiAgent.handle_request`, replies with the same envelope shape. Reuses `gui_worker.py`'s existing `_dispatch(...)` function; only the outer loop changes.
- **Modified `agent_graph_nodes.py::_dispatch_in_subprocess`** → renamed to `_dispatch_via_worker_socket`. Instead of `subprocess.run([sys.executable, "-m", "controller.gui_worker"], ...)`, connects to `/run/user/$UID/icebreaker-gui-worker.sock` and sends the same envelope. Falls back to the old subprocess path (with `apport-noui` silencing recommended) if the socket doesn't exist — so the code path degrades gracefully on hosts where the user unit didn't start.
- **First-boot hook**: `/usr/libexec/icebreaker/gui-worker-firstboot.sh` runs once per user login, calls `systemctl --user enable --now icebreaker-gui-worker.service`. Registered via XDG autostart (`~/.config/autostart/icebreaker-gui-worker-enable.desktop`).
- **Landlock + Seccomp**: user-scope unit gets its own profile — allows reading AT-SPI D-Bus paths, denies filesystem writes outside `~/.local/state/icebreaker/`, denies network.
- **Latency win**: subprocess spawn was ~200ms per gui.* call; socket call is ~5ms. Also removes apport dialog spam since the process doesn't die per call.
- **Failure recovery**: if the worker daemon dies (rare — proper GLib init should prevent it), systemd restarts it (`Restart=on-failure`, `RestartSec=2s`). The Controller's `_dispatch_via_worker_socket` retries once on connection refused before surfacing an error to the user.

Files:
- NEW `dual-brain/controller/gui_worker_daemon.py` (~120 LOC).
- NEW `cx-distro/distro/systemd/user/icebreaker-gui-worker.service` (installed under `~/.config/systemd/user/` on first login).
- NEW `cx-distro/libexec/icebreaker/gui-worker-firstboot.sh`.
- NEW `cx-distro/distro/etc/xdg/autostart/icebreaker-gui-worker-enable.desktop`.
- Modified `dual-brain/controller/gui_worker.py` — keep as fallback (or delete once socket path is proven), factor `_dispatch(...)` into shared module.
- Modified `dual-brain/controller/agent_graph_nodes.py` — swap the subprocess call for a socket call.

Testing:
- New `tests/test_gui_worker_daemon.py` — spawn the daemon in a fixture, connect, send request, assert envelope shape identical to the pre-Fix-M subprocess path. ~6 tests.

GT rows:
- **F-91_OC** — persistent GUI worker daemon replaces per-call subprocess. Symptom: SIGTRAP in libglib on `# list my open windows` under v6.12 Fix I+. Root cause: root systemd inherits none of the user's session env; pyatspi aborts on missing D-Bus session. Fix: user-scope systemd unit with own mainloop + socket IPC. Regression lock: `test_gui_worker_daemon.py`.

**Why Step 1**: without Fix M, every OC edition user hitting a `gui.*` command sees the same crash pattern — makes the "super powerful" edition demo worse than v6.12, not better. The persistent worker is the correct architecture regardless of QB choice; folding it in at OC edition Step 1 means one code change, both v6.13_OC users AND future v6.13 current-edition point-releases benefit (since it's a pure improvement to the shared foundation, it can be backported to v6.13-current before v6.13_OC ships).

---

## 3. Naming convention (canonical)

- Docs home: `docs/v6.x_OC/` — this folder.
- ISO filenames: `v6.13-arm64.iso` (current/Gemini) and `v6.13_OC-arm64.iso` (OC). Same for amd64.
- Backend name: `qb.backend = "opencode"` in `/etc/icebreaker/controller.toml` for OC; `qb.backend = "gemini"` for current.
- Systemd unit: `icebreaker-qb_oc.service` (underscore-friendly systemd path).
- Config file: `/etc/icebreaker/qb_oc.json` (curated model whitelist for OpenCode).
- GROUND_TRUTH failure IDs: `F-91_OC`, `F-92_OC`, `F-93_OC`, `F-94_OC` — the `_OC` suffix makes edition attribution obvious at grep time.
- Build env var: `EDITION={current,oc}` (short form so shell scripts stay readable). ISO filenames use `_OC` (visible artifact); build variables use lowercase `oc`.

---

## 4. Architecture

### 4.1 Shared foundation

Both editions share the same v6.12 codebase (Controller, PB, mcpd, GUI Agent, RPA Bridge, terminal). Divergence is a **build-time switch** driven by an `EDITION` env var passed into `incremental/build/build-iso.sh`.

- `EDITION=current` → no opencode binary shipped; `qb.backend = "gemini"` default in `/etc/icebreaker/controller.toml`. Bit-identical to v6.12 code path.
- `EDITION=oc` → opencode binary + `OpencodeBackend` module included; systemd `icebreaker-qb_oc.service` shipped; `qb.backend = "opencode"` default; Control Center exposes a **Models** tab with dropdown reading the curated whitelist.

### 4.2 OC edition QB architecture

```
User query (# take a screenshot)
      │
      ▼
Icebreaker Controller (Python daemon, existing)
      │
      ├─ builds shell context (cwd, recent turns, session_cwd override — v6.12 Fix D)
      │
      ▼
OpencodeBackend  (new module: controller/backends/opencode.py)
      │
      ├─ HTTP POST /session/<id>/message on opencode's local daemon
      │      body: { model: <user-selected>, agent: "plan", parts: [...] }
      │
      ▼
opencode daemon  (`opencode serve --port 4096 --hostname 127.0.0.1`)
      │   systemd unit: icebreaker-qb_oc.service
      │   Landlock:  only opencode.json (RO) + /var/lib/icebreaker/opencode/ (RW)
      │   Seccomp:   restricted syscall set (harvested per L.2)
      │   RestrictNetwork: only api.anthropic.com / api.openai.com /
      │                     generativelanguage.googleapis.com / 127.0.0.1
      │
      ├─ Runs its own plan-reflect-retry loop (bounded by Icebreaker):
      │      --agent plan  (write / edit / bash / patch / webfetch = DENY)
      │      --max-tokens 4096, --timeout 30s, --max-turns 5
      │
      ▼
ND-JSON event stream on HTTP SSE endpoint /event
      │
      ├─ Icebreaker parses events, logs to audit (INV-8, K.4)
      ├─ Renders CoT panel in real-time (fixes v6.12's empty CoT panel — K.5)
      │
      ▼
Final Intent Object (JSON matching intent.json schema)
      │
      ▼
Existing AgentGraph pipeline (unchanged from v6.12):
      planner_node normalizes Intent
      → risk_classifier → executor → verifier → hitl_gate → mcpd_dispatcher
      → audit_writer → END
```

### 4.3 Model hot-swap surface

**Terminal TUI and shell trigger:**
- Persistent dropdown in Control Center → Models tab (populated from `/etc/icebreaker/qb_oc.json` whitelist)
- Runtime override via shell: `# model claude-opus-4.7` sets `SessionState.qb_model_override` (mirrors v6.9 nav.cd's SessionState mutation pattern via session_op manifest)
- Per-turn env override: `IB_MODEL_OVERRIDE=anthropic/claude-opus-4-7` in the shell trigger environment

**Whitelist config** (`/etc/icebreaker/qb_oc.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "anthropic/claude-opus-4-7",
  "agent": "plan",
  "permission": {
    "bash":     "deny",
    "edit":     "deny",
    "write":    "deny",
    "patch":    "deny",
    "webfetch": "deny"
  },
  "provider": {
    "anthropic": {
      "whitelist": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]
    },
    "openai": {
      "whitelist": ["gpt-5", "gpt-4o", "gpt-4o-mini"]
    },
    "google": {
      "whitelist": ["gemini-2.5-pro", "gemini-2.5-flash"]
    }
    // ollama provider block removed for v6.13_OC per D-R2-5; returns in v6.14_OC
  }
}
```

Default = Claude Opus 4.7. Cloud providers only in v6.13_OC (D-R2-5 supersedes D2 — Ollama offline path deferred to v6.14_OC).

---

## 5. Fix K — OpencodeBackend + agentic planner + model registry

### K.1 New module: `dual-brain/controller/backends/opencode.py`

Implements `BrainBackend` ABC (existing at `backends/base.py`). Owns:

- **Daemon supervision** — spawns `opencode serve` if not running; health-checks via `/health`.
- **HTTP client** — POST `/session/<id>/message` with per-request `model` override.
- **Event stream parser** — consumes SSE `/event` stream, emits parseable events for Icebreaker's audit + CoT.
- **Plan-mode enforcement** — always sends `agent: "plan"` + rejects any response containing `tool.called` events (defense in depth on top of opencode's own permission config).
- **Retry policy** — bounded (max 3 reflect loops, 30s wall clock).

Reuses:
- `backends/base.py::BrainBackend` interface — `complete(system, user, schema, max_retries)` shape unchanged.
- `backends/litellm_adapter.py` cost/token accounting shape — port the token-count / cost fields for the audit row.
- `agent_graph_nodes.py::_dispatch_in_subprocess` pattern (v6.12 Fix I+) for subprocess-safety.
- `agent_graph_nodes.py::audit_writer_node` — one call gets an audit row identical in shape to Gemini turns.

Reads:
- `/etc/icebreaker/qb_oc.json` (model whitelist).
- `SessionState.qb_model_override` (per-session hot swap).
- Env `IB_MODEL_OVERRIDE` (per-turn shell trigger override).
- API keys via existing secret store: `~/.local/share/opencode/auth.json` OR env-var passthrough (BP-8: never in config).

### K.2 Model registry + Control Center UI

- New `controller/model_registry.py` reads the qb_oc.json whitelist + augments with provider metadata (cost per 1M tokens, latency band, capability tags — "vision", "long-context", "fast").
- New GUI file `dual-brain/gui/control/models_page.py` — dropdown of allowed models, current selection, "verify" button that pings the model with a 20-token test prompt and shows green/red like the existing preset verifier.
- New GUI file `dual-brain/gui/control/api_keys_page.py` — separate tab for Anthropic / OpenAI / Google key entry (per D3).
- Reuses `controller/preset_verifier.py` (from Phase 5 Scope C) for the health-check semantics + XDG cache for TTL-based badges.

### K.3 Shell + TUI hot-swap wiring

- Add `impl_kinds/session_op.py::set_model` (mirrors `set_cwd` — mutates `SessionState.qb_model_override`).
- New `# model <name>` phrase in the QB prompts (nav.cd pattern — session-op tools bypass QB entirely and route through the manifest).
- Terminal TUI reads `SessionState.qb_model_override` in its status bar; updates on every turn's ResultEvent (piggybacks v6.12 Fix G's session-state RPC exposure).

### K.4 Plan-reflect-retry bounds

Every safety knob is set from Icebreaker's side (not opencode's):
- `--max-turns 5` — opencode CLI flag capping the internal ReAct loop.
- `--timeout 30000` — 30-second wall clock.
- `permission = {bash: deny, edit: deny, write: deny, patch: deny, webfetch: deny}` in qb_oc.json.
- MCP disabled entirely (opencode's `mcp` config section empty).
- OpencodeBackend rejects any response containing `tool.called` events, logs to audit as `error_kind="opencode_tool_leak"` for triage.
- `--agent plan` mode — read-only agent.

### K.5 CoT panel live-stream (side benefit)

Because opencode emits per-step ND-JSON events, Icebreaker's TUI CoT panel finally has real per-step data to render. This closes v6.12's "chain of thought is empty" complaint from Stage E without needing Task #151 (astream_events on LangGraph) — the events come from opencode instead. Bridge maps opencode's `message.part.updated` events to Icebreaker's `CotEvent` shape.

---

## 6. Fix L — Two-ISO build pipeline

### L.1 Build variable: `EDITION`

`incremental/build/build-iso.sh` accepts `EDITION={current,oc}`. Default `current` (preserves v6.12 build behavior).

- `EDITION=current`: skip opencode install, skip OpencodeBackend Python module install (venv), skip qb_oc.json copy, skip systemd unit, skip Models tab in Control Center. Bit-identical to v6.12.
- `EDITION=oc`: install opencode binary from Go build artifact into `/usr/bin/opencode`, install OpencodeBackend module, copy curated qb_oc.json, install `icebreaker-qb_oc.service` systemd unit + Landlock/Seccomp profile, wire Models + API Keys tabs.

### L.2 Cross-compile opencode (revised per D-R2-1 — Bun, not Go)

**opencode is TypeScript running on Bun** (correction from Revision 1). Bun supports `bun build --compile --target=bun-linux-<arch>` to produce a single-file executable that embeds the Bun runtime — no runtime install needed on the guest.

Add `incremental/build/opencode-build.sh`:
- Clones `sst/opencode` at a pinned Git SHA (documented in `models/checksums.sha256` and in `OPENCODE_BINARY_INTEGRITY.md`).
- Runs `bun build --compile --target=bun-linux-amd64 --outfile opencode-amd64 <entry>` on the build VM.
- Runs `bun build --compile --target=bun-linux-arm64 --outfile opencode-arm64 <entry>` (Bun supports cross-arch compile from amd64 host).
- SHA-256 both binaries into `models/checksums.sha256` (R7 extension covers non-model binaries that read user input).
- Ships to `/usr/bin/opencode` in the OC ISO.
- Expected size per binary: ~100–150MB (embedded Bun runtime + opencode JS).

**Fallback (Option B)**: if the compile output misbehaves under Landlock/Seccomp (Bun tries to write to a cache dir the ruleset denies), install `bun` via the tarball from bun.sh (~50MB) + ship opencode source (~10MB), systemd unit runs `ExecStart=/usr/bin/bun /opt/icebreaker/opencode/index.ts serve ...`. Preflight (D-R2-3) determines A vs B before implementation.

`mcpd-harvest.sh`-adjacent script harvests opencode's syscall set for the Landlock+Seccomp profile — same shape either way (compile output vs runtime+source both need syscall harvest).

### L.3 Rebuild wrapper

`cx-distro/rebuild/rebuild-v67.sh` learns a `V67_EDITION` env var:
- `V67_EDITION=current` (default) → produces `v6.13-{arch}.iso`.
- `V67_EDITION=oc` → produces `v6.13_OC-{arch}.iso`.
- `V67_EDITION=both` → both editions × both arches.

For v6.13 ship: two invocations, each producing 2 ISOs. Total 4 ISOs on the Mac.

### L.4 systemd unit for opencode daemon

New file `cx-distro/distro/systemd/icebreaker-qb_oc.service`:
- `Type=simple`, `ExecStart=/usr/bin/opencode serve --port 4096 --hostname 127.0.0.1 --config /etc/icebreaker/qb_oc.json`.
- `RestrictNetwork` allowlist: `api.anthropic.com`, `api.openai.com`, `generativelanguage.googleapis.com`, `127.0.0.1`.
- `Landlock`: only `/etc/icebreaker/qb_oc.json` (RO), `/var/lib/icebreaker/opencode/` (RW for sessions), `~/.local/share/opencode/auth.json` (RO).
- `SystemCallFilter`: harvested set from opencode-harvest.
- `NoNewPrivileges=yes`, `ProtectHome=tmpfs`, `PrivateTmp=yes`, `MemoryHigh=1G`, `TasksMax=100`.
- `Wants=network-online.target` + `After=network-online.target`.
- Ordering: starts AFTER `icebreaker-controller.service` (Controller supervises it).

### L.5 Boot-time model whitelist verification

First-boot check confirms every model in the whitelist is reachable with the stored API key (reuses `preset_verifier.py`). If any fails, boots with a red badge in the Control Center — user knows before their first query which providers need setup.

---

## 7. Safety envelope preservation

| Invariant | How preserved in OC edition |
|---|---|
| **INV-1** brain isolation | opencode never touches mcpd; `--agent plan` + config `permission: deny-all` disable every opencode tool; OpencodeBackend rejects any response containing tool events. Defense in depth (config + runtime check + Landlock). |
| **INV-2** schema enforcement | OpencodeBackend translates opencode's plan JSON → Intent Object → runs through existing `intent_schema.validate()`. Malformed plans → BrainSchemaError → audit "opencode_schema_mismatch". |
| **INV-3** mcpd network isolation | Unchanged. opencode talks HTTP to LLM providers via its own daemon; mcpd stays stdio-only. |
| **INV-4** parameter validation | Unchanged. Intent Object still validated before executor → mcpd. |
| **INV-5** sandboxing | opencode subprocess IS a new sandbox surface — new systemd unit gets Landlock + Seccomp identical in shape to mcpd's. Boot fails if Landlock kernel < 5.13. |
| **INV-6** COW before destructive | Unchanged. mcpd's fs.write handler still applies COW when the Intent reaches it. |
| **INV-7** model weight integrity | Extended: `models/checksums.sha256` now covers **opencode binary hash** too. R7 extension: "any binary that reads user input during turn processing carries a checksum." Boot verifies. |
| **INV-8** audit log integrity | Every opencode call gets a `model`, `tokens_in`, `tokens_out`, `cost_estimate_usd`, `duration_ms`, `error_kind` (if any) audit row identical in shape to v6.12 Gemini rows. INV-8 hash chain preserved. See D4 for rotation strategy. |

Full expansion lives in `SAFETY_ENVELOPE.md`.

---

## 7.5 Update mechanism (from D-R2-7 — SoTA research 2026-07-24)

State-of-the-art hybrid combining minisign + systemd-sysupdate + HITL-gated overlay updates + Chrome-style binary deltas + TLS cert pinning. Aligns with INV-6 (deliberate human approval) and INV-7 (checksum before load).

### 7.5.1 Trust root and signing

- **Minisign** (jedisct1/minisign — ed25519, single public key, no PKI). Public key at `/usr/share/icebreaker/minisign.pub` shipped in the ISO (~100 bytes).
- Private key held offline by release manager (password-protected file). Rotation via ISO update.
- First-boot script SHA-256s the public key against a hash baked into GRUB config; mismatch aborts boot with a clear error.
- Choice rationale: minisign wins over cosign (no infra) and GPG (no PKI ceremony) for a small team shipping to security-conscious users. Used at distro scale by Alpine + WireGuard + libsodium.

### 7.5.2 What gets updated by which mechanism

| Component | Where | Update path |
|---|---|---|
| Kernel + base OS | squashfs root (RO) | `systemd-sysupdate` A/B partitions (v6.14_OC) |
| mcpd binary | `/opt/icebreaker/` overlay | Chrome/Courgette binary delta (~50KB typical) |
| opencode binary | `/opt/icebreaker/` overlay | Chrome/Courgette binary delta |
| Python controller | `/opt/icebreaker/venv/` overlay | Full replacement (smaller, no delta risk) |
| Config (`qb_oc.json`, `catalogue.toml`) | `/etc/icebreaker/` overlay | Full replacement, signed individually |
| Model weights (GGUF) | `/var/lib/icebreaker/models/` | Never change post-v1.0 (per spec) |

All fetches: signed `releases.icebreaker.sh/latest.json` (TLS cert pinned to specific fingerprint).

### 7.5.3 HITL-gated apply flow

1. `update_checker.py` timer fires weekly → fetches metadata → verifies minisign → writes `/var/lib/icebreaker/update-available.json`.
2. Control Center → System → Updates tab reads the JSON, shows: "v6.14_OC available. mcpd 1.2→1.3 (delta 47KB), opencode 0.4→0.5 (delta 2.1MB), Python controller (full 8MB). Changelog: [...]. [Install] [Later]".
3. **Install button is INV-6-locked for 3s** — matches HitlModal's approve lockout. User must deliberately click after seeing the diff.
4. On Install: fetch each component → SHA-verify → minisign-verify → stage to `/var/lib/icebreaker/staged/<version>/` → atomic symlink swap (`/opt/icebreaker/current` → `staged/<version>`) → `systemctl restart icebreaker-controller`.
5. Prior version kept as `staged/<prev>` for 7 days for manual rollback.
6. **Auto-rollback safety-net**: if daemon fails to come back up healthy in 30s → automatic symlink revert + restart, red banner shown.

### 7.5.4 Threat model coverage

- Compromised release feed → minisign verification fails.
- Compromised TLS → cert pinning breaks handshake.
- MITM'd checksum → minisign covers the checksum file too.
- Replay attack (rollback to known-vulnerable version) → monotonic version check in manifest.
- Compromised public key → key rotation ships as ISO update via sysupdate; users see new key hash in GRUB.

### 7.5.5 Scope for v6.13_OC (this cycle) — CHECK layer only

Ship:
- `dual-brain/controller/update_checker.py` — weekly poll + minisign verify + write status JSON.
- `cx-distro/distro/systemd/icebreaker-update-check.{service,timer}` — weekly timer.
- `dual-brain/gui/control/updates_page.py` — Control Center tab. Install button DISABLED with "Install path lands in v6.14_OC — for now, download the ISO manually."
- `/usr/share/icebreaker/minisign.pub` — public key.
- `models/checksums.sha256.minisig` — signed checksums manifest.
- Extend `models/checksums.sha256` to include mcpd + opencode binary hashes.

**Deferred to v6.14_OC**: actual install happy path (delta apply, staging, symlink swap, auto-rollback), and `systemd-sysupdate` integration for the immutable root.

**GT row**: F-95_OC — update-check + minisign infrastructure landed in v6.13_OC; A/B partition sysupdate + in-place component apply land in v6.14_OC.

---

## 8. User-approved decisions (locked)

### D1 — Cold-start: pre-warm at boot with visible progress

The Controller pre-warms opencode at daemon startup. During pre-warm, the terminal + shell trigger + GUI all render a "Currently indexing + warming up models" status card so the user understands why the first turn isn't accepting input yet.

Implementation shape:
- On boot, `icebreaker-controller.service` starts, sees `qb.backend == "opencode"`, calls `OpencodeBackend.prewarm()` before accepting any RPC turns.
- Prewarm = spawn `opencode serve`, health-poll `/health` until ready (up to 90s), then fire one `--agent plan` no-op through the daemon to force the JS/Bun VM + LLM connection init.
- During prewarm, the daemon accepts RPCs but returns a soft "warming" response (new RPC error code `QB_PREWARMING` — not INTERNAL_ERROR) that the terminal + shell client render as "Currently indexing + warming up models — ready in ~Xs".
- Terminal TUI shows a spinner + text in the CoT panel; shell trigger `ib_run.py` sees the new error code and prints the friendly message instead of `[error]`.
- Prewarm is one-time per daemon start; a warm daemon serves subsequent turns in ~200ms.

### D2 — Local Ollama shipped in v6.13_OC (**SUPERSEDED by D-R2-5 on 2026-07-24**)

**Status**: superseded. See §0 D-R2-5. v6.13_OC ships cloud-only; Ollama deferred to v6.14_OC.

Original text retained for history: [Ollama + `qwen2.5:7b` first-boot pull, ~5-8GB ISO bloat, alongside cloud models in whitelist.] The following implementation items are **not built** in v6.13_OC:
- ~~Add `ollama` to `cx-distro/config/base-packages-{arm64,amd64}.txt`.~~
- ~~New systemd unit `ollama.service`.~~
- ~~First-boot script `/usr/libexec/icebreaker/ollama-firstboot.sh`.~~
- ~~`qb_oc.json` whitelist includes `ollama/*`.~~ (removed — cloud providers only)

### D3 — First-boot API-key onboarding: opens Control Center → API Keys page

Existing Control Center (from Phase 5 Scope C) already has model preset infrastructure. Add a new **API Keys** tab (splits key entry from model selection — Models tab keeps the dropdown + verifier; API Keys tab is dedicated to key management).

Implementation shape (updated per D-R2-5, cloud-only for v6.13_OC):
- First-boot wizard: on daemon-ready signal AND `~/.local/share/opencode/auth.json` absent AND no env vars for cloud provider keys → open Control Center via `xdg-open` or the GTK app launcher, pre-navigated to the API Keys tab.
- API Keys tab has fields for Anthropic / OpenAI / Google API keys with paste boxes + a "Verify" button per row (reuses `preset_verifier.py` for the 20-token test call).
- Each key entry writes to `~/.local/share/opencode/auth.json` (opencode's own auth store) AND sets the corresponding env var in `/etc/icebreaker/env.d/qb_oc.conf` (loaded by the systemd unit via `EnvironmentFile=`).
- Skip button says "Skip — switch to current edition (v6.13-*.iso) for a Gemini-only Icebreaker without API keys" so the user understands OC needs at least one cloud key. Ollama offline path arrives in v6.14_OC.

### D4 — Audit-log strategy: log everything full-fidelity + AI-summarize daily (toggleable)

Full ND-JSON events from opencode land in the audit log verbatim, appended as they arrive. When the daily rollup toggle is **on** (default), once every 24 hours a systemd timer runs an AI compression pass that consumes the day's raw events and replaces them with a per-day summary, keeping the file size bounded. When the toggle is **off**, raw logs accumulate forever — the timer still fires but is a no-op, and the daemon surfaces a warning card once the log exceeds a size threshold so the user isn't surprised by disk usage.

Implementation shape:
- **Full logging (all turns)**: `/var/log/icebreaker/controller-audit.log` — hash-chained JSONL, one line per event (existing INV-8 chain). Opencode's ND-JSON events are wrapped in the existing AuditFields envelope so line size + chain invariants still hold. This is what forensics reads.
- **Rollup toggle**: `/etc/icebreaker/audit.toml` — `[audit.rollup] enabled = true` (default) or `false` to disable AI-summarization + truncation. Toggle is ALSO surfaced in Control Center → Settings tab so users can flip it without touching config files. Runtime change applies at the next timer fire (no daemon restart needed — timer re-reads config on every run).
- **Daily rollup (when `enabled = true`)**: systemd timer `icebreaker-audit-summarize.timer` fires nightly at 03:00 local. Runs `/usr/libexec/icebreaker/audit-summarize.sh` which:
  1. Checks `enabled` toggle first — if `false`, logs "rollup disabled by config; skipping" and exits 0. No side effects.
  2. Snapshots the audit log at midnight → `/var/log/icebreaker/audit-YYYY-MM-DD.raw.jsonl.zst` (compressed, retained 7 days for debug).
  3. Streams the snapshot through the OC daemon's cheapest model (Haiku or local Qwen) with a strict "summarize into <500 tokens per turn, preserve error_kind, intent action, target hash" prompt.
  4. Writes the summary to `/var/log/icebreaker/audit-YYYY-MM-DD.summary.jsonl` — human-scannable + machine-queryable.
  5. Truncates the primary `controller-audit.log` to only the last 24h of full events; older events are represented by the summary file entries.
- **When toggle = false — safety net**: daemon checks `stat --printf='%s' controller-audit.log` on every 100th audit write. If size exceeds `[audit.rollup] warn_bytes` (default 500 MB) → yields a `TurnEvent` with type `audit_disk_warning` that Terminal + shell render as a soft yellow banner ("Audit log is 512 MB — daily rollup is OFF. Enable in Control Center → Settings, or archive `/var/log/icebreaker/controller-audit.log` manually"). If size exceeds `[audit.rollup] error_bytes` (default 5 GB) → audit writes still succeed, but banner escalates to red with "Disk pressure risk." Never blocks turn execution.
- **Hash chain preservation**: summary lines are themselves chained into the audit log with a distinct `event_type: "daily_summary_v1"` so the chain never breaks. Original raw snapshots are separately verifiable.
- **Fall-back**: if summarization fails (LLM down, disk full), the timer logs an error and skips truncation for that day — full log grows one more day rather than losing data.
- **Tunable**: 7-day raw snapshot retention + 500-token/turn compression ratio + warn/error byte thresholds are tunable via `/etc/icebreaker/audit.toml`.
- **Config file shape**:
  ```toml
  [audit.rollup]
  enabled          = true          # daily AI summarization on/off
  time             = "03:00"       # local time to run
  raw_retention_days = 7           # keep compressed snapshots this long
  summary_max_tokens_per_turn = 500
  warn_bytes       = 524288000     # 500 MB — soft banner
  error_bytes      = 5368709120    # 5 GB   — red banner
  ```
- **New files to create**:
  - `dual-brain/controller/audit_summarizer.py` — summarization runner (~140 LOC — added disk-check + toggle-check logic).
  - `dual-brain/controller/audit_disk_watcher.py` — the 100th-write size check that emits the banner event (~40 LOC).
  - `cx-distro/distro/etc/icebreaker/audit.toml` — the config file above.
  - `cx-distro/distro/systemd/icebreaker-audit-summarize.service`.
  - `cx-distro/distro/systemd/icebreaker-audit-summarize.timer`.
- **Control Center wiring**: Settings tab (`dual-brain/gui/control/settings_page.py` — extend existing or create if missing) exposes a switch ("Daily audit log rollup") with tooltip explaining the tradeoff (disk usage vs. forensic completeness). Read/write through `controller/config.py` — reuses the existing GuiConfig plumbing pattern from Phase 5 Scope B (BP-11).
- **New GT rows**: F-94_OC (rollup + toggle), F-95_OC (disk-watcher banner).
- **New R-rule**: R18 — "audit log compression preserves the hash chain; summaries are audit events themselves, never replacements outside the chain. Disabling rollup is a user choice, but the daemon surfaces a visible warning when the log crosses configured size thresholds."

---

## 9. Critical files to modify / create

### New in v6.13_OC only

- `dual-brain/controller/backends/opencode.py` — OpencodeBackend implementing BrainBackend ABC (~250 LOC).
- `dual-brain/controller/model_registry.py` — whitelist + metadata + verifier bridge (~80 LOC).
- `dual-brain/controller/audit_summarizer.py` — D4 daily rollup runner (~120 LOC).
- `dual-brain/gui/control/models_page.py` — Models tab (~150 LOC).
- `dual-brain/gui/control/api_keys_page.py` — API Keys tab (~120 LOC).
- `dual-brain/controller/manifests/session.set_model.yaml` — session-op manifest for `# model <name>`.
- `dual-brain/controller/impl_kinds/session_op.py` — extend with `set_model` method (~20 LOC).
- `dual-brain/scripts/opencode_healthcheck.py` — daemon health probe.
- `cx-distro/distro/etc/icebreaker/qb_oc.json` — curated model whitelist.
- `cx-distro/distro/etc/icebreaker/audit.toml` — audit rotation config.
- `cx-distro/distro/systemd/icebreaker-qb_oc.service` — opencode daemon unit.
- `cx-distro/distro/systemd/icebreaker-qb_oc.landlock` — Landlock ruleset.
- `cx-distro/distro/systemd/icebreaker-qb_oc.seccomp` — seccomp filter.
- `cx-distro/distro/systemd/icebreaker-audit-summarize.service` — D4 runner service.
- `cx-distro/distro/systemd/icebreaker-audit-summarize.timer` — D4 daily timer.
- `cx-distro/distro/systemd/ollama.service` — D2 local model daemon.
- `cx-distro/distro/etc/icebreaker/env.d/qb_oc.conf` — API key env vars for the OC daemon.
- `cx-distro/libexec/icebreaker/ollama-firstboot.sh` — D2 first-boot Ollama model pull.
- `cx-distro/libexec/icebreaker/audit-summarize.sh` — D4 timer wrapper.
- `incremental/build/opencode-build.sh` — cross-compile opencode.
- `incremental/build/opencode-harvest.sh` — syscall harvest for seccomp.

### Modified in both editions

- `dual-brain/controller/config.py` — new `[qb.opencode]` config block (only used when backend=opencode).
- `dual-brain/controller/backends/__init__.py` — register OpencodeBackend in the backend registry (BP-1 pattern).
- `dual-brain/controller/prompts/qb_*.txt` — add `# model <name>` phrase examples per language.
- `cx-distro/build.sh` — accept `EDITION` env, branch installs, embed qb_oc.json only when EDITION=oc.
- `cx-distro/rebuild/rebuild-v67.sh` — accept `V67_EDITION`, produce edition-suffixed ISO filenames (`v${VN}[_OC]-${arch}.iso`).
- `incremental/build/build-iso.sh` — accept EDITION, name output `v${VN}[_OC]-${arch}.iso`.
- `incremental/versions/v2.manifest` — new F-51 markers for OpencodeBackend + daemon unit anchors (only enforced when EDITION=oc).
- `incremental/GROUND_TRUTH.md` § 3 — add v6.13 row split into Current + OC; § 7 — F-91_OC (OpencodeBackend), F-92_OC (opencode-harvest), F-93_OC (edition split), F-94_OC (audit rotation); § 8 — design decision on two-ISO shape.
- `CLAUDE.md` — new "Editions" section documenting the split; extend INV-1 with the opencode language ("QB may be Gemini one-shot or opencode agentic loop — either way, zero MCP.").
- `models/checksums.sha256` — add opencode binary hashes per arch + Ollama model hash.

### Tests

- `dual-brain/controller/tests/test_opencode_backend.py` (NEW, ~15 tests):
  - subprocess spawns / kills cleanly
  - plan-only mode rejects tool.called events
  - model hot-swap per turn (assert HTTP body carries the override)
  - session cwd override still respected (Fix D compat)
  - schema mismatch → BrainSchemaError → audit "opencode_schema_mismatch"
  - timeout → BrainProviderError with recognizable reason
  - ND-JSON event → CoT event translation
  - whitelist violation (user picks a model not in whitelist) → friendly deny
  - prewarm state → QB_PREWARMING error code
- `dual-brain/controller/tests/test_model_registry.py` (NEW, ~8 tests).
- `dual-brain/controller/tests/test_audit_summarizer.py` (NEW, ~6 tests) — dry-run mode + hash chain preservation.
- `dual-brain/gui/tests/test_models_page.py` (NEW, ~6 tests) — mocked GTK signal wiring.
- `dual-brain/gui/tests/test_api_keys_page.py` (NEW, ~5 tests).
- `cx-distro/tests/test_edition_split.sh` (NEW) — asserts the two ISOs differ in exactly the intended file set.

---

## 10. Verification

### Stage A — local pytest

`cd dual-brain && python3 -m pytest -q` → prior count + ~40 new = ~2140 pass.

### Stage B — F-51 marker verification

New markers anchored (only enforced when EDITION=oc; skipped otherwise so the current-edition ISO is not blocked).

### Stage C — differential build test

Build both ISOs with `V67_EDITION=both`; run `cx-distro/tests/test_edition_split.sh` — asserts current vs OC diff is exactly the intended file set (no accidental cross-contamination).

### Stage D — deploy OC to running guest

scp the new modules + opencode binary + qb_oc.json + systemd unit to the running .34 guest (v6.12 install). Enable in `/etc/icebreaker/controller.toml`: `[qb] backend = "opencode"`. `systemctl daemon-reload && systemctl restart icebreaker-controller icebreaker-qb_oc`. Verify daemon is up.

### Stage E — live smoke queries on OC backend

7 golden queries, TUI + shell:
1. `# take a screenshot` → opencode plans, hot-swap to Claude Haiku for speed, emits gui.screenshot intent, subprocess GUI worker (v6.12 Fix I+) executes, screenshot saved.
2. `# create ~/report.md summarizing the last 3 turns` → opencode plans multi-step (system.disk + fs.write with content), reflect-retry produces clean plan, both steps execute.
3. `# model gpt-4o` → session-op mutates SessionState.qb_model_override, terminal status bar updates, next turn uses GPT-4o.
4. `# what's in this folder` → Fix D session_cwd + Fix I+ subprocess + opencode plan-mode all cooperate; CoT panel shows real per-step events (opencode ND-JSON, not v6.12's post-hoc inference).
5. `# whatever nonsense` → opencode's agent reflects, decides unsupported, emits `system.unsupported` intent — soft-outcome path from Fix F still renders friendly card.
6. `# ping the anthropic api` (out-of-catalogue) → opencode confidently emits system.unsupported (not a hallucinated network call) — validates plan-only sandbox held.
7. First-boot flow (fresh guest): daemon shows "Currently indexing + warming up models" status, Control Center opens on API Keys tab, user pastes key → verify → green badge → next query works.

### Stage F — rebuild both editions × both arches

`V67_EDITION=both bash cx-distro/rebuild/rebuild-v67.sh` on the build VM. 4 ISOs produced. Estimated wall clock: ~4.5h. arm64 downloads first per persistent memory rule.

### Stage G — fresh UTM boot for each of the 4 ISOs

Same 7 golden queries per edition per arch. Two current ISOs behave like v6.12. Two OC ISOs demo agentic mode + Ollama offline path. All ISOs' audit logs verify INV-8 hash chain intact after 24h + one rollup cycle.

---

## 11. Rollout order (revised per D-R2-4)

1. **v6.13 (current) build** — in flight now on GCP `icebreaker-build-vm`, tmux session `v613arm`. amd64 done (3.7GB, SHA `3650406e…`), arm64 building. Includes v6.12 HITL hotfix cycle F-92_OC → F-97_OC.
2. **v6.13 SCP + UTM verify** — download both ISOs to Mac. Boot arm64 in UTM. Trigger Tier-2 HITL end-to-end (`# write hello to /tmp/probe.txt` → `a` → confirm `/tmp/probe.txt` written). Acceptance test for all v6.12 hotfixes.
3. **Tag `v6.13`** in git on successful UTM verify.
4. **Branch `feat/v6.13_OC`** from v6.13 tag.
5. **Fix M** (§2.5 — persistent GUI worker daemon) is the FIRST commit on `feat/v6.13_OC`. Backport to `hotfix/v6.13.1` for the current-edition point-release (pure improvement to shared foundation, no reason to hold v6.13 users on broken subprocess pattern).
6. **D-R2-3 preflight** — validate opencode HTTP mode assumptions locally. Decision gate for Fix K design.
7. **Fix K** (OpencodeBackend + hot-swap + model registry).
8. **Fix L** (two-ISO build pipeline with `EDITION={current,oc}`, Bun-compile per arch).
9. **Delta 6 v1 UX layer** (status badge + prewarming + Models tab + API Keys tab + first-boot card).
10. **Delta 7 update-check foundation** (minisign + weekly timer + Updates tab display).
11. **Build v6.13_OC-{amd64,arm64}.iso** on GCP VM. UTM-verify per §10 Stage E.
12. **Ship v6.13_OC**.
13. **v6.14_OC** — Ollama integration (D-R2-5 revisit), full update apply path (D-R2-7 Phase 2), systemd-sysupdate A/B partitions, browser MCP (Phase 7 M7.3).

**Timeline estimate**: v6.13 tag → 4-6 weeks → v6.13_OC ship candidates on both arches.

---

## 12. Rule reinforcements

**R17 — BrainBackend pluggability with zero-execution guarantee**: any new backend added to `controller/backends/` must (a) implement the BrainBackend ABC, (b) prove zero tool-execution capability via a regression test that asserts no tool_call events survive its `complete()` call, (c) be registered via BP-1's registry pattern (never hardcoded call sites), (d) ship a Landlock+Seccomp profile if it runs as a subprocess. Extends R16 to specifically cover backend swaps as pipeline migrations.

**R18 — Audit log compression preserves the hash chain**: any daily/periodic rotation of the audit log MUST write summaries as audit events themselves (chained into the log with a distinct `event_type` like `daily_summary_v1`), never as external replacements. If summarization fails, the timer skips truncation for that day rather than losing data. See D4.

---

## 13. Open items tracked elsewhere (not blockers)

- Final Ollama default model choice — decision recorded in `MODEL_REGISTRY.md` when the compression + latency tradeoff is measured on target hardware.
- Landlock ruleset shape for opencode — decided during `opencode-harvest.sh` runs on the build VM.
- Exact cost band cutoffs for the "when to pick which model" guidance — recorded in `MODEL_REGISTRY.md` after v6.13_OC beta feedback.
- Whether to bundle a browser MCP server in the OC edition — deferred to v6.14 unless demand is loud.

---

## 14. Cross-references

- `docs/v6.x_OC/README.md` — index.
- `docs/v6.x_OC/ARCHITECTURE.md` — expanded diagram + traffic flow.
- `docs/v6.x_OC/SAFETY_ENVELOPE.md` — invariant-by-invariant expansion of § 7.
- `docs/v6.x_OC/MODEL_REGISTRY.md` — model choice rationale, cost bands.
- `docs/v6.x_OC/HOT_SWAP_UX.md` — `# model <name>` phrase + UI spec.
- `docs/v6.x_OC/OPENCODE_BINARY_INTEGRITY.md` — pinned SHA, cross-compile, checksum discipline.
- `docs/v6.x_OC/RESEARCH_NOTES_2026-07-22.md` — Explore-agent briefs preserved verbatim.
- `docs/v6.x_OC/COMPARISON_matrix.md` — feature-by-feature diff between editions.

---

*Authoritative version: this file. Any conflict with `~/.claude/plans/*` — this file wins.*
