# Icebreaker

## The AI-native operating system built around a security boundary no prompt can talk its way across.

**The model that understands you cannot execute. The model that executes never
sees your conversation.**

Most AI systems begin with: *How much can the model do?*

Icebreaker begins with: *What must the model never be able to do?*

Icebreaker is a bootable Ubuntu 24.04 system that turns plain-English intent
into real Linux and desktop actions through two isolated AI brains, a typed
control plane, native kernel confinement, human approval, and tamper-evident
audit.

Ask it to inspect a failing service, install a package, organize files, operate
a desktop app, or explain what the machine is doing. Icebreaker can act—but no
general-purpose model is ever one prompt away from root.

**Two brains. One hardened path to the kernel. No ambient authority.**

## The USP: separate understanding from privilege

```text
You
 │
 ▼
QUARANTINED BRAIN                 Understands language and untrusted content
No privileged tools              Cannot execute system actions
 │
 │  schema-validated Intent Object
 ▼
CONTROLLER                        Risk floor · policy · verifier · HITL · audit
 │
 │  opaque intent reference
 ▼
PRIVILEGED BRAIN                  Local 1.5B execution specialist
No raw conversation              Grammar-constrained MCP calls
 │
 ▼
mcpd                              JSON Schema validation · stdio only
 │
 ├─ Landlock filesystem boundary
 ├─ Seccomp-BPF syscall allowlist
 ├─ Copy-on-Write dry run
 └─ Append-only, hash-chained audit
 │
 ▼
Linux
```

A prompt injection may confuse the model reading a webpage or document. It
still does not gain the privileged execution surface. The model allowed to
propose privileged calls is local, narrow, constrained, and blind to the
original conversation.

Icebreaker does not ask an LLM to behave securely. It **builds a system in
which unsafe authority is unavailable.**

## What that unlocks

| Value | Proof in the system |
| --- | --- |
| **Real work, not command suggestions** | 23 governed system tools for files, processes, services, packages, networking, and machine state |
| **Desktop agency with informed consent** | 24 GUI/RPA tools, vision grounding, HiDPI-aware coordinates, AT-SPI, and annotated action previews |
| **Reversible destruction** | Copy-on-Write staging shows the real filesystem diff before protected changes are committed |
| **Oversight without approval fatigue** | Read-only work flows automatically; risk can only escalate; Tier 3 adds a consequence report and three-second lockout |
| **A local privileged specialist** | `run7_cot_q4km.gguf`: 940 MB Q4_K_M, 100% adversarial refusal and 95.5% grammar-valid MCP output on the recorded evaluation |
| **Forensics, not vibes** | Approved, denied, and rejected intents enter an append-only, `fsync`-per-line, hash-chained audit log |
| **An OS, not an install script** | Reproducible Ubuntu images with embedded model verification, first boot, recovery mode, and ARM64/AMD64 builds |

## Why this is different from other “AI operating systems”

“AI OS” currently describes several useful—but very different—ideas. This is a
positioning comparison, not a security verdict on other projects.

