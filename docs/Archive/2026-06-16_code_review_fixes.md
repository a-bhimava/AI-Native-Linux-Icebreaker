# Code Review Fixes — 2026-06-16

Pre-Phase 6 code review surfaced 10 issues across CLAUDE.md, CI scripts, mcpd,
controller, and training pipeline. After verification against the actual code,
3 were fixed immediately and 7 were deferred with documented reasoning.

## Fixed (on branch `feature/phase6-mcpd-sdnotify`)

### Fix A: INV-7 attribution corrected in CLAUDE.md

**Problem:** INV-7 stated "mcpd MUST verify the SHA-256 of each GGUF model file
at daemon startup." mcpd is a JSON-RPC tool daemon — it does not load models.
llama-server loads models, launched by `start-pbd`/`start-qbd` scripts. The
invariant attributed a security responsibility to the wrong process.

**Fix:** Rewrote INV-7 to attribute verification to the correct components:
- Start scripts (`start-pbd`, `start-qbd`) verify before launching llama-server
- `build.sh` verifies before producing the ISO image
- `first-boot` re-verifies as defense-in-depth

Also fixed two stale references in the same file:
- Quick Reference: `qwen2.5-coder-1.5b-q4.gguf` → `run7_cot_q4km.gguf` (Phase 4 output)
- OOM recovery: `python3 scripts/sft_train.py --low-memory --lr 1e-4 --resume` →
  `LOW_MEM=1 RESUME=1 bash 04_train.sh` (uses script's own LR of 2e-4)

**File:** `CLAUDE.md` (3 edits)

### Fix B: G5.P2 gates wired into controller/ci.sh

**Problem:** Phase 5 was marked "Complete" in CLAUDE.md with "G1–G11 + G5.1–G5.P2b
green." But `dual-brain/controller/ci.sh` stopped at G5.P1d — gates G5.P2a, G5.P2b,
and G5.P2c did not exist. The tests themselves pass (139 tests across 8 files), but
they were never registered as named CI gates.

**Fix:** Added three gate blocks after G5.P1d:
- G5.P2a: `test_openai_backend.py`, `test_verifier.py`
- G5.P2b: `test_daemon.py`, `test_client.py`, `test_protocol.py`
- G5.P2c: `test_presenter_registry.py`, `test_screen_reader_presenter.py`, `test_gtk_presenter.py`

Updated header comment and exit banner to reflect the complete gate range.

**File:** `dual-brain/controller/ci.sh`

### Fix C: G2 tool count check tightened

**Problem:** `src/mcpd/ci.sh` line 59 checked `if [[ "$TOOLS" -lt 20 ]]` — passing
silently with 20 or 21 tools when the spec requires exactly 22. A regression
removing two tool implementations would go undetected.

**Fix:** Changed to `if [[ "$TOOLS" -ne 22 ]]` with message "expected exactly 22."

**File:** `src/mcpd/ci.sh`

## Deferred (with reasoning)

### Issue #4: 04_train.sh uses `[ ]` instead of `[[ ]]` and unquoted expansions

**Location:** `privileged-brain/04_train.sh` lines 21–22, 69–70

**Why deferred:** Phase 4 is complete. The scripts work correctly — the `[ ]` vs
`[[ ]]` difference is style-only (no glob/split risk with quoted vars), and the
unquoted `$FLAG` expansion is a deliberate empty-suppression pattern (flags are
single words or empty). Does not ship in the ISO.

### Issue #5: deploy_to_vm.sh injects unvalidated REMOTE_DIR into ssh commands

**Location:** `dual-brain/scripts/deploy_to_vm.sh` lines 38, 108–115

**Why deferred:** Dev-only deployment script. Does not ship in the ISO. REMOTE_DIR
defaults to `~/dual-brain` and is only overridden by the operator who runs the
script. Low real-world risk. Fix belongs in a future hardening pass.

### Issue #6: Landlock PartiallyEnforced silently accepted

**Location:** `src/mcpd/src/sandbox/landlock.rs` lines 83–86

**Why deferred:** The code uses ABI v1 (minimum, kernel 5.13+). PartiallyEnforced
with v1 on a v1-capable kernel is pathological — it indicates a broken environment,
not a normal degradation path. `NotEnforced` already hard-fails (bail!). The
practical risk is near-zero. Candidate for Phase 7 hardening review.

**To verify later:** On the Phase 6 ISO, run mcpd and confirm Landlock reports
FullyEnforced in the tracing output. If PartiallyEnforced ever appears in the wild,
revisit this decision.

### Issue #8: server.rs uses .expect() and `as u32` cast on pid

**Location:** `src/mcpd/src/server.rs` line 209

**Why deferred:** The JSON schema for `process.inspect` already has `"maximum":
4194304` (PID_MAX_LIMIT). Schema validation runs before dispatch (server.rs lines
186–191). The `.expect("schema-validated")` is guarded by the schema pass, and
4194304 fits in u32 (max 4.2B). Not a real bug.

### Issue #9: deploy_to_vm.sh bundles API keys in tarball

**Location:** `dual-brain/scripts/deploy_to_vm.sh` lines 28–30, 88–96

**Why deferred:** Intentional by design — comment explicitly states keys travel in
the tarball for CI gates G9/G10. Dev tooling only. `gcloud compute scp` uses
encrypted transport. Does not ship in the ISO. Fix belongs in a future hardening
pass (use `gcloud secrets` or SSH session env vars instead).

### Issue #10: OOM recovery LR (1e-4) differs from training script (2e-4)

**Location:** `CLAUDE.md` line 373, `privileged-brain/04_train.sh` line 67

**Status:** Fixed as part of Fix A. The bare `python3` invocation with `--lr 1e-4`
was replaced with `LOW_MEM=1 RESUME=1 bash 04_train.sh`, which uses the script's
own LR. No longer an issue.

### Issue #7: Quick Reference model names stale

**Location:** `CLAUDE.md` lines 429–430

**Status:** Fixed as part of Fix A. Model names now reference `run7_cot_q4km.gguf`
for PB with role comments.

## Verification checklist

- [x] `cargo test` in `src/mcpd/` — 167 tests pass (129 unit + 37 integration + 1 sdnotify)
- [x] P2 test files run — 139 tests pass
- [x] `bash -n` syntax check on both `ci.sh` files — clean
- [x] `grep "mcpd MUST verify" CLAUDE.md` — zero results (attribution fixed)
- [x] `grep "run7_cot" CLAUDE.md` — appears in Phase Status and Quick Reference
- [ ] Run full `dual-brain/controller/ci.sh` on Linux VM (G5.P2a/b/c gates need live run)
- [ ] Confirm Landlock FullyEnforced on Phase 6 ISO (deferred issue #6 watchpoint)
