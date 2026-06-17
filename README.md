# Icebreaker — AI-Native OS

An Ubuntu fork where a locally-running LLM is a first-class OS citizen. Users express
intent in natural language; the AI translates it into safe, auditable system operations
executed against the Linux kernel.

## Architecture

```
User ──► Quarantined Brain (QB) ──► Intent Object ──► Controller ──► Privileged Brain (PB) ──► mcpd ──► kernel
              │                          │                  │                │                    │
         Zero exec               Schema-validated    Risk classifier    UUID only           Landlock +
         Zero MCP                 INV-2 enforced      Tier 0–3 HITL     (no raw text)       Seccomp-BPF
```

**Two AI brains, strictly isolated (INV-1):**
- **Quarantined Brain** — parses natural language into structured Intent Objects. Zero
  execution capability, zero MCP tool access. Cloud (Gemini/OpenAI) or local GGUF.
- **Privileged Brain** — translates opaque intent UUIDs into MCP tool calls. Zero access
  to raw user input. Local fine-tuned GGUF (`run7_cot_q4km.gguf`).

**Controller** — trusted Python mediator. Schema-validates intents, classifies risk
(Tier 0–3), gates destructive ops with human-in-the-loop approval (3s lockout),
passes only opaque UUIDs to the PB, audits everything.

**mcpd** — Rust MCP daemon. 22 tools via JSON-RPC over stdio only (no network).
Sandboxed: Landlock + Seccomp-BPF + COW snapshots before destructive writes.

## Project Status (June 2026)

| Phase | Status |
|---|---|
| Phase 0 — Environment & models | Complete |
| Phase 1 — mcpd (Rust daemon) | Complete |
| Phase 2 — Dual-Brain Controller | Complete |
| Phase 3 — Kernel sandboxing | Complete (folded into Phase 1) |
| Phase 4 — Fine-tune Privileged Brain | Complete |
| Phase 5 — UX + Graduated Determinism | Complete |
| Phase 6 — ISO distribution | **Complete** (PRs #15–21 merged) |
| Phase 7 — Hardening + release | Not started |

**Test count:** 1446 passing (25 skipped).

## Directory Map

| Directory | What | Owner |
|---|---|---|
| `src/mcpd/` | Rust MCP daemon (22 tools, Landlock/Seccomp/COW sandbox) | Rust engineer |
| `dual-brain/controller/` | Python Controller (orchestration, schema validation, HITL, audit) | Backend engineer |
| `privileged-brain/` | PB fine-tuning pipeline (SFT/DPO, data generation, evaluation) | ML engineer |
| `cx-distro/` | ISO build pipeline (live-build, Docker, 6-stage `build.sh`) | DevOps engineer |
| `models/` | GGUF model weights + `checksums.sha256` | — |
| `shell/` | V1 shell trigger (preserved, coexists with Controller) | — |
| `docs/` | Implementation plans, architecture docs | — |

## Key Documents

| Document | Purpose |
|---|---|
| [`AI_Native_OS_Whitepaper.md`](./AI_Native_OS_Whitepaper.md) | Architecture, design decisions, KPIs (source of truth) |
| [`CLAUDE.md`](./CLAUDE.md) | Invariants INV-1–8, security-critical files, agent workflow rules |
| [`docs/implementation_plan.md`](./docs/implementation_plan.md) | Master build plan, phase gates, failure mode register |
| [`docs/Archive/phase6_implementation_plan.md`](./docs/Archive/phase6_implementation_plan.md) | Phase 6 PR order, acceptance criteria, CI gates (archived — phase complete) |

## Quick Start

```bash
# Run the Controller (dev mode)
cd dual-brain
pip install -e .
python3 -m controller "what is the system status"

# Run tests
cd dual-brain && PYTHONPATH=. python3 -m pytest controller/tests/ -x -q

# Build the ISO (requires Docker + Linux)
cd cx-distro
docker build -t icebreaker-build -f Dockerfile.build ..
docker run --privileged -v "$(pwd)/..:/build" icebreaker-build

# Static ISO checks (runs on macOS)
bash cx-distro/tests/test_build_output.sh --static-only
```

## Security Model

Eight architectural invariants (INV-1 through INV-8) are non-negotiable. See
[`CLAUDE.md`](./CLAUDE.md) for the full list. Key constraints:

- **INV-1:** Brain isolation — QB has zero exec, PB has zero raw user input
- **INV-3:** mcpd communicates over stdio only — no TCP/UDP/UNIX listeners
- **INV-5:** Landlock applied before fork, Seccomp before execve
- **INV-6:** COW dry-run before destructive ops, 3s approval lockout
- **INV-7:** SHA-256 verification of all model weights before loading or embedding
- **INV-8:** Append-only, hash-chained audit log for every intent
