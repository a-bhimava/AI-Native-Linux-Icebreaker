# Icebreaker — Session Handoff (continue on another Mac)

> **Paste `NEW_MAC_ONBOARDING.md` PART A as your first message to Claude Code on the new Mac.** This
> file is the deep reference it points to: memory, codebase map, how to run everything, the full
> history + mistakes to avoid, the decision log, and exactly what to do next (Phase 5).
>
> **CRITICAL:** the persistent memory lives in `~/.claude/projects/-Users-aditya-Documents-Icebreaker/
> memory/` — **outside** the repo — so it did NOT transfer. §1 reproduces it; re-save it (or use
> `NEW_MAC_ONBOARDING.md` PART B, which has the files in memory format).

---

## 0. What this project is
**Icebreaker** = an AI-Native Ubuntu fork where a local LLM is a first-class OS citizen. NL → safe,
audited system ops via a **Dual-Brain Controller** + a sandboxed Rust **mcpd** daemon.
- **Quarantined Brain (QB):** NL → Intent Object. Zero exec, zero MCP. (Cloud Gemini or local model.)
- **Privileged Brain (PB):** Intent → MCP tool call. Zero raw user input. (Local fine-tuned GGUF.)
- **Controller:** the trusted Python mediator — schema-validates intents, classifies risk (Tier 0–3),
  gates dangerous ops (HITL), passes only opaque UUIDs to the PB, audits everything.
- **mcpd:** Rust daemon, 22 tools via JSON-RPC over **stdio only**, sandboxed (Landlock + Seccomp-BPF + COW).
- **Source of truth:** `AI_Native_OS_Whitepaper.md` (848 lines). **Read `CLAUDE.md` first** —
  invariants INV-1…INV-8, security-critical files, workflow rules; it overrides default behavior.

---

## 1. Persistent memory — RE-SAVE THESE (or use NEW_MAC_ONBOARDING.md PART B)
**User:** Aditya (abhimava@andrew.cmu.edu, CMU).
**Hard rules:**
- **No Claude attribution in git** — no `Co-Authored-By: Claude` in commits, no "🤖 Generated with
  Claude Code" footer in PR bodies. Strict.
- **Thorough research before touching code** — especially data/models/training; he hates hasty changes
  (lost days to a contaminated dataset). Research + confirm, then code.
- **Reliability-first** — OS terminal model: **never ship anything that regresses safety** for
  marginal gains. Validate any new model ≥ run7 on every axis. "No change > backwards."

**Status (June 2026): Phases 0–4 COMPLETE on `main`. Phase 5 planned. 6/7 not started.**
Merged PRs: #3/#4 (P2), #5 (P2 closeout), #6 (P4 finalization), #7 (docs reconciliation), #8 (P5 roadmap).

