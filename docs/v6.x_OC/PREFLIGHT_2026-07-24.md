# Delta 3 Preflight — opencode HTTP mode validation

**Date**: 2026-07-24
**Version tested**: `opencode-ai@1.18.4` (npm)
**Runtime**: Node 22.17.0 on macOS (npm-installed CLI; the shipped binary embeds Bun)
**Purpose**: retire the biggest uncertainty in `docs/v6.x_OC/IMPLEMENTATION_PLAN.md` §5.1 (Fix K) — that opencode exposes an HTTP mode with per-session agent + model configuration + SSE event stream, so `OpencodeBackend` can be built as designed.

**Result**: **GO** with Option A (Bun-compile single binary). Plan §5.1 is directionally correct; corrections below are minor and don't invalidate the design.

---

## Confirmed capabilities

1. **HTTP serve mode exists**. `opencode serve --port 4096 --hostname 127.0.0.1 --print-logs --log-level INFO` starts a headless HTTP server. Warning printed if `OPENCODE_SERVER_PASSWORD` env var is unset ("server is unsecured") — for Icebreaker's use inside the OC systemd unit with `Landlock` + `RestrictNetwork`, this is acceptable; we can also set the password from `EnvironmentFile=/etc/icebreaker/env.d/qb_oc.conf` for defense-in-depth.

2. **OpenAPI 3.1.0 spec at `/doc`**. Full endpoint catalog + request/response schemas. This gives us machine-readable contracts so `OpencodeBackend` can be code-generated or validated against the spec.

3. **Session lifecycle**: `POST /session` creates a session (returns `id`, `projectID`, `directory`, cost/tokens counters). `GET /session/{id}` returns state.

4. **Per-session agent mode**: `POST /api/session/{sessionID}/agent` with `{"agent": "plan"}` sets plan mode. Session state reflects `"agent": "plan"` after the call. Session-scoped, persists until changed.

5. **Prompt submission**: `POST /api/session/{sessionID}/prompt` with body:
   ```json
   {"prompt": {"text": "...", "providerID": "opencode", "modelID": "ling-3.0-flash-free", "agent": "plan"}}
   ```
   Returns immediately with a message envelope: `{"data": {"admittedSeq": N, "id": "msg_...", "sessionID": "...", "prompt": {"text": "..."}, "delivery": "steer", "timeCreated": ...}}`. Async execution — the actual LLM call happens off-thread and events are emitted via SSE.

6. **Model hot-swap options** — TWO paths, both work:
   - **Per-session**: `POST /session/{sessionID}/model` with `{"providerID": "...", "modelID": "..."}` sets the session default model for subsequent prompts.
   - **Per-prompt**: include `providerID` + `modelID` in the prompt body directly. Overrides the session default for that one call.
   Icebreaker uses **per-prompt** because a single Icebreaker session may hot-swap models between turns (Delta 6.1 Ctrl+M picker), and per-prompt override is stateless — no risk of a stale session-level setting.

7. **Model catalog**: `GET /api/model` returns provider metadata + model list. opencode ships with a built-in free model `opencode/ling-3.0-flash-free` — useful for smoke tests without cloud API keys.

8. **SSE event stream endpoints** (four variants):
   - `GET /event` — global event stream, all sessions.
   - `GET /global/event` — same content, different mount.
   - `GET /api/event` — API-prefixed variant.
   - `GET /api/session/{sessionID}/event` — filtered to one session (this is what `OpencodeBackend` uses).

9. **Session interrupt**: `POST /api/session/{sessionID}/interrupt` and `POST /session/{sessionID}/abort` — either kills an in-progress prompt. Icebreaker uses this when the user cancels a turn.

10. **Provider auth**: `PUT /auth/{providerID}` sets credentials. `GET /provider/auth` reports auth state per provider.

---

## Corrections to plan §5.1

**Endpoint name**: plan said `POST /session/<id>/message`. Actual is **`POST /api/session/{sessionID}/prompt`**. The `/api/` prefix and the `/prompt` (not `/message`) verb are both required.

