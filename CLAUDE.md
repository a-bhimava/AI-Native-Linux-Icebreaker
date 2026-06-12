# CLAUDE.md — AI-Native OS Project

> This file is read automatically by Claude Code at session start.  
> It encodes the architectural invariants, security constraints, and anti-patterns for this codebase.  
> **Read this before writing a single line of code or proposing any change.**

---

## What This Project Is

An AI-Native Ubuntu fork in which a locally-running LLM is a first-class OS citizen. Users express intent in natural language; the AI translates it into safe, auditable system operations executed against the Linux kernel.

**Core documents:**
- [`AI_Native_OS_Whitepaper.md`](./AI_Native_OS_Whitepaper.md) — architecture, design decisions, KPIs (the source of truth)
- [`docs/IMPLEMENTATION_PLAN.md`](./docs/IMPLEMENTATION_PLAN.md) — build order, phase gates, failure mode register, risk mitigations

Read both before making any architectural change.

---

## Directory Map

```
/
├── AI_Native_OS_Whitepaper.md     # Definitive architecture reference
├── CLAUDE.md                      # This file
├── docs/
│   └── IMPLEMENTATION_PLAN.md     # Detailed build plan + failure register
├── privileged-brain/              # Fine-tuning pipeline for the Privileged Brain
│   ├── scripts/                   # Python training scripts (sft_train.py, dpo_train.py, etc.)
│   ├── inference/grammar/         # GBNF grammar for constrained decoding
│   ├── data/                      # Training data (synthetic + processed)
│   └── *.sh                       # Shell wrappers for the training pipeline
├── src/
│   └── mcpd/                      # Rust MCP daemon (Phase 1 — COMPLETE, on main)
├── dual-brain/                    # Phase 2 deployable bundle (Python)
│   ├── controller/                #   - Controller package (skeleton in; M2.0+ adds backends, audit, hitl, session, repl)
│   ├── scripts/                   #   - Operator scripts (export_mcpd_catalogue.py, start_pb.sh, start_qb_local.sh)
│   ├── docs/phase2/               #   - Mirror of project Phase 2 planning docs
│   └── README.md                  #   - "tar dual-brain → scp → extract on VM" deploy workflow
├── shell/                         # V1 shell trigger — preserved untouched; coexists with Controller
├── backups/                       # Local snapshots of VM state (e.g. backups/jun4/)
├── cx-distro/                     # ISO build pipeline (NOT YET BUILT — Phase 6)
└── models/
    └── checksums.sha256           # SHA-256 of all GGUF model weight files
```

## Phase Status

| Phase | Status | Module |
|---|---|---|
| Phase 0 | Complete | Env setup, models, MCP handshake |
| Phase 1 | Complete | mcpd Rust daemon (M1.0–M1.10, all exit gates green on Linux) |
| Phase 2 | Complete | Dual-Brain Controller (M2.0–M2.14; gates G1–G11 green; merged to `main`) |
| Phase 3 | Complete (folded into Phase 1) | Landlock + Seccomp-BPF + COW landed alongside the mcpd tools (M1.3 / M1.4 / M1.5); exit criteria met within Phase 1; kept as a heading for whitepaper continuity |
| Phase 4 | Complete | PB finalized: `run7_cot_q4km.gguf` — 100% adversarial refusal, 95.5% grammar-valid MCP, 940 MB, checksummed. run8 continued-tune rejected (safety regression). Pipeline in `privileged-brain/` |
| Phase 5 | Not started (planned) | UX + Graduated Determinism — roadmap in [`docs/phase5_roadmap.md`](./docs/phase5_roadmap.md) |
| Phase 6 | Not started | ISO distribution |
| Phase 7 | Not started | Hardening + release |

**Never start a phase before its predecessors have passed their exit criteria.** See `docs/IMPLEMENTATION_PLAN.md § Go/No-Go Gate Checklist`.

---

## Absolute Architectural Invariants

These are non-negotiable. Violating any of them breaks the security model. **Do not propose changes that contradict them without first updating the whitepaper and getting explicit human sign-off.**

### INV-1: Brain Isolation
```
Quarantined Brain:  ZERO MCP tool connections. ZERO execution capability.
Privileged Brain:   ZERO access to raw user input. ZERO access to external document content.
```
The only information that flows from Quarantined Brain toward Privileged Brain is a structured Intent Object (validated by the Controller) → then reduced to an opaque UUID reference. **Never** let raw text from the Quarantined Brain reach the Privileged Brain.

