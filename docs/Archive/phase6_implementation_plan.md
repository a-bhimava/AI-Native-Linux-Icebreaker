# Phase 6 — ISO Distribution: Implementation Plan

> Companion to the Phase 6 plan (`plans/joyful-bouncing-wolf.md`). This file is the
> build-order checklist: file-by-file changes, test plan, gates, PR sequence, rollout.
> Update it as work lands; treat the plan as the spec, this as the playbook.
>
> **Prerequisites:** Phases 0–5 complete (1347 tests, PRs #9–#14 merged to `main`).

## §0 — Status snapshot (update on every PR)

| Milestone | Status | PR | Notes |
|---|---|---|---|
| M6.1 mcpd sd_notify | ✅ Merged | PR #15 (`8bb8444`) | `sd-notify` crate, `READY=1` datagram |
| M6.2 UNIX transport + PB config | ✅ Merged | PR #16 (`04fbb8a`) | `_unix_http.py`, `pb_transport` config fix |
| M6.3 systemd units | ✅ Merged | PR #17 (`540c567`) | Service users, socket perms, parallel startup, 43 tests |
| M6.4 Python packaging + config layering | ✅ Merged | PR #18 (`7063e26`) | PEP 621 `pyproject.toml`, 2-tier config merge, schema gaps |
| M6.5 cx-distro scaffold + build.sh | ✅ Merged | PR #19 (`ed786c4`) | 6-stage build.sh, Dockerfile, distro config, 19 static + 7 pytest |
| M6.6 First-boot + safe mode | ✅ Merged | PR #20 (`45a8d94`) | Systemd oneshot first-boot (INV-7, health poll, group add), safe-mode diagnostics, `--safe-mode` CLI |
| M6.7 CI gates + QEMU test | ✅ Merged | PR #21 (`565962f`) | G12–G15 CI gates, QEMU boot checklist, config layering integration test |

## §1 — Invariants we must not break

INV-1 (brain isolation), INV-2 (schema-only intents), INV-3 (mcpd stdio-only), INV-4
(per-tool schema), INV-5 (sandbox before fork), INV-6 (≥3 s lockout, monotonic clock),
INV-7 (model checksums — **primary focus of this phase**), INV-8 (audit
append-only/fsync/0o640/not-model-writable).

Engineering best practices BP-1…BP-12 from `CLAUDE.md` apply.

WF-5 — `cx-distro/` is owned by DevOps engineer; `dual-brain/controller/` by Backend
engineer; `src/mcpd/` by Rust engineer. WF-6 — every PR touching security-critical
files needs two-human-reviewer LGTM. WF-7 — one module per PR (PRs #15–#16 are
separate modules; PRs #17–#18 touch `dual-brain/` but different subsystems).

## §2 — Architecture decisions

Nine architecture decisions (ADRs) govern Phase 6. Full rationale in the plan file;
summary here for quick reference.

| ADR | Decision | Key constraint |
|---|---|---|
| ADR-1 | FHS-compliant paths | `/etc/icebreaker/`, `/usr/share/icebreaker/`, `/var/lib/icebreaker/models/`, `/usr/libexec/icebreaker/` |
| ADR-2 | **Live ISO, not installer** | `live-build` produces squashfs live session. No preseed, no autoinstall, no d-i. Install-to-disk deferred to Phase 7. |
| ADR-3 | Vendored venv at `/opt/icebreaker/venv/` | `pip install .` from `pyproject.toml`. CLI wrapper calls venv Python. No system-wide pip pollution. |
| ADR-4 | PB transport configurable | `pb_transport` in `RunConfig` + config schema. `_build_pb()` reads config instead of hardcoding `"http"`. |
| ADR-5 | llama-server built from pinned commit | `LLAMA_CPP_COMMIT` hash file. Binary SHA-256 recorded. `build.sh` verifies. |
| ADR-6 | Service users + socket permissions | `_icebreaker_pb`, `_icebreaker_qb`, group `icebreaker-users`. Socket mode 0660. `sysusers.d` + `tmpfiles.d`. |
| ADR-7 | Parallel PB/QB startup | No `After=` between pbd and qbd. Boot budget: <60s mid-range, <120s min-spec. |
| ADR-8 | 2-tier config layering | System `/etc/icebreaker/controller.toml` + user XDG. Section-level merge. Drop-in dirs deferred to Phase 7. |
| ADR-9 | Models bind-mounted into Docker | Docker image = toolchain only. Models never in build context. `build.sh` verifies checksums before Docker. |

## §3 — PR order

```
PR #15 (mcpd sd_notify)    PR #16 (unix transport + PB fix)
         \                   /
          \                 /
           PR #17 (systemd units)
                  |
           PR #18 (pyproject.toml + config layering)
                  |
           PR #19 (cx-distro scaffold + build.sh)
                  |
           PR #20 (first-boot + safe mode)
                  |
           PR #21 (CI gates + QEMU test)
```

PRs #15 and #16 proceed **in parallel** (Rust vs Python, different modules).

Why this order: sd_notify (#15) and UNIX transport (#16) are leaf dependencies with no
overlap. systemd units (#17) reference both. Python packaging (#18) is needed before
the ISO build can `pip install .`. cx-distro (#19) is the bulk of the phase — it
consumes all prior PRs. First-boot (#20) and CI gates (#21) are validation layers on
top.

## §4 — Files touched per PR

### PR #15 — mcpd sd_notify (`feature/phase6-mcpd-sdnotify`)

**Module:** `src/mcpd/` | ~15 lines production + ~20 lines test

| Action | File | Change |
|---|---|---|
| MOD | `src/mcpd/Cargo.toml` | Add `sd-notify = "0.4"` under `[target.'cfg(target_os = "linux")'.dependencies]` |
| MOD | `src/mcpd/src/main.rs` | After `sandbox::apply()`, before `server::run_stdio_server()`: `#[cfg(target_os = "linux")] { sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok(); }` |
| NEW | `src/mcpd/tests/test_sdnotify.rs` | Set `NOTIFY_SOCKET` to a temp UDS, start mcpd init, verify `READY=1` datagram received |

**Tests (~3):** sd_notify sends datagram on Linux, no-op on non-Linux, INV-3 preserved.

---

### PR #16 — UNIX transport + PB config fix (`feature/phase6-unix-transport`)

**Module:** `dual-brain/controller/` | ~120 lines production + ~80 lines test

Addresses two issues: (1) PB transport hardcoded to `"http"` in `__main__.py:43`,
(2) `transport="unix"` raises `BrainConfigError` stub in `llama_local_backend.py:58-63`.

| Action | File | Change |
|---|---|---|
| NEW | `dual-brain/controller/backends/_unix_http.py` | ~50 lines: `UnixHTTPConnection(http.client.HTTPConnection)` overrides `connect()` for `socket.AF_UNIX`. `UnixHTTPAdapter(requests.adapters.HTTPAdapter)` returns custom connection pool. Parses `unix:///path/to.sock`. No new pip dependency. |
| MOD | `dual-brain/controller/backends/llama_local_backend.py` | Replace `BrainConfigError` stub (lines 58–63): if `transport == "unix"`, parse endpoint, mount `UnixHTTPAdapter`, update health check URL. `_stream_provider()` and `_call_provider()` use `self._session` unchanged. ~30 lines. |
| MOD | `dual-brain/controller/config.py` | Add `pb_transport: str = "http"` to `RunConfig` (~line 146). Add `pb_transport=section.get("pb_transport", "http")` in `_build_run_config()` (~line 411). |
| MOD | `dual-brain/controller/schemas/controller_config.json` | Add `"pb_transport": {"type": "string", "enum": ["http", "unix"]}` to `run` properties. |
| MOD | `dual-brain/controller/__main__.py` | Line 43: `transport="http"` → `transport=cfg.run.pb_transport`. 1-line fix. |
| MOD | `dual-brain/controller/controller.toml.example` | Add commented `# pb_transport = "http"` in `[run]` section. |
| NEW | `dual-brain/controller/tests/test_unix_http.py` | Connection creation, endpoint parsing, adapter mounting, mock socket roundtrip, error on missing socket, error on malformed endpoint. |
| MOD | `dual-brain/controller/tests/test_llama_local_backend.py` | `transport="unix"` construction with mock socket, PB reads `pb_transport` from config, `transport="http"` regression. |

**Tests (~18):**
- `_unix_http.py`: connection, parsing, adapter, roundtrip, errors (~8)
- Backend: unix construction, http regression, PB transport config, health check (~6)
- Config: `pb_transport` loads, defaults to `"http"`, schema validates `"unix"` (~4)

---

### PR #17 — systemd units (`feature/phase6-systemd-units`)

**Module:** `dual-brain/controller/systemd/` + `dual-brain/scripts/` | ~250 lines

| Action | File | Purpose |
|---|---|---|
| NEW | `dual-brain/controller/systemd/icebreaker-pbd.service` | PB llama-server. `User=_icebreaker_pb`. `Type=exec`. `RuntimeDirectory=icebreaker`. `MemoryMax=4G`. `ProtectSystem=strict`. `NoNewPrivileges=yes`. No `After=` on qbd (parallel, ADR-7). |
| NEW | `dual-brain/controller/systemd/icebreaker-qbd.service` | QB llama-server. Same hardening. `User=_icebreaker_qb`. Separate cgroup. |
| NEW | `dual-brain/controller/systemd/icebreaker-mcpd@.service` | Per-user mcpd. `Type=notify`. `NotifyAccess=main`. `ProtectSystem=strict`. `NoNewPrivileges=yes`. |
| MOD | `dual-brain/controller/systemd/icebreaker-controller.service` | Add `After=icebreaker-pbd.service icebreaker-qbd.service`. Update `ExecStart` to venv Python. |
| MOD | `dual-brain/controller/systemd/icebreaker-controller.socket` | Update `ListenStream` to `/run/user/%U/icebreaker/controller.sock`. |
| NEW | `dual-brain/controller/systemd/icebreaker.tmpfiles.d.conf` | `d /run/icebreaker 2775 root icebreaker-users -` |
| NEW | `dual-brain/controller/systemd/icebreaker.sysusers.d.conf` | Service users: `_icebreaker_pb`, `_icebreaker_qb`. Group: `icebreaker-users`. Memberships. |
| NEW | `dual-brain/scripts/start-pbd` | Sources `locations.env`. Resolves model via registry. Binds llama-server to UNIX socket `/run/icebreaker/pbd.sock`. Mode 0660. |
| NEW | `dual-brain/scripts/start-qbd` | Same for QB. Socket at `/run/icebreaker/qbd.sock`. Includes `--grammar-file`. |

**Tests (~8):**
- `systemd-analyze verify` passes for all units
- Unit ordering: controller `After=` pbd+qbd; pbd and qbd have no ordering between them
- All units have `NoNewPrivileges=yes` and `ProtectSystem=strict`
- mcpd has `Type=notify` + `NotifyAccess=main`
- `RuntimeDirectory=icebreaker` present in pbd/qbd
- sysusers.d creates correct users/group
- tmpfiles.d sets correct permissions
- start-pbd/start-qbd are executable and pass shellcheck

---

### PR #18 — Python packaging + config layering (`feature/phase6-python-packaging-and-config`)

**Module:** `dual-brain/` | ~150 lines production + ~100 lines test

| Action | File | Change |
|---|---|---|
| NEW | `dual-brain/pyproject.toml` | PEP 621. `name=icebreaker-controller`. `python_requires>=3.10`. Dependencies from `requirements.txt`. Setuptools backend. |
| MOD | `dual-brain/controller/config.py` | (1) `load_layered()` (~60 lines): system `/etc/icebreaker/controller.toml` → user XDG. Section-level merge (user section replaces entire system section). (2) Auto-detect distro paths: if `/usr/libexec/icebreaker/mcpd` exists, default `mcpd_binary` to it. |
| MOD | `dual-brain/controller/schemas/controller_config.json` | Ensure `pb_transport` in run section (from #16). Add `ephemeral_history` boolean. Add `cost` and `limits` top-level sections (already in dataclasses, missing from schema). |
| MOD | `dual-brain/controller/tests/test_config.py` | Layering tests, packaging tests. |

**Tests (~20):**
- Config layering: system-only, user-only, system+user merge, section-level replacement,
  missing system path falls back (~10)
- Distro path detection: mcpd binary auto-detect, schema dir auto-detect (~3)
- Schema additions: ephemeral_history validates, cost/limits sections validate (~4)
- Packaging: pyproject.toml valid, `pip install .` succeeds in temp venv (~3)

---

### PR #19 — cx-distro scaffold + build.sh (`feature/phase6-cx-distro`)

**Module:** `cx-distro/` (new) | ~600 lines

This is the bulk of Phase 6. Creates the ISO build infrastructure.

```
cx-distro/
├── build.sh                              # SECURITY-CRITICAL (INV-7)
├── Makefile                              # make iso, make clean, make verify
├── Dockerfile.build                      # Ubuntu 24.04 + live-build + Rust + llama.cpp
├── LLAMA_CPP_COMMIT                      # Pinned commit hash
├── README.md                             # Build/verify/test instructions
├── config/
│   ├── package-lists/
│   │   └── icebreaker.list.chroot        # Pinned apt packages
│   ├── includes.chroot/
│   │   ├── etc/icebreaker/               # System config
│   │   ├── etc/systemd/system/           # Units from PR #17
│   │   ├── etc/tmpfiles.d/              
│   │   ├── etc/sysusers.d/             
│   │   ├── usr/bin/icebreaker            # CLI wrapper
│   │   ├── usr/libexec/icebreaker/       # Binaries (staged by build.sh)
│   │   ├── usr/share/icebreaker/         # Catalogue, grammars, schemas, prompts
│   │   ├── opt/icebreaker/venv/          # Python venv (created by build.sh)
│   │   └── var/lib/icebreaker/models/    # GGUFs + checksums (staged by build.sh)
│   ├── hooks/
│   │   └── 0100-icebreaker-setup.hook.chroot
│   └── bootloaders/grub/grub.cfg
└── tests/
    ├── test_build_checksums.sh           # INV-7 (8 cases)
    ├── test_systemd_units.sh
    ├── test_paths.sh                     # FHS layout + permissions
    ├── test_venv.sh                      # Venv integrity
    └── test_qemu_boot.sh                 # QEMU smoke (manual)
```

**`build.sh` phases (SECURITY-CRITICAL — INV-7 enforcement):**

1. **Prerequisites** — verify `live-build`, `debootstrap`. Verify Linux or Docker.
2. **Model checksum verification** — `sha256sum --check` against `models/checksums.sha256`.
   Exit 1 on any mismatch or missing file. Non-optional, non-skippable.
3. **mcpd binary verification** — `strings $binary | grep -c MCPD_FS_TEST_ROOTS` must be
   `0`. Rejects test-only builds per CLAUDE.md § Test-Only Knobs.
4. **llama-server verification** — verify binary exists and SHA-256 matches recorded hash.
   If missing, build from pinned commit inside Docker.
5. **Python venv creation** — `pip install dual-brain/` into
   `/opt/icebreaker/venv/`. Verify `python3 -m controller --help` exits 0.
6. **File staging** — copy models, binaries, config, schemas, grammars, prompts, systemd
   units into `config/includes.chroot/` at FHS paths.
7. **live-build** — `lb config --distribution noble --architectures amd64 --binary-images iso-hybrid`
   then `lb build`. No preseed. No installer.
8. **ISO checksum** — `sha256sum output/*.iso > output/sha256sums.txt`.

**Dockerfile.build:** Ubuntu 24.04, `live-build`, `debootstrap`, Python 3 + venv,
Rust toolchain, llama.cpp from pinned commit. Models bind-mounted at runtime (ADR-9).

**Tests (~12):**
- `test_build_checksums.sh`: correct pass, wrong checksum fail, missing GGUF fail,
  missing checksums.sha256 fail, test-feature mcpd rejected, llama-server checksum
  checked, empty models dir fail, partial models fail (~8)
- `test_venv.sh`: venv Python exists, `python3 -m controller --help` exits 0, imports
  succeed (~4)

---

### PR #20 — First-boot + safe mode (`feature/phase6-first-boot`)

**Module:** `cx-distro/` + `dual-brain/controller/` | ~150 lines

| Action | File | Purpose |
|---|---|---|
| NEW | `cx-distro/config/includes.chroot/usr/libexec/icebreaker/first-boot` | (1) verify model checksums (INV-7 defense-in-depth), (2) start pbd+qbd in parallel (ADR-7), (3) poll health endpoints, (4) add user to `icebreaker-users`, (5) drop sentinel `.first-boot-complete`. |
| NEW | `cx-distro/config/includes.chroot/etc/systemd/system/icebreaker-first-boot.service` | `Type=oneshot`. `ConditionPathExists=!/var/lib/icebreaker/.first-boot-complete`. Runs once. |
| NEW | `cx-distro/config/includes.chroot/usr/libexec/icebreaker/safe-mode` | Recovery: verify checksums, check journals, start services individually with verbose logging, offer config reset. |
| MOD | `dual-brain/controller/__main__.py` | `--safe-mode` flag that execs the safe-mode script (or prints "not available in dev" if script absent). |

**Tests (~5):**
- First-boot sentinel prevents re-run
- Checksum failure logged with clear error message
- Safe mode script executable and passes shellcheck
- `--safe-mode` flag recognized by argparse
- First-boot service has correct `ConditionPathExists`

---

### PR #21 — CI gates + QEMU test (`feature/phase6-ci-gates`)

**Module:** `cx-distro/tests/` + CI | ~200 lines

| Action | File | Purpose |
|---|---|---|
| NEW | `cx-distro/tests/test_paths.sh` | FHS layout inside chroot: files exist, permissions correct |
| NEW | `cx-distro/tests/test_qemu_boot.sh` | QEMU boot smoke: login, `icebreaker --help`, `systemctl is-active`. Manual. |
| NEW | `cx-distro/ci.sh` | G12 (checksums), G13 (systemd verify), G14 (path layout), G15 (venv). |
| MOD | `dual-brain/controller/ci.sh` | Reference G12–G15 (optional, only if `cx-distro/` exists). |
| NEW | `cx-distro/tests/test_config_layering.sh` | Integration: temp system+user config → verify merged output. |

**Tests (~10):**
- Path layout validation (~5)
- Config layering integration (~3)
- QEMU boot smoke (manual)
- Gate runner exit codes

## §5 — CI gates (Phase 6)

After the existing G1–G11 + G5.x block in `dual-brain/controller/ci.sh`:

```bash
# G12 — build.sh checksum verification (INV-7)
bash cx-distro/tests/test_build_checksums.sh

# G13 — systemd unit validation
bash cx-distro/tests/test_systemd_units.sh

# G14 — FHS path layout
bash cx-distro/tests/test_paths.sh

# G15 — Python venv integrity
bash cx-distro/tests/test_venv.sh
```

G12 is the **security-critical** gate: 8 test cases covering correct checksums,
tampered models, missing files, test-feature mcpd rejection, and llama-server
provenance. It implements INV-7 at the build level.

## §6 — Test budget

| PR | New Tests | Notes |
|----|-----------|-------|
| #15 mcpd sd_notify | ~3 | Rust integration |
| #16 unix transport + PB | ~18 | `_unix_http.py`, backend, config, PB transport |
| #17 systemd units | ~8 | Shell: `systemd-analyze`, ordering, hardening, shellcheck |
| #18 packaging + config | ~20 | Layering, schema, auto-detect, pyproject.toml |
| #19 cx-distro | ~12 | Checksum verification (8 cases), venv integrity |
| #20 first-boot | ~5 | Sentinel, error logging, safe-mode, argparse |
| #21 CI gates | ~10 | Paths, config integration, QEMU |
| **Total new** | **~76** | |

Running total: 1347 + ~76 = **~1423 tests**.

## §7 — Distro configuration

System default config shipped at `/etc/icebreaker/controller.toml`:

```toml
[qb]
backend = "local"

[qb.local]
model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
draft_model_id = "qwen-2.5-coder-0.5b-instruct-q4_k_m"
endpoint = "unix:///run/icebreaker/qbd.sock"
transport = "unix"
grammar_path = "/usr/share/icebreaker/grammars/qb_intent.gbnf"
max_tokens = 512
timeout_seconds = 30

[run]
mcpd_binary = "/usr/libexec/icebreaker/mcpd"
audit_log = "/var/log/icebreaker/controller-audit.log"
pb_endpoint = "unix:///run/icebreaker/pbd.sock"
pb_transport = "unix"
pb_model_id = "run7_cot"
mcpd_schemas_dir = "/usr/share/icebreaker/schemas"

[paths]
catalogue_path = "/usr/share/icebreaker/catalogue.toml"
model_search_dirs = ["/var/lib/icebreaker/models", "~/.local/share/icebreaker/models"]

[prompts]
prompts_dir = "/usr/share/icebreaker/prompts"
```

CLI wrapper at `/usr/bin/icebreaker`:
```bash
#!/usr/bin/env bash
exec /opt/icebreaker/venv/bin/python3 -m controller "$@"
```

## §8 — Rollout

1. Each PR is a feature branch off `main`. No commits directly on `main`.
2. PR title format: `phase6(scope): description` (e.g. `phase6(mcpd-sdnotify): ...`).
3. PR body: **no Claude attribution** (no `Co-Authored-By:`, no "Generated with Claude
   Code" footer). Include a "Risk & rollback" section.
4. After merge, update §0 status table in this file.
5. After all PRs land, tag `phase6-complete` and write the closeout note in
   `docs/implementation_plan.md`.

## §9 — Hardware test matrix

| Config | Spec | Boot Target |
|--------|------|-------------|
| Low-end | i5/Ryzen 5, 16GB, no GPU, SATA SSD | <120s to AI-ready |
| Mid-range | i7/Ryzen 7, 32GB, RTX 3060, NVMe | <60s to AI-ready |
| Server | Xeon/EPYC, 64GB+, T4, NVMe | <60s to AI-ready |

Each config: boot to login, `icebreaker "system status"` returns valid response,
`systemctl is-active icebreaker-pbd icebreaker-qbd` shows active, adversarial suite
passes.

## §10 — Out-of-scope (explicit, deferred to Phase 7)

| Item | Reason |
|------|--------|
| Install-to-disk (Calamares) | Phase 6 is live ISO; installer is Phase 7 |
| Drop-in config directories (`*.d/`) | Phase 6 does 2-tier; drop-ins are Phase 7 |
| OSTree immutable updates | Phase 6 uses dpkg in venv; OSTree is Phase 7 |
| AppArmor profiles | Phase 6 uses systemd hardening; custom profiles Phase 7 |
| GPG-signed ISO | Phase 6 has SHA-256 manifests; GPG signing Phase 7 |
| Penetration testing | Phase 7 per implementation plan |
| Man pages | Phase 7 documentation |
| Socket activation for mcpd | Phase 6 uses Controller-spawned subprocess |
| Privilege-separated audit sink | Phase 6 uses `/var/log/icebreaker/` with perms |
| Multi-user concurrent sessions | Phase 6 validates single-user |

## §11 — Risk register (Phase 6 specific)

| # | Risk | Mitigation |
|---|------|------------|
| P6-R1 | `build.sh` checksum bypass | Fail-hard. Security-critical file review. CI gate G12 (8 test cases). |
| P6-R2 | systemd ordering race | `Type=notify` + `sd_notify`. PB/QB parallel. First-boot validates. |
| P6-R3 | Non-reproducible build | `Dockerfile.build` + pinned llama.cpp commit + pinned apt versions. |
| P6-R4 | QEMU-only testing | 3-config hardware matrix. Manual bare-metal log required for gate. |
| P6-R5 | UNIX transport regression | `transport="http"` path preserved. Both paths tested (18 tests). |
| P6-R6 | Config layering breaks users | 2-tier only (simple). `load(explicit_path)` unchanged. Regression tests. |
| P6-R7 | PB uses wrong transport | `_build_pb()` reads `cfg.run.pb_transport`. Tested. No hardcoded value. |
| P6-R8 | llama-server unverified | Built from pinned commit. SHA-256 recorded. `build.sh` verifies. |
| P6-R9 | Python not importable in ISO | Venv at `/opt/icebreaker/venv/`. `test_venv.sh` verifies imports. |
| P6-R10 | Socket permission denied | `tmpfiles.d` + `sysusers.d` + first-boot adds user to group. |

## §12 — Acceptance criteria

### PR #15 — mcpd sd_notify

- sd_notify sends `READY=1` datagram on Linux.
- No-op on non-Linux (compile-time gate).
- INV-3 preserved: no TCP/UDP/UNIX listeners opened by mcpd.
- `sd-notify` is a Linux-only dependency.

### PR #16 — UNIX transport + PB config

- `_unix_http.py` parses `unix:///path/to.sock` correctly.
- `LlamaCppLocalBackend(transport="unix")` constructs without error.
- `LlamaCppLocalBackend(transport="http")` behavior unchanged (regression).
- `_build_pb()` reads `cfg.run.pb_transport` (not hardcoded).
- Config without `pb_transport` defaults to `"http"` (backward compat).
- Schema validates `"unix"` and `"http"` for `pb_transport`.

### PR #17 — systemd units

- `systemd-analyze verify` passes for all units.
- pbd and qbd start in parallel (no ordering between them).
- Controller starts after pbd + qbd (`After=` present).
- All units have `NoNewPrivileges=yes` and `ProtectSystem=strict`.
- mcpd template has `Type=notify`.
- sysusers.d/tmpfiles.d configs are syntactically valid.
- start-pbd/start-qbd pass shellcheck.

### PR #18 — Python packaging + config layering

- `pip install .` from `dual-brain/` succeeds in a fresh venv.
- `python3 -m controller --help` works from the installed venv.
- System-only config loads. User-only config loads.
- System+user merge: user `[qb]` replaces entire system `[qb]`.
- Missing `/etc/icebreaker/` falls back to XDG gracefully.
- Older configs without new sections load without error.

### PR #19 — cx-distro scaffold

- `build.sh` exits 1 on tampered model checksum.
- `build.sh` exits 1 on missing GGUF file.
- `build.sh` rejects mcpd binary containing `MCPD_FS_TEST_ROOTS`.
- `build.sh` verifies llama-server SHA-256.
- Venv `python3 -m controller --help` exits 0 inside chroot.
- `lb build` produces a bootable ISO (when run on Linux with models).

### PR #20 — First-boot + safe mode

- Sentinel `.first-boot-complete` prevents re-run.
- Checksum failure during first-boot logged with recovery guidance.
- `--safe-mode` recognized by argparse.
- Safe-mode script passes shellcheck.

### PR #21 — CI gates

- G12–G15 pass when `cx-distro/` exists and artifacts are staged.
- QEMU boot smoke: login → `icebreaker --help` → `systemctl is-active` (manual).
- Config layering integration test produces expected merged output.

### "Phase 6 complete" aggregate

All seven PR acceptance sections above, plus:

- [ ] ISO boots on 3 bare-metal hardware configs (documented with timing)
- [ ] Boot-to-AI-ready: <60s on mid-range, <120s on minimum-spec
- [ ] Adversarial test suite (75 payloads) passes inside ISO environment
- [ ] All 1347+ existing tests pass inside ISO venv
- [ ] ISO size <4 GB
- [ ] `docs/phase6_implementation_plan.md` §0 updated to reflect all milestones as ✅
- [ ] One human reviewer + module owner LGTM per PR (WF-6)

## §13 — Update protocol for this file

- On every PR merge: update §0 status row, set the PR column, append the merge SHA in
  parens.
- On every milestone closeout: write a 3-bullet closeout note at the bottom of §14
  (what shipped, what's deferred, guidance for next collaborator).
- Do NOT edit the plan file to reflect status — status lives here.

## §14 — Closeout notes (update as PRs land)

### PR #15 — mcpd sd_notify (`8bb8444`, June 2026)

- **Shipped:** `sd-notify` crate dependency, `sd_notify::notify_ready()` call in mcpd's
  `run_stdio_server()` after successful init. Compile-time gated (`#[cfg(target_os = "linux")]`).
  Code review fixes from devils-advocate analysis also landed (doc reconciliation, CI gate wiring).
- **Deferred:** Nothing.
- **Next:** PR #16 (UNIX transport) was developed in parallel.

### PR #16 — UNIX transport + PB config (`04fbb8a`, June 2026)

- **Shipped:** `_unix_http.py` HTTP-over-AF_UNIX adapter, `pb_transport` field in `RunConfig`
  schema + `config.py`, `_build_pb()` reads transport from config instead of hardcoding `"http"`.
  Backward-compatible: missing `pb_transport` defaults to `"http"`.
- **Deferred:** Nothing.
- **Next:** PR #17 (systemd units) depends on both #15 and #16.

### PR #17 — systemd units (`540c567`, June 2026)

- **Shipped:** Five systemd units (`icebreaker-controller.service`, `icebreaker-pbd.service`,
  `icebreaker-qbd.service`, `icebreaker-mcpd@.service`, `icebreaker-controller.socket`). Service
  users (`_icebreaker_pb`, `_icebreaker_qb`) via `sysusers.d`, runtime dirs via `tmpfiles.d`.
  Socket perms 0660, group `icebreaker-users`. PB/QB start in parallel (no `After=` between them).
  `start-pbd` and `start-qbd` wrapper scripts with checksum verification (INV-7). 43 new tests.
- **Deferred:** Socket activation (systemd `Accept=yes` for mcpd) — Phase 7.
- **Next:** PR #18 (Python packaging) must land before cx-distro can `pip install .`.

### PR #18 — Python packaging + config layering (`7063e26`, June 2026)

- **Shipped:** PEP 621 `pyproject.toml` replacing `setup.py`. 2-tier config layering: system
  `/etc/icebreaker/controller.toml` + user `~/.config/icebreaker/controller.toml` with
  section-level merge (`load_layered()`). Schema gap fixes: added `pb_transport`, `daemon`,
  `hitl`, `session`, `prompts`, `paths` sections to `controller_config.json` schema. 95 new tests.
- **Deferred:** Drop-in config dirs (`/etc/icebreaker/controller.toml.d/`) — Phase 7.
- **Next:** PR #19 (cx-distro scaffold) consumes the installable package.

### PR #19 — cx-distro scaffold + build.sh (`ed786c4`, June 2026)

- **Shipped:** `cx-distro/` directory with Dockerized 6-stage `build.sh` (preflight → mcpd →
  llama-server → venv → chroot → ISO). `Dockerfile.build` (Ubuntu noble + live-build + Rust +
  cmake + Python). Pin files (`LLAMA_CPP_COMMIT`, `UBUNTU_BASE`). Production `controller.toml`
  (UNIX sockets, FHS paths, correct catalogue model IDs — `pb_model_id` uses the canonical
  catalogue key `qwen-2.5-coder-1.5b-instruct-q4_k_m`, not the dev-default file stem `run7_cot`).
  Pure `KEY=VALUE` `locations.env` for systemd `EnvironmentFile`. POSIX sh CLI wrapper. Chroot
  hook enabling services. `--skip-to=N` and `--no-models` flags for fast iteration. INV-7:
  SHA-256 verification of all GGUF models before embedding (uses `basename` to handle absolute
  paths in `checksums.sha256`). 19 static shell checks + 7 pytest cases. 1442 total tests (up
  from 1347).
- **Deferred:** Preseed / first-boot wizard (stub `install.cfg` created; full implementation PR #20).
- **Next:** PR #20 (first-boot + safe mode), then PR #21 (CI gates + QEMU test).