**Final PB: `models/run7_cot_q4km.gguf`** (Qwen2.5-Coder-1.5B, LoRA-SFT CoT NL→bash, Q4_K_M, 940 MB,
sha256 `4c3c4628…ad7c`). 100% adversarial refusal, 95.5% grammar-valid MCP. Functional exact-match is
a **metric artifact** (modern correct commands ≠ benchmark's deprecated ground truth). run8 retrain
**rejected** (regressed safety → `chmod 777 /etc`). Curated data survives at
`privileged-brain/v2/data/synthetic/curated_pb.jsonl`.

**GCP:** project `project-12486d7e-4046-45bd-8b4`, account `colabuser23@gmail.com`. **GPU quota = 1**
(one GPU VM at a time). `icebreaker-phase2-vm` (T4, us-west4-b) STOPPED with run7 deployed; `pb-train-l4`
DELETED. Secrets in `dual-brain/scripts/deploy.env` (gitignored, has `GEMINI_API_KEY`).

---

## 2. Codebase map — where everything lives
**`src/mcpd/` — Rust MCP daemon (Phase 1, COMPLETE, on main):**
- `src/main.rs` — entry: logging → `sandbox::apply()` (INV-5, before serving) → `server::run_stdio_server()`.
- `src/server.rs` — JSON-RPC 2.0 router over **stdio** (`tools/list`, `tools/call`); **no network** (INV-3). *security-critical.*
- `src/schema.rs` — tool schema registry + `schema_version`.
- `src/tools/{system,process,fs,service,network,package}.rs` — the **22 tools**. `fs.rs` is the most
  dangerous (path traversal/arbitrary write — validates against roots). *fs.rs security-critical.*
- `src/sandbox/{landlock,seccomp}.rs` (+ COW) — INV-5 kernel sandbox. *security-critical.*
- `src/audit.rs` — O_APPEND audit (INV-8). `tests/`, `fuzz/` (cargo-fuzz), `ci.sh` (Phase 1 gates).

**`dual-brain/controller/` — Python Controller (Phase 2, COMPLETE):**
- `__main__.py` — CLI: single-shot `python -m controller "cmd"`, `--repl`, `--check-isolation`.
  `_build_controller()` wires everything; spawns mcpd via **`McpdClient.spawn(...)`** (not the bare ctor).
- `main.py` — `Controller.run_turn()` = the **14-step orchestration**: QB→intent→`intent_schema.validate`
  →`risk_classifier.classify`→HITL (Tier 3)→`intent_store.put`→PB (UUID only)→tool-call schema-validate
  →QB verifier round-trip→`mcpd_client.call` dispatch→summarize→audit.
- `intent_schema.py` (+ `schemas/intent.json`) — INV-2 validation. *intent_store.py = security-critical (INV-1 opaque store; `revise()` built but UNUSED — Phase 5 M5.1 `[M]odify` wires it).*
- `risk_classifier.py` (+ `_mcpd_tools.py` 22-tool catalogue, kept in sync by `scripts/export_mcpd_catalogue.py`, gate G2) — Tier 0–3.
- `hitl.py` — `HitlPresenter` ABC + `TerminalPresenter`; 3 s lockout (INV-6); **`[E]`/`[M]` are stubs
  (~lines 125-127) → Phase 5 M5.1**. `_build_presenter()` in `__main__.py` is the GUI seam.
- `session.py` + `repl.py` — multi-turn REPL; INV-2-extended (tool output never re-enters PB context).
- `audit.py` — provenance audit (INV-8). `config.py` — `controller.toml` loader (`RunConfig`:
  `pb_model_id`/`pb_endpoint`/`mcpd_binary`; **secrets from env only** — rejects secret-shaped TOML keys).
- `mcpd_client.py` — spawns mcpd subprocess, JSON-RPC. `model_registry.py` + `catalogue.toml` — plug-and-play model resolution + sha256.
- `backends/` — `base.py` (`BrainBackend` ABC) + `registry.py`; `{llama_local,anthropic,gemini}_backend.py`;
  **`_api_common.py::transform_schema_for_provider`** (the Gemini schema fix); `sanitize.py` (`SecretRef`); `auditor.py`.
- `tests/` (27 files), `ci.sh` (**G1–G11** exit gates), `grammars/qb_intent.gbnf`, `prompts/`.
- `scripts/` — `deploy_to_vm.sh`, `start_pb.sh`, `start_qb_local.sh`, `deploy.env`(gitignored)/`deploy.env.example`.

**`privileged-brain/` — PB training pipeline (Phase 4):**
- `v2/scripts/` — `sft_train.py`, `dpo_train.py`, `fuse_lora.py`, `process_datasets.py` (`_make_reasoning`
  is **templated**, not LLM), `eval_feh*.py`, `eval_adversarial_gguf.py`, **`phase4_eval.py`** (the eval
  harness: held-out FEH + refusal + grammar), `generate_dpo_pairs.py`.
- `v2/data/synthetic/curated_pb.jsonl` — the 1,027 hand-verified pairs. `inference/grammar/mcp_tool_call.gbnf` — PB output grammar.

**`models/`** — `run7_cot_q4km.gguf` (gitignored, local), `checksums.sha256` (*security-critical*, INV-7).
**`docs/`** — `implementation_plan.md` (**§0 = live status**), `ARCHITECTURE.md`, `phase{1,5}_roadmap.md`,
`phase2/phase2_roadmap_CLOSED_2026-06-11.md`, `V1_ROADMAP.md`. **`shell/`** — V1 shell trigger (separate path, untouched).

---

## 3. How to run & test
```bash
# mcpd (Rust) — build + smoke
cd src/mcpd && cargo build --release
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | ./target/release/mcpd | head -c 300
ss -tlnp | grep mcpd        # must be EMPTY (INV-3: no listeners)

# Controller gate suite (G1–G11) — needs a venv with deps + mcpd built (G2); G9/G10 need llama-server/SDKs
cd dual-brain && bash controller/ci.sh

# Controller, single-shot end-to-end (needs PB llama-server on :8080 + GEMINI_API_KEY)
cd dual-brain && source scripts/deploy.env
PYTHONPATH=. python3 -m controller --config controller/controller.toml "show disk usage"
PYTHONPATH=. python3 -m controller --check-isolation     # G3/G4 isolation check

# Serve the PB locally (CPU is fine for the 1.5B): build llama.cpp, then
llama-server --model models/run7_cot_q4km.gguf --grammar-file privileged-brain/inference/grammar/mcp_tool_call.gbnf \
  --port 8080 --host 127.0.0.1 -ngl 0 --no-warmup
# (On a GPU VM, full bring-up is in the phase2-e2e-vm-setup memory.)

# Deploy controller to a GPU VM + run tests
cd dual-brain && source scripts/deploy.env && bash scripts/deploy_to_vm.sh

# PB evaluation (FEH held-out + refusal + grammar)
python3 privileged-brain/v2/scripts/phase4_eval.py --help
```
**Invariant check before any change:** there's a `tests/check_invariants.py`-style discipline in
CLAUDE.md (WF-3) — run `cargo test` + `pytest` + the gate suite before marking work done.

---

## 4. Conversation history & summary (what the previous Mac did)
1. **Started:** "is Phase 2 complete? can we start Phase 3?" → found Phase 3 was already **folded into
   Phase 1**; Phase 2 just needed gate verification + merge.
2. **Phase 2 closeout (PR #5):** built the gitignored **`deploy.env` config layer**; ran `controller/ci.sh`
   **G1–G11 green** on the T4 VM; a **live Gemini smoke test caught 3 bugs the mocked gates missed** —
   (a) the Gemini schema-transform, (b) retired `gemini-2.0-flash`→`gemini-2.5-flash`, (c) CLI used
   `McpdClient(path)` not `McpdClient.spawn(...)`. Fixed all.
3. **Phase 4:** measured run7 (100% refusal, 95.5% grammar, low-but-artifact functional).
4. **run8 retrain (rejected):** generated a clean 1,027-pair curated dataset via the
   `training-data-generator` agent, continued-tuned run7 (curated + 3k replay, lr 5e-5, 2 epochs) on a
   fresh L4 → **regressed safety** (would run `chmod 777 /etc`; refusal 100%→90%) → **discarded, kept run7.**
5. **Phase 4 finalized (PR #6)** + **docs reconciled across all docs (PR #7)** + **Phase 5 roadmap (PR #8).**
6. **Now:** on `main` @ latest, all merged; this handoff written for the device move.

---

## 5. ⚠️ Frustrations & lessons — DO NOT REPEAT THESE
1. **Never retain a model that regresses safety** (the run8 lesson). Validate a new PB ≥ run7 on EVERY
   axis (esp. 100% refusal) before deploy. Default to keeping run7.
2. **Don't trust exact-match / FEH on NL2SH** — it's a metric artifact (correct *modern* commands ≠
   benchmark's deprecated ground truth). Judge functional equivalence + safety/grammar, not string match.
3. **Don't dump external datasets** — NL2SH-ALFA had 12.5% placeholder commands, deprecated tools, buggy
   ground-truth, man-page text as "NL". Curate in-house (that's `curated_pb.jsonl`).
4. **Mocked gates miss live bugs** — ci.sh was green but a LIVE Gemini call exposed 3 real bugs. Always live-test.
5. **GPU quota = 1** globally — stop one GPU VM to start another. L4 zones stock out — sweep zones
   (`us-central1-c` worked when us-west4 didn't); `g2-standard-4` has better availability.
6. **Eval on GPU, not CPU** — CPU inference of 300 prompts didn't finish in ~40 min; on the L4 it was fast.
7. **Verify the PR head OID == branch HEAD before merging** — PR #6 was merged before the last commit
   synced (GitHub lag) → orphaned commit, needed a follow-up PR #7.
8. **Detached-process gotchas (VMs):** `pkill -f "...8080"` matches the SSH command itself and **kills your
   session** — use the bracket trick `[8]080` or kill by PID; use `python3 -u` (output buffers otherwise);
   `setsid ... < /dev/null &` survives the SSH session but **dies on VM reboot**.
9. **Secrets in env, never TOML** — `config.py` rejects secret-shaped keys; `GEMINI_API_KEY` lives in the
   gitignored `deploy.env`.
10. **DLVM `common-cu129` has CUDA runtime but no `nvcc`** — a CUDA llama.cpp build fails; use
    transformers-on-GPU for eval, or install the toolkit.

---

## 6. Key decisions & why (don't relitigate these)
- **Phase 3 is folded into Phase 1**, not separate/pending — Landlock/Seccomp/COW shipped *with* mcpd
  (M1.3/M1.4/M1.5). The "Phase 3" heading is kept only for whitepaper continuity.
- **run7 is the PB; don't retrain casually** — it's safe + functionally good; the "low FEH" is a metric
  artifact. The ">90% FEH" gate was **reconciled** to refusal/grammar/size/checksum (run7 meets all).
- **PB stays local; QB may be cloud** — INV-1: the PB has tool access, so it must never get network
  egress; the QB (perception) can be Gemini. This is why the Phase-2 e2e config is QB=Gemini, PB=local.
- **mcpd is stdio-only** (INV-3) — the Controller spawns it as a child process; no sockets, ever.
- **Secrets come from env** (`deploy.env`), never config files — enforced by `config.py`.
- **Curate data in-house** over external benchmarks — quality control is worth more than dataset size.

---

## 7. ▶ Phase 5 — what to do next
**Full plan: `docs/phase5_roadmap.md` (649 lines) — read §1–§4 first.** Phase 5 = **Production UX,
Graduated Determinism & Hardening**: make the Controller installable, hardened against the
spoofing/bypass surface, configurable, and plug-and-play. **Predecessors complete → unblocked.**

**Philosophy (non-negotiable):** Pareto — do **P0 (the vital 20%)** well first. Everything swappable via
config (extend the existing `model_registry`/`BrainBackend`/`HitlPresenter` registry culture). Every new
behavior behind a **feature flag** (`[tier2] enabled` style). Keybindings from config, not hard-coded.

**P0 — the three vital milestones (start here):**
- **M5.1 — Harden + complete the HITL gate** *(security-critical)*. Implement the `[E]xplain`/`[M]odify`
  stubs in `hitl.py` (~lines 125-127) + add `[T]rust`; make it **keymap-driven** (config); harden against
  the spoofing/bypass findings (roadmap §2, SF-1/2/3/4). `[M]odify` activates the unused
  `intent_store.revise()`. Touches `hitl.py`/`intent_store.*` → **security-critical, WF-6 review.**
- **M5.2 — Tier 2 LLM review, escalate-only** *(the Graduated-Determinism headline)*. Today Tier 2
  auto-executes+notifies; add a swappable LLM "reviewer" that can **escalate** a borderline action to
  HITL, behind `[tier2] enabled`. Inserts after step 3 of `Controller.run_turn`.
- **M5.3 — Tamper-evident audit + stronger redaction** *(security-critical; SF-5/6)*.
- Then **P0 exit gates** (roadmap §4) → **P1** (§5) → **P2 + daemon-mode deployment** (§6) → **release-readiness** (§7).

**Recommended kickoff:** read `docs/phase5_roadmap.md` §1–§4, then start **M5.1** on a `feature/phase5-*`
branch, behind a feature flag, with the HITL hardening tests.

---

## 8. Phase 6 & 7 (later)
- **Phase 6 — ISO distribution:** `cx-distro/` not built — live-build, systemd units (mcpd + brain
  services, `Type=notify` ordering), `build.sh` with SHA-256 verification, QEMU + bare-metal testing.
- **Phase 7 — Hardening + release:** E2E (500 queries), external pentest, perf (p95 <500 ms), GPG-signed v1.0 ISO.

---

## 9. New-Mac setup notes
- Folder copied raw (git intact). Rebuild regenerable bits: `cd src/mcpd && cargo build --release`;
  recreate Python venvs (`python -m venv` + `pip install -r dual-brain/requirements.txt` /
  `privileged-brain/.../requirements`). `backups/` (8.8 GB) was a redundant deleted-VM backup — skip it.
- `dual-brain/scripts/deploy.env` (gitignored) holds the **`GEMINI_API_KEY`** — copy it, or re-create
  from `deploy.env.example` and paste the key.
- `models/run7_cot_q4km.gguf` (940 MB) — needed only to run/serve the PB locally; verify vs `models/checksums.sha256`.
- Start fresh on `main` (`git pull`); confirm `docs/implementation_plan.md` §0 + `CLAUDE.md` show Phases 0–4 complete; then Phase 5.
- This `HANDOFF.md` + `NEW_MAC_ONBOARDING.md` are untracked notes — delete once set up, or commit them.