### INV-2: Controller Schema Enforcement
```
The Controller MUST:
  - Reject any Intent Object with fields outside the defined schema
  - Reject any Intent Object with string values containing shell metacharacters
  - NEVER pass raw text payload between the two brains
  - Pass ONLY opaque reference IDs to the Privileged Brain
```

### INV-3: mcpd Network Isolation
```
mcpd MUST communicate exclusively over stdio pipes.
mcpd MUST NOT open any TCP, UDP, or UNIX socket listener.
CI MUST assert `ss -tlnp` shows zero mcpd processes with open ports.
```

### INV-4: Parameter Validation Before Execution
```
Every MCP tool call parameter MUST be validated against the tool's JSON Schema
before the tool executes. No parameter may contain unescaped shell metacharacters.
Use schema validators, not ad-hoc string checks.
```

### INV-5: No Execution Without Sandboxing
```
Landlock MUST be applied to the execution child process BEFORE it is forked.
Seccomp-BPF MUST be applied in the child process before execve.
If Landlock is unavailable (kernel < 5.13), mcpd MUST exit with a clear error — never run without it.
```

### INV-6: COW Before Destructive Operations
```
Any fs.delete or fs.write OUTSIDE the user's home directory MUST go through
a COW dry-run first. The user must see the dry-run report before the commit.
The Approve button must be disabled for at least 3 seconds after the report is shown.
```

### INV-7: Model Weight Integrity
```
mcpd MUST verify the SHA-256 of each GGUF model file against models/checksums.sha256
at daemon startup. If any checksum fails, mcpd MUST refuse to start.
build.sh MUST verify all checksums before running mksquashfs.
```

### INV-8: Audit Log Integrity
```
The audit log MUST be opened with O_APPEND. It MUST record every intent,
including rejected ones, with timestamp, intent ID, risk level, user, and outcome.
The audit log MUST NOT be writable by the AI models or their inference processes.
```

---

## Security-Critical Files

The following files implement core security mechanisms. **Any PR touching these files requires human review from the module owner — do not approve without reading the change carefully.**

| File | Why It's Sensitive |
|---|---|
| `dual-brain/controller/schema_validation.*` | Schema bypass here = prompt injection succeeds |
| `dual-brain/controller/intent_store.*` | Opaque ID store — any leak of raw text here breaks INV-1 |
| `src/mcpd/tools/fs.rs` | Path traversal, arbitrary write — most dangerous mcpd module |
| `src/mcpd/server.rs` | Network listener check — INV-3 |
| `src/mcpd/sandbox/landlock.rs` | Ruleset definition — too-permissive rules = sandbox escape |
| `src/mcpd/sandbox/seccomp.rs` | Syscall whitelist — missing syscall = escape vector |
| `src/mcpd/sandbox/cow.rs` | Commit atomicity — non-atomic commit = data corruption |
| `inference/grammar/mcp_tool_call.gbnf` | Grammar definition — must stay in sync with mcpd schemas |
| `models/checksums.sha256` | Authoritative hash manifest — tampering = wrong model loaded |
| `cx-distro/build.sh` | ISO builder — checksum verification lives here |

---

## Test-Only Knobs (Production Must NOT Compile These In)

Some mcpd hardening tests need to widen a runtime check without weakening the kernel sandbox underneath. Those hooks live behind cargo features that default to **off**. If you find one in a release binary, that is a build-system bug, not a feature request.

| Feature | Env var | Effect | Verification |
|---|---|---|---|
| `fs-test-roots` | `MCPD_FS_TEST_ROOTS=/abs/path[:/abs/path...]` | Appends extra paths to `tools::fs::default_roots()` so the M1.3 Landlock kernel-enforcement integration test can prove the *kernel* (Landlock) rejects an out-of-root read after userspace `validate()` admits it. Landlock's own allow list is NOT widened by this. | `strings target/release/mcpd \| grep -c MCPD_FS_TEST_ROOTS` must be `0` on any binary that ships outside CI. ci.sh G1 sets `--features fs-test-roots` only for the test build. |

