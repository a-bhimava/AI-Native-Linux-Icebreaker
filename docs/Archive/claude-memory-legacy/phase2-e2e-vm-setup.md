---
name: phase2-e2e-vm-setup
description: How the full QB→PB→dispatch end-to-end is brought up on a GPU VM
metadata:
  type: project
---
The full dual-brain pipeline runs end-to-end: **QB = Gemini cloud, PB = local `run7_cot_q4km.gguf`, dispatch via mcpd.** Proven on `icebreaker-phase2-vm` (now stopped). To bring it up on any Linux GPU VM:

**Layout:** `~/dual-brain/` (controller; venv needs `python3-venv` apt pkg), `~/src/mcpd/` (build `cargo build --release` → `~/src/mcpd/target/release/mcpd`), `~/models/run7_cot_q4km.gguf`, `~/pb-grammar/mcp_tool_call.gbnf`, `~/llama.cpp/build/bin/llama-server` (CPU build `-DGGML_CUDA=OFF` is fine for the 1.5B).

**Bring-up:**
1. PB server — persist with `setsid` (NOT `pkill -f "...8080"`, which self-matches the ssh cmd and kills your session — use the `[8]080` bracket trick or kill by PID):
   `setsid bash -c "~/llama.cpp/build/bin/llama-server --model ~/models/run7_cot_q4km.gguf --grammar-file ~/pb-grammar/mcp_tool_call.gbnf --port 8080 --host 127.0.0.1 --ctx-size 4096 -ngl 0 --no-warmup > ~/pb-server.log 2>&1" < /dev/null &` → wait for `:8080/health`.
2. `controller/controller.toml` = example with `[qb] backend="gemini"` + an active `[run]` block: `mcpd_binary = "/home/<user>/src/mcpd/target/release/mcpd"` (ABSOLUTE) and `pb_endpoint = "http://127.0.0.1:8080"`.
3. `source scripts/deploy.env` (real GEMINI_API_KEY), then `PYTHONPATH=. python3 -m controller --config controller/controller.toml "<nl command>"`.

mcpd correctly rejects PB tool calls with hallucinated params (INV-4) — that's the safety boundary, not a bug. Gemini gotcha: [[gemini-schema-transform]].
