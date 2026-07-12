# Icebreaker v1.0-rc1

> **Template status.** This file is a TEMPLATE. Blanks marked
> `{{ VAR_NAME }}` are populated by Scope I's ISO build (`make -C
> incremental release-notes`) with the actual SHA-256s, dates, and GPG
> key ID. Do NOT hand-edit the blanks — the build script fills them
> from the artifacts it produces so no drift is possible between the
> shipped ISO and its release notes.

**Release date:** {{ RELEASE_DATE }}
**Git tag:** `v1.0-rc1` (`{{ TAG_COMMIT_SHA }}`)
**Signing key:** GPG `{{ GPG_KEY_ID }}` — see § Verification

**ISO SHA-256** (verify BEFORE booting per F-14):
- `icebreaker-v1.0-rc1-amd64.iso`: `{{ ISO_SHA256_AMD64 }}`
- `icebreaker-v1.0-rc1-arm64.iso`: `{{ ISO_SHA256_ARM64 }}`

---

## Overview

Icebreaker is an AI-native Ubuntu 24.04 fork in which a locally-running LLM is a first-class OS citizen. Users express intent in natural language; a dual-brain pipeline (untrusted Quarantined Brain generating a structured intent; sandboxed Privileged Brain executing it) translates the intent into safe, auditable system operations against the Linux kernel through a hardened MCP daemon.

`v1.0-rc1` is the **first tagged release**. Prior versions (v0 through v6.65) were incremental development builds; the v1.0 label reflects a hardened error surface, a named regression lock for every logged failure, and a live-verified corpus of user workflows. Compare to Talos Linux (immutable Kubernetes OS) and Bottlerocket (container-focused base): Icebreaker's differentiator is the dual-brain security model and the graduated-determinism HITL layer, not the packaging model.

---

## Highlights

1. **Dual-brain design** (INV-1). The Quarantined Brain has zero MCP tool connections and zero execution capability; the Privileged Brain never sees raw user text. Only opaque UUID references cross the boundary.
2. **mcpd sandbox stack** (INV-5). Landlock filesystem confinement + Seccomp-BPF syscall allowlist + copy-on-write staging for destructive operations, applied in the parent BEFORE fork.
3. **Textual TUI + AT-SPI GUI Agent + Robot Framework RPA Bridge**. Terminal, desktop, and web-automation modalities share the same intent pipeline and audit trail.
4. **Control Center** — 8 GTK4 pages driven by JSON Schema. Every user-tunable knob is config-first (not a hidden constant), enforced by the `G23 no-new-knobs` CI gate.
5. **Curated MCP allowlist scaffold** ready for Phase 7 activation. Third-party MCP servers (Playwright, MS Graph, Google Workspace) will land through a pinned + SHA-checked + sandboxed allowlist rather than an unrestricted registry.

---

## Security posture

The eight architectural invariants enforced in `v1.0-rc1`:

| ID | Invariant | Enforcement site |
|----|-----------|------------------|
| INV-1 | Brain isolation | `dual-brain/controller/intent_store.py` + prompt architecture |
| INV-2 | Controller schema enforcement | `controller/schemas/intent.json` + `intent_schema.py::validate()` |
| INV-3 | mcpd stdio-only (no network) | `ci.sh` G-check + `src/mcpd/server.rs` |
| INV-4 | Parameter validation before execution | `mcpd/schemas/*.json` + JSON Schema validators |
| INV-5 | No execution without sandboxing | `mcpd/sandbox/{landlock,seccomp,cow}.rs` |
| INV-6 | COW before destructive operations | `mcpd/sandbox/cow.rs` + 3-second HITL lockout |
| INV-7 | Model weight integrity | `models/checksums.sha256` + `start-pbd` / `start-qbd` verify |
| INV-8 | Audit log integrity | `controller/logger.py` + O_APPEND + hash-chain |

Sandbox tiers:

| Tier | Examples | HITL behavior |
|------|----------|---------------|
| 0 (read-only) | `fs.list`, `system.status` | Auto-approve |
| 1 (bounded write, home) | `fs.write` under `$HOME` | Auto-approve with audit |
| 2 (system changes) | `package.install`, `service.restart` | Prompt required |
| 3 (destructive) | `fs.delete` outside home | Prompt + COW dry-run + 3-s lockout |

Audit hash-chain (INV-8): every intent (approved AND denied) is written with `O_APPEND`, `fsync`-per-line, and hash-chained. Edits or deletions are detectable. The audit log is not writable by the AI models or their inference processes.

---

## Component versions

| Component | Version | SHA-256 | Notes |
|-----------|---------|---------|-------|
| `mcpd` (amd64) | `{{ MCPD_AMD64_VERSION }}` | `{{ MCPD_AMD64_SHA256 }}` | Rust static build |
| `mcpd` (arm64) | `{{ MCPD_ARM64_VERSION }}` | `{{ MCPD_ARM64_SHA256 }}` | Rust static build; F-55 fixed |
| `llama-server` (amd64) | `{{ LLAMA_AMD64_VERSION }}` | `{{ LLAMA_AMD64_SHA256 }}` | Static; no shared-lib deps (F-21) |
| `llama-server` (arm64) | `{{ LLAMA_ARM64_VERSION }}` | `{{ LLAMA_ARM64_SHA256 }}` | Static; NEON |
| Privileged Brain GGUF | `run7_cot_q4km` | `{{ PB_GGUF_SHA256 }}` | 940 MB; 100% adversarial refusal; 95.5% grammar-valid MCP |
| Quarantined Brain (default) | `gemini-2.5-flash` | N/A (cloud) | Fallback chain configurable per Scope B |
| Ubuntu base | `24.04.{{ UBUNTU_POINT }} LTS` | `{{ UBUNTU_SQUASHFS_SHA256 }}` | debootstrap from `archive.ubuntu.com` |
| Kernel | `linux-generic 6.8.{{ KERNEL_POINT }}` | — | Stock Canonical build |
| Python | `3.12.{{ PY_POINT }}` | — | Ubuntu default |
| Rust | `{{ RUST_VERSION }}` | — | Build-host toolchain |

All hashes verified by `first-boot` on every ISO boot (INV-7). A mismatch aborts the boot with a clear error rather than falling back to unverified binaries.

---

## Supported platforms

- **amd64**: **AVX2 minimum** (Haswell 2013+ / x86-64-v3 baseline; per R10). Verified in QEMU-KVM and VirtualBox with EFI boot. First-boot detects CPU capability and refuses to start the inference server on unsupported CPUs with a clear "AVX2 required" banner.
- **arm64**: Verified on Apple Silicon under UTM **Virtualize** (native), not Emulate. Cross-arch TCG builds are supported for CI but are NOT gate-worthy — the F-55 incident (`faccessat` syscall missing from the arm64 seccomp allowlist because it was authored against amd64 numbers) demonstrated that TCG fakes the syscall table and hides real bugs. Rule R8-arm64 codifies this: every arch needs its own harvest-gate pass on native hardware or true-virt emulation.

Known-good hardware for arm64: Apple M-series (M1 / M2 / M3 / M4) via UTM Virtualize.

---

## Installation & first boot

1. Verify the ISO SHA-256 (§ Verification).
2. Write to USB or attach to your hypervisor:
   - UTM (Apple Silicon): create a Virtualize VM, attach ISO as CD, boot.
   - QEMU: `qemu-system-x86_64 -m 8G -boot d -cdrom icebreaker-v1.0-rc1-amd64.iso -enable-kvm`
   - VirtualBox: create EFI VM, attach as live CD (EFI mode required).
3. On first boot, the checksum verifier (INV-7 defense-in-depth) re-hashes every GGUF model file. Boot aborts with a red banner on mismatch.
4. First-boot walks the user through:
   - Setting the QB (Gemini) API key via the Control Center Models page or the `ib-setup-key` polkit helper.
   - Optional: enabling the Anthropic / OpenAI fallback backends.
   - Verifying model checksums pass.
5. `--safe-mode` CLI flag disables all AI features and boots to a plain XFCE desktop for recovery scenarios.

See `cx-distro/README.md` for hypervisor-specific settings.

---

## Upgrade path

N/A — first tagged release. This section is reserved as the template for future releases.

For upgrades within the `v1.0-rc*` line (patch-level ISOs — same tag, different `+N` suffix per R-Scope-I), replace the ISO, re-verify SHA-256, boot. User config in `~/.config/icebreaker/controller.toml` is backward-compatible per Scope B; new fields resolve to their documented defaults.

---

## Known issues

- **F-56** (`ResultEvent` NameError on some `fs.read` paths derived from `context.cwd`) — status at `v1.0-rc1` build time: **{{ F56_STATUS }}**. If the fresh ISO reproduces it, a follow-up patch on the `v1.0-rc1+N` label lands the fix + `test_f56_*` module.
- **Verifier truncation on multi-page prompts** (F-53 mitigation applied). Scope B raised max_tokens ceilings to 1M and exposed them in Control Center → Models. If a legitimate turn is rejected with `verifier call failed: BrainTruncationError`, raise `qb.gemini.max_tokens` in the Control Center; the fix landed but longer prompts can still exceed the default.
- **UTM Emulate mode on arm64 host** (amd64 guest emulated via Rosetta) is not gate-worthy — F-55 class of bugs is invisible under TCG-adjacent emulation. Use UTM Virtualize (native arm64) or a real amd64 host.
- Additional issues are logged in `incremental/GROUND_TRUTH.md § 7 Failure Log` as they surface. Cross-reference before filing.

---

## Deferred to post-v1.0

The following are Phase 7 or Phase 8 milestones, out of scope for `v1.0-rc1`. Roadmap in `docs/ROADMAP_Phase7_Phase8_2026-07-09.md`.

**Phase 7 — Broader tools + hardening**:
- M7.1 Trust-tier taxonomy + `x-icebreaker-trust` schema field on every mcpd tool
- M7.2 Curated external MCP allowlist (Playwright / MS Graph / Google Workspace) with SHA-pinned + sandboxed spawns
- M7.3 Playwright MCP for browser automation
- M7.4 MS Graph + Google Workspace MCP for Office / Docs
- M7.5 Analytics + Vega-Lite chart tool + `fs.find`
- M7.6 End-to-end hardening + external penetration test
- M7.7 v1.0 GPG-signed release

**Phase 8 — Autonomous agent loops**:
- M8.1 Multi-turn Goal state
- M8.2 Self-verification loop
- M8.3 Sub-agent roles with tier ceilings
- M8.4 LangGraph adapter
- M8.5 Fleet-safe observability

See `AI_Native_OS_Whitepaper.md § 10.5` for the 2026-07-10 audit that shaped the scope of `v1.0-rc1`.

---

## Verification

**ISO integrity** (before booting):

```
sha256sum icebreaker-v1.0-rc1-amd64.iso  # must match this document
sha256sum icebreaker-v1.0-rc1-arm64.iso
```

**GPG signature verification**:

```
gpg --recv-keys {{ GPG_KEY_ID }}
gpg --verify icebreaker-v1.0-rc1-amd64.iso.sig icebreaker-v1.0-rc1-amd64.iso
gpg --verify icebreaker-v1.0-rc1-arm64.iso.sig icebreaker-v1.0-rc1-arm64.iso
```

**Model integrity** (inside the running system):

```
sudo -u icebreaker sha256sum -c /var/lib/icebreaker/models/checksums.sha256
```

**First-boot re-verification is automatic** (INV-7 defense-in-depth). If any hash is wrong, boot aborts with a red banner rather than falling back to unverified binaries.

---

## Credits

Aditya (`colabuser23@gmail.com`) — architecture, implementation, and audit-driven completion of Phases 0 through 6. AI coding assistants used across the sprint under human review; every PR gated by tests + CI + human LGTM.

---

## Appendix A: Bug fixes v6.6 → v1.0-rc1

Every F-xx entry between v6.6 (2026-07-08) and `v1.0-rc1`, with subsystem tag + R14 regression-lock reference. Full detail in `incremental/GROUND_TRUTH.md § 7 Failure Log`.

| ID | Subsystem | Summary | Regression lock (R14) |
|----|-----------|---------|-----------------------|
| F-41 | `prompts/qb_gemini.txt`, `intent.json` | CSV column-of-ones: PB fabricated sample data instead of using user's literal bytes | `corpus/intent_corpus.json` category=`write-content` + `broad-os.content.*` rows |
| F-42 | `main.py` | `_cot` NameError on every `system.unsupported` intent | `test_intent_corpus.py::test_smalltalk_and_meta_rows_route_to_unsupported` + `test_f48_unsupported_cot_state.py` |
| F-43 | `main.py` | `DO_NOT_EMIT` placeholder UUID leak (streaming path) | `test_f43_f47b_intent_id_normalization.py` |
| F-44 | `main.py`, `verifier.py` | Verifier single-shot rejection under network flake | `test_verifier_retry_mode.py` |
| F-45 | `hitl.py` | `signal.signal` from worker thread crashed every Tier 3 HITL | `test_hitl.py::test_ask_from_worker_thread_no_crash` |
| F-46 | `prompts/qb_gemini.txt` | `find` intent silently mapped to `fs.list` | `corpus/intent_corpus.json` category=`search-UNSUPPORTED` |
| F-47b | `main.py` | Non-streaming path missed the F-43 UUID override | `test_f43_f47b_intent_id_normalization.py::test_normalize_call_site_present_in_non_streaming_path` |
| F-48 | `main.py` | `step_state="unsupported"` crashed `CotEvent` construction | `test_f48_unsupported_cot_state.py` |
| F-49 | `verifier.py`, `config.py` | Verifier retry strategy — configurable retry_mode | `test_verifier_retry_mode.py` (19 parametrized) |
| F-50 | `terminal/app.py`, `.desktop` files | Sticky-NL race + GTK4 DRI3 invisible-window | v6.62 daemon fix + xfce4 desktop polish |
| F-51 | `incremental/versions/v2.manifest` | Stale-venv reseed shipping fixes-missing venv | v2.manifest F-51 marker set (grows per PR) |
| F-52 | `main.py` | Backend response_schema rejected placeholder UUIDs before daemon-side normalize could run | `test_f52_backend_schema_relax.py` |
| F-53 | `verifier.py`, `main.py` | Generic "verifier call failed" hid `BrainTruncationError` root cause | `test_f53_verifier_truncation_surface.py` |
| F-54 | `config.py`, GUI | 2048-shaped magic numbers as invisible dependencies on the tested workload | Scope B backward-compat + G23 no-new-knobs gate |
| F-55 | `src/mcpd/sandbox/seccomp.rs` | arm64 seccomp missing `faccessat` (48) — journalctl SIGSYS on `service.logs` | Scope G rebuild + F-51 marker `F55-arm64-facc` |
| F-56 | `main.py` | `ResultEvent` NameError on `fs.read` paths via `context.cwd` | Verified on fresh `v1.0-rc1` (see § Known issues) |

The July 2026 completion sprint (PRs #27–#30, Scopes A–F) landed 2329 tests, 63 → 0 silent exception swallows, and a named regression lock for every entry above. Rule **R14 — Every F-xx fix carries a named regression lock** codifies this going forward: no Failure Log entry is closed without a live test that fails on regression. See `incremental/GROUND_TRUTH.md § 2 The Rules` and `AI_Native_OS_Whitepaper.md § 10.5`.