**Rules:**
- Never reference a `fs-test-roots`-style feature from `src/main.rs` or any code on the normal request path.
- Never gate a security check behind a feature — features can only **add** test surfaces, never **remove** production checks.
- New test-only knobs follow the same pattern: cargo feature off by default, env var read inside `#[cfg(feature = "...")]`, listed in this table, verified absent from the production binary by ci.sh.

---

## Forbidden Patterns

**Never write code that does any of the following:**

```python
# FORBIDDEN: shell=True in any subprocess call within mcpd or controller
subprocess.run(user_input, shell=True)  # ❌

# FORBIDDEN: raw user text as a parameter to any MCP tool call
mcp_call("fs.write", {"path": user_message})  # ❌

# FORBIDDEN: skipping COW for destructive operations outside home dir
execute_immediately("fs.delete", "/etc/config")  # ❌

# FORBIDDEN: hardcoded model path without hash verification
load_model("/opt/ainative/models/qwen.gguf")  # ❌ — must verify SHA-256 first

# FORBIDDEN: connecting Quarantined Brain to any MCP tool
quarantined_brain.connect_mcp(mcpd)  # ❌

# FORBIDDEN: passing Quarantined Brain output text directly to Privileged Brain
privileged_brain.execute(quarantined_brain.last_response)  # ❌

# FORBIDDEN: Landlock/Seccomp applied after fork in child process
child_process.apply_seccomp()  # ❌ — must be applied in parent before fork

# FORBIDDEN: TCP listener in mcpd
TcpListener::bind("0.0.0.0:8080")  // ❌ in mcpd
```

```rust
// FORBIDDEN: unwrap() on security-critical paths — always handle errors explicitly
let path = validate_path(input).unwrap(); // ❌

// FORBIDDEN: string interpolation into shell commands
let cmd = format!("rm -rf {}", user_path); // ❌

// FORBIDDEN: using std::process::Command with shell=true equivalent
Command::new("sh").arg("-c").arg(user_input) // ❌
```

---

## Engineering Best Practices

These are the standing engineering norms for this codebase. They sit alongside the
Architectural Invariants (which are non-negotiable) and encode *how* we build so the
invariants survive contact with real features. Follow them by default; deviate only with a
documented reason and human sign-off.

### BP-1: Plug-and-Play by Default
Anything we expect to swap or upgrade — models, brain backends, the risk classifier, the
Tier-2 reviewer, the HITL presenter, the audit sink, the keymap — goes through a **registry +
config selector**, never a hardcoded branch. Follow the established pattern (`model_registry.py`
+ `catalogue.toml`, the `BrainBackend` registry, the `HitlPresenter` ABC). Adding a new
implementation must not require editing call sites.

### BP-2: Feature-Flag New Behavior, Default to the Safe/Current Path
Every new behavior ships behind a flag (TOML knob, env var, or cargo feature) that defaults to
**off / current behavior**, so it can roll forward and back independently. **Never gate a
security check behind a flag** — flags may only *add* surface, never *remove* a check (see
§ Test-Only Knobs). Config additions must be backward-compatible: an older config keeps working.

### BP-3: Sanitize All Externally-Influenced Text Before a Terminal
Any string derived from a model, a document, or user input is **untrusted display data**. Strip
ANSI escapes and C0/C1 control characters and neutralize `\r`/`\n` before rendering it to a TTY
or log. Raw model/document text rendered to a terminal is a prompt-spoofing vector (it can
redraw an approval prompt). Sanitize at the boundary, not ad-hoc per call site.

### BP-4: Human-Gate Integrity
A human approval gate must reflect a **deliberate, present-tense** decision: flush the input
buffer before reading (no pre-buffered/pasted bytes may decide), require a single intentional
keypress, enforce the approval lockout on a monotonic clock (INV-6), default to **deny** on
timeout / non-TTY / interrupt, and reserve an always-available deny key (Esc).

### BP-5: Risk Classification Is Escalate-Only
An ML/LLM/policy pass may **raise** an operation's risk tier; it may **never lower** the
rule-based floor. Encode this as a runtime assertion, not a convention. A classifier that emits
malformed or unexpected output must fail toward *more* oversight, never less.