| Approach | Representative projects | What it optimizes for | Icebreaker’s difference |
| --- | --- | --- | --- |
| Natural-language shell and desktop agents | [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter), [NatShell](https://github.com/Barent/natshell) | Let a model plan and operate an existing computer | A typed, sandboxed boundary sits between language understanding and privileged execution |
| Agent operating-system runtimes | [AIOS](https://github.com/agiresearch/AIOS) | Schedule agent memory, context, storage, and tools | Icebreaker governs real kernel and desktop actions as a bootable end-user OS |
| AI-first Linux distributions | [CX Linux](https://github.com/cxlinux-ai/cx-distro), [aiOS](https://github.com/thoerner/ai-os) | Integrate inference and AI experiences into Linux | Dual-brain isolation, COW previews, graduated approval, and tamper-evident audit are the product core |
| **Icebreaker** | This repository | **A secure intent-to-kernel control plane** | **Capability without handing untrusted model context ambient authority** |

## This already goes far beyond the terminal

- **OpenCode and Textual AI terminals** with streaming progress, cancellation,
  structured execution states, and human approval.
- **Vision-grounded desktop control** that sees interactive elements, previews
  the target, and performs bounded mouse or keyboard actions.
- **Native adapters for LibreOffice, Firefox, and GNOME Files**, with AT-SPI as
  the general accessibility-tree fallback.
- **Allowlisted Robot Framework workflows** behind a sandbox, time budget, and
  HITL policy.
- **Plan-and-execute orchestration** with LangGraph-compatible state and
  persistent sessions.
- **Pluggable cloud and local backends** through a registry and LiteLLM, while
  privileged inference remains local through llama.cpp.
- **GTK4/LibAdwaita control surfaces** for models, behavior, limits, tools,
  status, themes, onboarding, errors, and audit review.
- **One declarative tool catalogue** driving prompts, schemas, risk
  classification, tests, and drift detection.

## Proof, not promises

| Surface | Evidence |
| --- | --- |
| Privileged model | 940 MB local Qwen 2.5 Coder fine-tune; grammar-constrained through GBNF |
| Execution | 23 schema-validated system tools over stdio-only JSON-RPC |
| Desktop | 20 GUI tools + 4 RPA tools exposed through the `iceui` MCP surface |
| Defense in depth | Controller schema, escalate-only risk, Landlock, Seccomp-BPF, COW, HITL, model hashes, audit chain |
| Regression discipline | Thousands of tests, adversarial corpora, CI gates through G25, syscall harvests, QEMU gates, and UTM test plans |
| Distribution | Hybrid ARM64 and AMD64 Ubuntu ISOs with architecture-specific `mcpd` and `llama-server` builds |

Every real guest failure is recorded with its symptom, root cause, fix, release,
and named regression guard in
[`incremental/GROUND_TRUTH.md`](./incremental/GROUND_TRUTH.md). That ledger is
not cleaned up for marketing. It is how the project stops the same bug from
shipping twice.

## Download

> Release placeholders: replace `OWNER/REPOSITORY` when the assets are
> published.

| Image | Runs on | ISO | Verify |
| --- | --- | --- | --- |
| **ARM64** | Apple silicon in UTM, ARM hardware | [Download ISO](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-arm64.iso) | [SHA-256](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-arm64.iso.sha256) |
| **AMD64** | Intel/AMD PCs, QEMU, VirtualBox | [Download ISO](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-amd64.iso) | [SHA-256](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-amd64.iso.sha256) |

```bash
shasum -a 256 icebreaker-v1.0_OC-arm64.iso
```

## Under the hood

```text
src/mcpd/                 Rust execution daemon, schemas, Landlock, Seccomp, COW
dual-brain/controller/    Intent pipeline, policy, HITL, verifier, audit, sessions
dual-brain/gui_agent/     AT-SPI, app APIs, screenshots, vision, input synthesis
dual-brain/rpa_bridge/    Sandboxed, allowlisted Robot Framework automation
dual-brain/gui/           GTK4 chat, Control Center, onboarding, audit, HITL
dual-brain/terminal/      Textual AI terminal and companion panel
privileged-brain/         SFT/DPO data, training, conversion, and evaluation
cx-distro/                Canonical Docker-based ISO builder
incremental/              Multi-arch images, smoke gates, QEMU gates, failure log
```

The authoritative technical contracts are:

1. [Architecture whitepaper](./AI_Native_OS_Whitepaper.md)
2. [Implementation plan](./docs/implementation_plan.md)
3. [Security and contributor rules](./CLAUDE.md)
4. [Ground-truth failure ledger](./incremental/GROUND_TRUTH.md)

## Start developing

Linux is the reference environment. macOS can run static and most Python tests,
but it cannot prove Landlock, Seccomp, live-image, or guest-boot behavior.

```bash
git clone https://github.com/a-bhimava/icebreaker.git
cd icebreaker

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./dual-brain

(cd dual-brain && PYTHONPATH=. python3 -m pytest controller/tests/ -x -q)
cargo test --manifest-path src/mcpd/Cargo.toml
bash cx-distro/tests/test_build_output.sh --static-only
```

## Build the ISO

The canonical pipeline compiles both architectures, builds `llama-server`,
packages Python and the model, assembles Ubuntu, runs the security harvest and
intent corpus, and emits checksummed hybrid images.

```bash
LABEL=v1.0 VN=6 V67_EDITION=oc V67_PROFILE=xfce-frosted \
  bash cx-distro/rebuild/rebuild-v67.sh
```

Read [`cx-distro/README.md`](./cx-distro/README.md) first. A clean
multi-architecture build is intentionally expensive and fails early on an
unhealthy hash, architecture, sandbox harvest, or corpus gate.

## Status

The architecture and major product surfaces are implemented. The latest ARM64
and AMD64 images pass the build-time smoke gates. Hardware, installer, and
release-signing gates remain tracked in the
[ground-truth ledger](./incremental/GROUND_TRUTH.md).

Treat current images as release candidates: use a VM, keep backups, and do not
trust them with irreplaceable data yet.

## Contributing

Make the system more capable **and** more constrained. Keep changes inside one
module, add a regression test for every bug, and expect extra review for
schemas, sandboxes, model hashes, the Controller boundary, and the ISO builder.

Discuss large changes in an issue first. Report exploitable security problems
through GitHub’s private security-advisory flow.

## License

An OSI-approved license still needs to be selected before public release. Until
a `LICENSE` file exists, this repository is source-visible but does not grant
permission to use, modify, or redistribute the code.