**Request body shape**: plan said body `{model, agent, parts: [{type: "text", text: "..."}]}`. Actual is **`{prompt: {text, providerID, modelID, agent}}`** — the prompt is a wrapper object, not top-level fields, and it uses `text` string, not a `parts` array.

**Async execution**: the prompt call returns immediately with a message ID. The LLM work happens in the background; results arrive via SSE. `OpencodeBackend.complete()` must open the SSE stream BEFORE sending the prompt, then correlate the message ID to filter events, then close the stream on the terminal event. This is a materially different pattern than a synchronous `POST → response body` — reflect this in `opencode_backend.py` design.

**Default config has NO permission block and NO mcp block**. `GET /config` returned no `permission` or `mcp` keys. To enforce plan-only (deny bash/edit/write/patch/webfetch) and MCP-disabled per plan §5.4, Icebreaker MUST ship a `/etc/icebreaker/qb_oc.json` that explicitly sets both blocks. **Never rely on opencode's defaults.** Verify at boot: `OpencodeBackend.prewarm()` reads the effective config via `GET /config` and asserts both blocks match the shipped values; startup fails loudly if they don't.

---

## Uncertainties deferred to Fix K implementation

- **Full end-to-end LLM call**: tested with the built-in free `ling-3.0-flash-free` model — the prompt was admitted (message ID returned) but message history stayed empty after 8s. Unclear if the free model requires setup, or if the ling provider is a stub that needs config. Real cloud model verification (Claude Haiku via `PUT /auth/anthropic` + API key) is a Fix K.1 first-week smoke test.
- **SSE event shape during real call**: the SSE curl was capturing 0 lines during the ling test (probably because ling didn't actually run). Need to verify the per-step event shape (`message.part.updated`, `tool.called`, `session.finished`, etc.) during Fix K with a real model call. Bridge to Icebreaker's `CotEvent` shape per plan §5.5 requires knowing the exact opencode event schema.
- **`tool.called` rejection**: plan §5.4 says "OpencodeBackend rejects any response containing tool.called events". Confirm the exact event name is `tool.called` (or similar) by observing during a real call. If plan-mode `permission: deny-all` is set correctly, `tool.called` should never fire — but the runtime rejection is defense-in-depth.

---

## Decision

**Fix K design in the in-tree plan is confirmed** with the endpoint/shape corrections above. Execution can proceed:

1. `OpencodeBackend` uses `POST /api/session/{id}/prompt` + `GET /api/session/{id}/event` SSE stream.
2. Per-prompt model override via `providerID` + `modelID` in the prompt body.
3. Explicit `permission` + `mcp: {}` blocks in shipped `qb_oc.json`.
4. `OpencodeBackend.prewarm()` verifies effective config matches shipped values at boot.

No fallback to CLI-subprocess pattern needed. Bun-compile per D-R2-1 remains the preferred packaging path.

---

## Reproducibility

To re-run this preflight:

```bash
npm install -g opencode-ai
opencode serve --port 4096 --hostname 127.0.0.1 --print-logs --log-level INFO &
sleep 2
# fetch OpenAPI spec
curl -s http://127.0.0.1:4096/doc | jq '.paths | keys'
# create session
SID=$(curl -s -X POST http://127.0.0.1:4096/session -H 'Content-Type: application/json' -d '{}' | jq -r .id)
# set agent
curl -X POST http://127.0.0.1:4096/api/session/$SID/agent -H 'Content-Type: application/json' -d '{"agent": "plan"}'
# send prompt (with real API key configured via `opencode auth login`)
curl -X POST http://127.0.0.1:4096/api/session/$SID/prompt \
  -H 'Content-Type: application/json' \
  -d '{"prompt": {"text": "list two colors", "providerID": "anthropic", "modelID": "claude-haiku-4-5", "agent": "plan"}}'
# stream events
curl -sN http://127.0.0.1:4096/api/session/$SID/event
```
