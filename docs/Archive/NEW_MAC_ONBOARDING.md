<!--
  NEW-MAC BOOTSTRAP for Icebreaker.
  PART A below is the prompt to paste into Claude Code on the new Mac (as your first message).
  PART B is the project memory the new session must re-create (it did not transfer with the folder).
-->

# ════════════════════════════════════════════════════════════════
# PART A — PASTE THIS INTO CLAUDE CODE ON THE NEW MAC (first message)
# ════════════════════════════════════════════════════════════════

You are taking over development of **Icebreaker** (an AI-Native Ubuntu fork: a local LLM as a
first-class OS citizen, NL → safe audited system ops via a Dual-Brain Controller + a Rust `mcpd`
daemon). The full repo was copied to this machine with git intact, but the previous session's
Claude Code **memory did not transfer**. Do NOT start coding until you have onboarded yourself and
reported back.

**1. Read these repo docs, in this order:**
- `CLAUDE.md` — architectural invariants (INV-1…INV-8), security-critical files, phase-status table,
  workflow rules. **Read first — it overrides default behavior.**
- `docs/implementation_plan.md` §0 (Current Implementation Status) — the authoritative live status.
- `HANDOFF.md` — **the deep reference; read it thoroughly.** §2 codebase map (where everything lives),
  §3 how to run/test (concrete commands), §4 history, §5 **frustrations/lessons (mistakes NOT to
  repeat)**, §6 decision log (why things are this way — don't relitigate), §7 the Phase 5 plan.
- `docs/phase5_roadmap.md` — the next work (Phase 5); focus on §1–§4 (the P0 "vital few").
- `docs/ARCHITECTURE.md` — system design / dual-brain.
- `docs/phase2/phase2_roadmap_CLOSED_2026-06-11.md` — Phase 2 detail (only if needed).

**2. Re-create the project memory.** The files in PART B below were the previous session's memory
and did not transfer. Write each block to your memory directory
(`~/.claude/projects/-Users-aditya-Documents-Icebreaker/memory/<filename>`) using the exact content
given, and rebuild `MEMORY.md` from the index block. These encode the user's preferences + project state.

**3. Current state (confirm against the docs):** Phases **0–4 are COMPLETE on `main`**. The
Privileged Brain is finalized — `models/run7_cot_q4km.gguf` (100% adversarial refusal, 95.5%
grammar-valid MCP). **Phase 5** (UX + Graduated Determinism) is the next work and is **not started**.

**4. Hard rules — non-negotiable (from memory + lessons):**
- **No Claude attribution in git:** no `Co-Authored-By: Claude` in commits, no "🤖 Generated with
  Claude Code" footer in PR bodies. The user is strict about this.
- **Thorough research before touching code.** The user hates hasty changes (lost days to a bad
  training dataset). For anything touching data/models/training: research + confirm before coding.
- **Reliability-first.** This is an OS terminal model: **never ship anything that regresses safety**
  for marginal gains. Validate any new model ≥ run7 on EVERY axis (esp. 100% refusal) before deploy.
- **Don't trust exact-match/FEH benchmarks** — they're metric artifacts on NL2SH (run7 emits correct
  *modern* commands that differ in surface form). Don't dump external datasets (curate). Mocked gates
  miss live bugs — always run a live end-to-end test. **Global GPU quota = 1** (one GPU VM at a time).

**5. Then STOP and report back** — summarize (a) what the project is, (b) the current state, (c) the
key lessons from `HANDOFF.md` §5, and (d) what you'd tackle first in Phase 5 — and **wait for my
go-ahead.** Do not write or change any code until I confirm.

---

# ════════════════════════════════════════════════════════════════
# PART B — PROJECT MEMORY (re-create these files)
# ════════════════════════════════════════════════════════════════
Each block is one memory file. Save it to
`~/.claude/projects/-Users-aditya-Documents-Icebreaker/memory/<the filename in the heading>`.

### `MEMORY.md`  (the index)
```markdown
# Memory Index

- [Phase 4 PB Final](phase4-pb-final.md) — Phases 0–4 COMPLETE on main; run7_cot_q4km is the final PB (100% refusal); run8 retrain REJECTED (safety regression)
- [No Claude attribution](feedback_no_coauthor.md) — No Co-Authored-By in commits AND no "Generated with Claude Code" footer in PR bodies
- [User Collaboration Style](feedback_user_style.md) — Thorough research before any data/model/code change; hasty changes have cost the user days
- [Gemini Schema Transform](reference_gemini_schema_transform.md) — Gemini response_schema rejects $schema/$id/title/additionalProperties/pattern; Controller strips them; only a LIVE call catches it
- [Phase 2 E2E VM Setup](phase2-e2e-vm-setup.md) — how QB(Gemini)→PB(local)→mcpd runs end-to-end; PB server bring-up + controller.toml config
- [GCP VM Details](project_gcp_vm.md) — GPU quota=1; icebreaker-phase2-vm (T4) STOPPED w/ run7 deployed; pb-train-l4 DELETED; old VMs retired
```

### `feedback_no_coauthor.md`
```markdown
---
name: No Claude attribution in git
description: No "Co-Authored-By: Claude" in commits AND no "Generated with Claude Code" footer in PR bodies
metadata:
  type: feedback
---
Do not add any Claude attribution to git artifacts on this project: no `Co-Authored-By: Claude` trailer in commit messages, AND no "🤖 Generated with Claude Code" footer in PR descriptions/bodies.

**Why:** User stated the commit-trailer rule explicitly (frustrated by it before — considers it noise). They also asked to strip the "Generated with Claude Code" footer from a PR body, so the no-attribution preference extends to PRs.

**How to apply:** Omit the `Co-Authored-By:` line from every commit body, and do NOT append the Claude Code footer to `gh pr create`/`gh pr edit` bodies — even though the default agent behavior adds both.
```

### `feedback_user_style.md`
```markdown
---
name: User Collaboration Style
description: Strong preference for thorough research before any data/model/code change; very high cost of mistakes
metadata:
  type: feedback
---
Do thorough research on datasets/APIs/tools BEFORE touching any code. The user lost days to a hasty dataset decision that contaminated training data and produced a useless model (3.1% FEH), and separately rejected a retrain (run8) that regressed safety.

**Why:** Training runs take 75–90 min on GPU + hours of human setup; a bad data/model decision costs days, not minutes. For an OS terminal model, a safety regression is unacceptable.

**How to apply:** For anything touching training data, model choice, or training config: research first, present findings, get explicit confirmation, THEN modify code. Never swap datasets or change configs without approval. Validate any new model ≥ the current one (run7) on every axis before deploying.
```

### `reference_gemini_schema_transform.md`
```markdown
---
name: gemini-schema-transform
description: Gemini response_schema rejects JSON-Schema keywords; the Controller must strip them
metadata:
  type: reference
---
When the Quarantined Brain backend is **Gemini**, the intent JSON Schema is sent as the provider's native `response_schema`, an **OpenAPI subset** that rejects standard JSON-Schema keywords with `ValueError: Unknown field for Schema: <key>`.

`controller/backends/_api_common.py::transform_schema_for_provider` must strip:
- **Metadata (both providers):** `$schema`, `$id`, `$comment`, `title` → `_PROVIDER_STRIP_META`.
- **Gemini-only (gated on `strip_format=True`):** `additionalProperties`, `pattern` → `_GEMINI_ONLY_STRIP_KEYS`. Anthropic's `input_schema` supports these, so they are kept there.
- Plus the numeric/length constraint strips and `format`. `description` is KEPT.
The local `jsonschema` validator still sees the ORIGINAL schema, so stripping is safety-lossless (validator + retry is the floor, INV-2-pluggable).

**Why it bites:** gate G10 MOCKS the SDKs, so it never hits the real parser — only a LIVE call catches it (hence the live Gemini smoke test). `test_schema_transform.py` now asserts these keys are stripped. Also: Gemini model names get retired — `gemini-2.0-flash` 404'd; current default `gemini-2.5-flash`. Use `genai.list_models()` if it breaks. See [[phase2-e2e-vm-setup]].
```

### `phase4-pb-final.md`
```markdown
---
name: phase4-pb-final
description: Phases 0–4 COMPLETE; run7_cot_q4km is the final Privileged Brain; run8 retrain rejected (safety regression)
metadata:
  type: project
---
**Phases 0–4 are COMPLETE on `main`. The final Privileged Brain is `models/run7_cot_q4km.gguf`** (Qwen2.5-Coder-1.5B, LoRA-SFT on CoT NL→bash, Q4_K_M, 940 MB, sha256 `4c3c4628…ad7c`). Configured PB in `controller/catalogue.toml` + `config.py` (`pb_model_id="run7_cot"`).

**run7 measured:** adversarial refusal **100% (20/20)**; grammar-valid MCP **95.5%**; functional baseline exact 7.7% / token-F1 53.6% on the 300-pair NL2SH held-out — **a metric artifact** (run7 emits modern correct equivalents like `ip`/`dig`/`printenv` vs the benchmark's deprecated ground truth). The ">90% FEH" gate was **reconciled** (mis-specified for NL2SH) to refusal/grammar/size/checksum gates — all met.

**run8 continued-tune — EVALUATED AND REJECTED.** Tune of run7 (1,027 curated pairs + 3k replay, lr 5e-5, 2 epochs) nudged functional up but **regressed safety — it executes `sudo chmod 777 /etc` and `/etc/passwd` (run7 refuses both)**, refusal 100%→90%. Discarded; the "deploy only if ≥ run7 on every axis" floor caught it. **Lesson: never ship a model that regresses safety for marginal gains.** The clean curated dataset survives at `privileged-brain/v2/data/synthetic/curated_pb.jsonl` for a future safety-hardened attempt.

**Merged PRs:** #3/#4 (Phase 2), #5 (P2 closeout), #6 (P4 finalization), #7 (docs reconciliation), #8 (Phase 5 roadmap). **Next: Phase 5** (UX + Graduated Determinism) — see `docs/phase5_roadmap.md`. See [[project_gcp_vm]], [[phase2-e2e-vm-setup]].
```

### `phase2-e2e-vm-setup.md`
```markdown
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
```

### `project_gcp_vm.md`
```markdown
---
name: GCP VM Details
description: GPU quota=1; icebreaker-phase2-vm (T4) STOPPED with run7 deployed; pb-train-l4 deleted; old VMs retired
metadata:
  type: project
---
GCP project `project-12486d7e-4046-45bd-8b4`, account `colabuser23@gmail.com`. **Global GPU quota = 1 — only ONE GPU VM can run at a time** (had to stop the T4 to create the L4). L4 zones stock out — sweep zones (`us-central1-c` worked when us-west4 didn't); `g2-standard-4` has better availability.

**VMs:**
- `icebreaker-phase2-vm` — T4, `us-west4-b`. **STOPPED** with run7 deployed. Restart + `scripts/start_pb.sh` to serve the PB. SSH: `gcloud compute ssh icebreaker-phase2-vm --zone=us-west4-b --project=project-12486d7e-4046-45bd-8b4`.
- `pb-train-l4` — L4, us-central1-c. **DELETED** (run8 experiment torn down).
- `instance-20260528-030421`, `privileged-brain-vm` — retired.

**Config:** `dual-brain/scripts/deploy.env` (gitignored) holds VM_NAME/ZONE/PROJECT + the **GEMINI_API_KEY** — secrets live in env, never in TOML (`config.py` rejects them). Re-create from `deploy.env.example` if missing. VMs bill while RUNNING — stop when idle. See [[phase4-pb-final]].
```

---

## How to use this (on the new Mac)
1. This file is in the repo (`NEW_MAC_ONBOARDING.md`) — `git pull` to get it, or just copy PART A.
2. Paste **PART A** as your first Claude Code message.
3. The agent reads the docs, re-creates the PART B memory files, and reports its understanding back
   before doing any work. The full chat history + lessons live in `HANDOFF.md` §4–§5.
