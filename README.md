# Icebreaker

**A safer, more approachable way to turn everyday intent into Linux actions.**

Icebreaker is an AI-native Ubuntu project being prepared for an open-source
release. It explores a simple idea: a person should be able to ask their
computer for help in plain language without giving an AI an unchecked path to
their files, shell, or network.

Instead of treating a model as an all-powerful agent, Icebreaker makes it one
part of a deliberately constrained system. The result is a bootable Ubuntu
experience, a local privileged model, an auditable controller, and a sandboxed
tool daemon that are designed to disagree safely when something is unclear or
risky.

> **Project status:** Icebreaker is actively developed research and engineering
> software, not a replacement for a production-hardened desktop OS. The latest
> multi-architecture images have passed build-time smoke gates; hardware and
> installer release sign-off remains tracked in
> [`incremental/GROUND_TRUTH.md`](./incremental/GROUND_TRUTH.md). Please test
> it in a VM before trusting it with important data.

## Download the ISO

Replace the two placeholder URLs below when publishing the GitHub Release.
Keep the SHA-256 files beside their matching ISOs so people can verify what
they downloaded.

| Architecture | Best for | Download |
| --- | --- | --- |
| ARM64 | Apple silicon Macs in UTM and ARM hardware | [Download ARM64 ISO](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-arm64.iso) · [SHA-256](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-arm64.iso.sha256) |
| AMD64 | Intel/AMD PCs, QEMU, and VirtualBox | [Download AMD64 ISO](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-amd64.iso) · [SHA-256](https://github.com/OWNER/REPOSITORY/releases/download/v1.0.0/icebreaker-v1.0_OC-amd64.iso.sha256) |

Example verification:

```bash
shasum -a 256 icebreaker-v1.0_OC-arm64.iso
# Compare the result with icebreaker-v1.0_OC-arm64.iso.sha256.
```

## What makes Icebreaker different?

The project is built around one non-negotiable principle: **language models do
not get to become the security boundary.** The controller, schemas, sandbox,
and human approval flow carry that responsibility instead.

```text
You
  │ natural-language request
  ▼
Quarantined Brain ──► validated Intent Object ──► Controller
       no tools                                  │ risk, policy, audit
                                                 ▼
                                    opaque reference for the
                                      Privileged Brain only
                                                 │
                                                 ▼
                                  mcpd ──► constrained Linux tools
                                           Landlock + Seccomp-BPF + COW
```

- **Quarantined Brain (QB):** understands a request and proposes a structured
  intent. It has no execution capability and no MCP tool connection. It can be
  configured to use a cloud provider or a local model.
- **Controller:** is the trusted mediator. It validates the intent schema,
  applies an escalate-only risk policy, asks for approval when required, and
  writes a tamper-evident audit trail.
- **Privileged Brain (PB):** is a local, fine-tuned GGUF model that receives
  only an opaque intent reference—not the user’s original text—and produces
  grammar-constrained MCP calls.
- **mcpd:** is the Rust execution daemon. It communicates over stdio, validates
  every tool parameter, applies Landlock and Seccomp-BPF confinement, and
  creates copy-on-write previews before destructive work outside the user’s
  home directory.

This design is intentionally less magical than a general-purpose autonomous
agent. That is the point: when Icebreaker cannot establish a safe path, it
should stop, explain why, and leave the decision with the person at the
keyboard.

## Start here as a developer

Icebreaker spans Rust, Python, Linux security primitives, model packaging, and
Ubuntu ISO engineering. You do not need to understand all of it on day one.
Pick the layer you care about, run its tests, and follow the contracts at its
boundaries.

### 1. Clone and orient yourself

```bash
git clone https://github.com/a-bhimava/icebreaker.git
cd icebreaker
```

Read these before changing behavior:

1. [`AI_Native_OS_Whitepaper.md`](./AI_Native_OS_Whitepaper.md) — the
   architecture and its eight security invariants.
2. [`AGENTS.md`](./AGENTS.md) — contributor rules, sensitive files, and
   testing expectations.
3. [`docs/IMPLEMENTATION_PLAN.md`](./docs/IMPLEMENTATION_PLAN.md) — build
   order, release gates, and the failure-mode register.
4. [`incremental/GROUND_TRUTH.md`](./incremental/GROUND_TRUTH.md) — the living
   record of real ISO-build and guest-test evidence.

### 2. Work on one layer at a time

| If you want to work on… | Start in… | Run first… |
| --- | --- | --- |
| Safe tool execution and sandboxing | [`src/mcpd/`](./src/mcpd/) | `cargo test --manifest-path src/mcpd/Cargo.toml` |
| Intent validation, orchestration, audit, and policy | [`dual-brain/controller/`](./dual-brain/controller/) | `cd dual-brain && PYTHONPATH=. python3 -m pytest controller/tests/ -x -q` |
| PB training and evaluation | [`privileged-brain/`](./privileged-brain/) | Read its [pipeline guide](./privileged-brain/README.md) first |
| ISO packaging and first boot | [`cx-distro/`](./cx-distro/) and [`incremental/`](./incremental/) | `bash cx-distro/tests/test_build_output.sh --static-only` |

For Python work, use a virtual environment and install the controller in editable
mode:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./dual-brain
```

The complete test suite and full ISO build need Linux. In particular, Landlock,
Seccomp-BPF, live-image assembly, and guest boot gates cannot be meaningfully
validated on macOS alone.

## Build an ISO

The reproducible builder lives in [`cx-distro/`](./cx-distro/). It builds the
Rust daemon, the local inference server, the Python environment, and an Ubuntu
live image in stages. It needs Docker, a Linux host, and verified GGUF weights
listed in [`models/checksums.sha256`](./models/checksums.sha256).

```bash
cd cx-distro
docker build -t icebreaker-build -f Dockerfile.build ..
docker run --privileged -v "$(pwd)/..:/build" icebreaker-build
```

For a release-quality, multi-architecture build, use the documented incremental
workflow and run its gates rather than improvising a one-off image:

```bash
# From the repository root on the Linux build host.
LABEL=v1.0 VN=6 V67_EDITION=oc V67_PROFILE=xfce-frosted \
  bash cx-distro/rebuild/rebuild-v67.sh
```

That workflow deliberately fails early when model hashes, architecture checks,
security harvests, or intent-corpus gates are not healthy. See
[`cx-distro/README.md`](./cx-distro/README.md) and
[`incremental/GROUND_TRUTH.md`](./incremental/GROUND_TRUTH.md) before cutting
an image for others.

## Safety model

Icebreaker’s architecture is not a set of aspirations; it has explicit
invariants that code and build gates are expected to enforce.

| Guardrail | What it means in practice |
| --- | --- |
| Brain isolation | QB has no tools; PB never receives raw user or document text. |
| Schema enforcement | The controller rejects unknown fields and unsafe parameters before dispatch. |
| No daemon listener | `mcpd` uses stdio JSON-RPC and must not expose TCP, UDP, or UNIX listeners. |
| Kernel confinement | Landlock and Seccomp-BPF are applied before the execution path is available. |
| COW for destructive changes | Operations outside a home directory are previewed before they can be committed. |
| Model integrity | Model weights are SHA-256 checked before startup and ISO embedding. |
| Auditable decisions | Accepted, denied, and rejected intents are append-only and hash-chained. |

The complete, authoritative wording is in
[`AI_Native_OS_Whitepaper.md`](./AI_Native_OS_Whitepaper.md) and
[`AGENTS.md`](./AGENTS.md). If a proposed shortcut conflicts with either, the
shortcut is not acceptable.

## Repository map

```text
src/mcpd/                 Rust MCP execution daemon and sandbox
dual-brain/controller/    Trusted Python controller and AI backends
dual-brain/gui_agent/     AT-SPI and application-API automation
dual-brain/rpa_bridge/    Constrained Robot Framework workflows
privileged-brain/         PB data, training, conversion, and evaluation
cx-distro/                Canonical Docker/live-image builder
incremental/              Reproducible ISO workflow, gates, and failure log
models/                   GGUF integrity manifest
docs/                     Architecture, roadmap, and release documentation
```

## Contributing

We would love careful contributors. The most valuable contributions are often
the unglamorous ones: a regression test for a real guest failure, a narrower
sandbox rule, clearer diagnostics, or documentation that keeps the security
model understandable.

- Keep a change focused on one module and one concern.
- Add a regression guard for every bug fix, then reproduce the original failure
  before calling it fixed.
- Treat the controller’s schema boundary, `mcpd`, sandbox code, model hashes,
  and ISO builder as security-sensitive. They need deliberate review.
- Do not weaken a safety check behind a feature flag or “temporary” switch.

Please open an issue to discuss substantial work before investing in a large
patch. For security-sensitive reports, use GitHub’s private security advisory
flow rather than publishing a working exploit in a public issue.

## License

**License selection is still required before public publication.** Add an
OSI-approved `LICENSE` file and replace this note before presenting the
repository as reusable open-source software. A public repository without a
license is visible source code, but it does not grant others permission to use,
modify, or distribute it.

## Thanks

Icebreaker is an ambitious systems project, and it gets better through scrutiny.
If you are here to test it, break it thoughtfully, improve the docs, or build a
safer interface to your computer: welcome. We are glad you are here.