### BP-6: Security Decisions Use Structured Facts, Not Model Free-Text
The fields a human (or gate) relies on to make a security decision — action, resolved target,
dry-run diff — come from validated schema and the sandbox COW preview. Model-authored free-text
(a "reason", a summary) may be shown as clearly-delimited narration but must never be the basis
of the risk label or the decision.

### BP-7: Audit Is Complete, Append-Only, and Tamper-Evident
Record **every** intent including rejected/denied ones, with full provenance (INV-8). Open with
O_APPEND, `fsync` each line, and hash-chain entries so edits/deletions are detectable. The audit
log must never be writable by the AI models or their inference processes; prefer a
privilege-separated sink for non-repudiation.

### BP-8: Secret Hygiene End-to-End
Config references secret **names** (env vars / credential handles), never values. Secrets never
land in logs, audit lines, REPL history, or error messages — redact by key name, value pattern,
**and** entropy. Scrub the environment of child processes (e.g. the `mcpd` subprocess inherits
no API keys). Prefer systemd credentials / a keyring over plain env vars in deployment.

### BP-9: Least Privilege & Defense in Depth
Every layer assumes the layer above it failed: userspace `validate()` *and* kernel Landlock
*and* seccomp *and* COW *and* the human gate. Never collapse two layers into one "because the
other one already checks it." Grant the narrowest filesystem/syscall/network scope that works;
widen only with documented justification.

### BP-10: Resource Governance — Fail Safe, Never Silently Drop
Bound untrusted input size, turn/request rate, and per-session cost. On limit breach or
backend failure, **deny/abort + audit + surface to the user** — never silently swallow, and
never let an injection or runaway loop become a cost/DoS amplifier.

### BP-11: Configurable UX with Safe Defaults and Preserved Discoverability
User-facing interaction (keybindings, color, presenter) is config-driven with sensible defaults
(numeric + mnemonic keys). Customization must not cost discoverability — a help affordance
(`?`) always shows the *active* bindings. Validate user config (no duplicate/ambiguous
bindings; reserved keys stay reserved).

### BP-12: Test the Boundary, Not the Happy Path
Any two components that must agree (e.g. a GBNF grammar and a JSON schema) get a **differential
test**. Every trust boundary gets an **adversarial corpus**. New security behavior adds a CI
gate to `ci.sh` and, for security-critical files, a regression guard. Live integration catches
what mocks cannot — add a smoke test against the real binary/provider before declaring done.

---

## Agent Workflow Rules

These rules apply to any AI coding agent (Claude Code, Codex, Gemini CLI, etc.) working on this codebase.

### WF-1: Branch Isolation
Always work in a feature branch. **Never commit directly to `main`.**
```bash
git checkout -b feature/phase1-mcpd-fs-module
```

### WF-2: Narrow Scope Per Task
Each agent task must have exactly one module scope. Do not ask an agent to:
- "Implement Phase 3" (too broad)
- "Build mcpd and the Controller" (two modules)

Do ask an agent to:
- "Implement `src/mcpd/tools/fs.rs` with path validation and unit tests"
- "Write the JSON Schema for the Intent Object in `controller/schemas/intent.json`"

### WF-3: Run Tests Before Done
Before marking any task complete, run:
```bash
cargo test                          # Rust unit + integration tests
pytest tests/                       # Python tests
python tests/check_invariants.py    # Architecture invariant assertions
```
If tests fail, fix them — do not mark the task done.

### WF-4: Architecture Is the Source of Truth
If your generated code disagrees with `AI_Native_OS_Whitepaper.md` or `docs/IMPLEMENTATION_PLAN.md`, **fix the code**, not the documents. If you genuinely believe a document is wrong, flag it for human review — do not silently deviate.

### WF-5: One Human Owner Per Module
| Module | Owner |
|---|---|
| `src/mcpd/` | Rust engineer |
| `dual-brain/controller/` | Backend engineer |
| `privileged-brain/` | ML engineer |
| `cx-distro/` | DevOps engineer |
| `inference/grammar/` | ML engineer |

Every PR must be reviewed by the module owner before merging.

### WF-6: Security File Review
Any change touching the files listed in **Security-Critical Files** above requires explicit `LGTM` from the module owner AND a second human reviewer. No exceptions.

### WF-7: No Cross-Module Changes in One PR
A single PR must not span mcpd + Controller + fine-tuning pipeline. Each module has its own PR. This makes review tractable and limits blast radius.

---

## Running the Fine-Tuning Pipeline (Phase 4)

The training pipeline is the most mature part of the codebase. The run order is:

```bash
cd privileged-brain/

# 1. Environment setup (~10 min)
bash 01_setup.sh

# 2. Download baseline datasets
bash 02_get_data.sh

# 3. Generate synthetic training data (requires ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
bash 03_generate_synthetic.sh

# 4. Train — run overnight (5–9 hours on Apple M4 / GPU)
bash 04_train.sh

# 5. Fuse LoRA adapter, quantize to GGUF, import to Ollama
bash 05_convert_and_import.sh

# 6. Start inference server
bash 06_start_inference.sh

# 7. Evaluate against baseline
bash 07_evaluate.sh
```

**Before the overnight training run, verify:**
- [ ] Checkpoint output directory exists and is writable: `ls -la training/adapters/sft/`
- [ ] At least 20GB free disk space for checkpoints: `df -h .`
- [ ] Run one test step to confirm no OOM: `python3 scripts/sft_train.py --epochs 1 --low-memory` (stop after first checkpoint)

**If training OOMs:**
```bash
python3 scripts/sft_train.py --low-memory --lr 1e-4 --resume
```

**If you need to resume from a checkpoint:**
```bash
bash 04_train.sh  # Already includes --resume logic in the script
```

---

## Key Invariant: Model Checksums

Every time a model file is created, modified, or moved, update `models/checksums.sha256`:

```bash
sha256sum models/*.gguf >> models/checksums.sha256
```

The CI pipeline verifies these checksums. A missing or mismatched checksum will fail the build and block the PR.

---

## Latency Budget (Non-Negotiable)

| Layer | Budget |
|---|---|
| Landlock ruleset application | <1ms |
| Seccomp-BPF filter installation | <1ms |
| COW overlay mount | <5ms |
| MCP JSON-RPC round-trip | <5ms |
| **Total security overhead** | **<10ms** |
| **Full pipeline (Tier 0/1)** | **<100ms p95** |

If a change you make causes any of these to exceed budget, resolve the regression before merging.

---

## Common Mistakes and How to Avoid Them

| Mistake | Prevention |
|---|---|
| Adding a TCP listener to mcpd for "easier debugging" | Use a Unix socket with strict file permissions for dev tooling only; never TCP |
| Passing user text to Privileged Brain "just for logging" | Logs are read by humans, not the model. Keep the separation strict. |
| Using `shell=True` because it's easier | Always use `Command::new()` with explicit arg list. If you need a pipeline, compose tools in Rust. |
| Skipping the data validation pass before training | Dangerous commands in training data = dangerous model. The pass is in the Phase 4 checklist for a reason. |
| Testing only in QEMU before claiming the ISO works | QEMU masks hardware compatibility bugs. Always test on physical hardware before marking Phase 6 done. |
| Widening Landlock rulesets "temporarily" | There is no temporary in a shipped OS. Start minimal; widen only after documented justification. |
| Approving a PR that touches sandbox code without reading it | Security-critical files require two human reviewers. This is enforced by branch protection rules. |
| Training SFT and DPO from different base model versions | Pin the model hash. `fuse_lora.py` asserts it at load time. |

---

## Quick Reference: Critical Commands

```bash
# Verify both models load
llama-server --model models/phi4-mini-q4.gguf --port 8080 &
llama-server --model models/qwen2.5-coder-1.5b-q4.gguf --port 8081 &

# Test MCP handshake
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | ./mcpd

# Verify mcpd has no network listeners (must return empty)
ss -tlnp | grep mcpd

# Run all tests
cargo test && pytest tests/

# Check model checksums
sha256sum --check models/checksums.sha256

# Evaluate fine-tuned model
cd privileged-brain && bash 07_evaluate.sh

# Build ISO (Phase 6 only — all prior phases must be complete)
cd cx-distro && bash build.sh

# Boot ISO in QEMU for testing
qemu-system-x86_64 -m 8G -boot d -cdrom ainative.iso -enable-kvm
```

---

*Last updated: June 2026 (Phase 2 Controller merged; Phase 5 planning — added the Engineering Best Practices section (BP-1…BP-12) and reconciled the Phase Status table). Update this file whenever an architectural decision changes, a new invariant is established, or a phase gate passes.*
