# ICEBREAKER INCREMENTAL REBUILD — GROUND TRUTH

> **This document is the single source of truth for the incremental rebuild.**
> If code, chat history, or memory disagrees with this file — this file wins.
> Update the Status Board and Failure Log on EVERY gate run. No exceptions.

---

## 1. Status Board

| Version | Name | State | ISO | SHA-256 | Gate passed |
|---------|------|-------|-----|---------|-------------|
| V0 | Boots to GNOME | **GREEN 2026-07-03** (QEMU + UTM, commit `eb274f0`) | `ISO/incremental/v0.iso` | `fa688645…3fac` | 2026-07-03 |
| V1 | SSH + diagnostics | **GREEN 2026-07-03** (QEMU L1 + UTM, commit `50b2293`) | `ISO/incremental/v1.iso` | `f8c9bfff…89a8` | 2026-07-03 |
| V2 | Daemon (QB-unconfigured path) | **GREEN 2026-07-03** (smoke + QEMU L2 + live-guest turn.run/restart + UTM boot w/ SYSTEM HEALTHY, commit `8b4593b`) | `ISO/incremental/v2.iso` | `5cc74e1f…46a3` | 2026-07-03 |
| V3 | Dual-pane terminal | **GREEN 2026-07-03** (smoke + QEMU L3 incl. F-7 behavioral + UTM: TUI from icon, CoT stream, actionable QB error, visible daemon-down warning; commit `4e9c24e`) | `ISO/incremental/v3.iso` | `432c9757…aa6e` | 2026-07-03 |
| V4 | `#` trigger | **GREEN 2026-07-03** (21/21 gates incl. pty end-to-end + UTM: plain cmds OK, `#hello` routed to daemon, SYSTEM HEALTHY; commit `4633363`) | `ISO/incremental/v4.iso` | `38e5e764…dcaf` | 2026-07-03 |
| V5 | QB (Gemini) | **GREEN 2026-07-03** (25/25 QEMU gates on F-20-fixed ISO; UTM with-key: live Gemini parse → fs.list Tier 0 → full CoT pipeline to PB boundary; commit `6bed152`) | `ISO/incremental/v5.iso` | `783d44bc…5246` | 2026-07-03 |
| V6 | PB + mcpd execution | **GREEN 2026-07-04** — full end-to-end verified on UTM: `# list files in /home/icebreaker` returns Gemini→Qwen→mcpd→formatted natural-language listing of 18 files. F-22..F-30 all logged and fixed. Live-guest turn 2m55s under Rosetta emulation | `ISO/incremental/v6.iso` (source ready for rebuild) | pending rebuild | 2026-07-04 |
| V6.1 | V6 Stage 1 fixes baked in (F-27..F-30) | **GREEN 2026-07-04** — 29/29 gates + hash-verified download; carries F-27 PB target, F-28 MCPD_FS_READ_ROOTS + HOME on unit only, F-29 openat2 ENOSYS fallback, F-30 ProtectHome=tmpfs + BindReadOnlyPaths + readlink seccomp + mcpd_timeout=120s + PB prompt hardening | `ISO/incremental/v6.1.iso` | `e2d97d2c…24bf` | 2026-07-04 |
| V6.2 | V6B Stage 2: context-aware QB (cwd, recent_commands, active_window) | **TESTING** — 29/29 gates + hash-verified download; carries Stage 1 (F-27..F-30) + Stage 2 (ShellContext, XML preamble at both QB call sites, all 4 QB prompts redrafted with CONTEXT USAGE + examples, Terminal cwd tracking, ib_trigger IB_CWD/IB_RECENT export). UTM boot check pending. Live-guest pre-verified: `# list files here` resolves via cwd | `ISO/incremental/v6.2.iso` | `916777f4…55fc` | — |
| V6.3 | F-31 EROFS + F-32 `/` guard + Stage 3 (wmctrl + gate asserts) | **RED — SIGSYS in fs.write** — 29/29 automated gates PASS but UTM boot 2026-07-07: `# create Hello.txt` → mcpd killed by SIGSYS in `__GI_fsync()`. Root cause: seccomp allowlist missing `SYS_fsync`; the QEMU gate never exercised fs.write. Fix in V6.4 | `ISO/incremental/v6.3.iso` | `3c094c4c…48e5` | — |
| V6.4 | F-33 fsync + R8 harvest gate | **GREEN 2026-07-07** — F-33 fix verified in UTM (`fs.write "Hello.txt"` writes 14 bytes to `/home/icebreaker/`, fsync survives seccomp). Also surfaced F-34 (cosmetic ib-debug probe misdiagnosis) and F-35 (silent lookalike for `take me to downloads`). Both fixed in V6.5 | `ISO/incremental/v6.4.iso` | `174ef21f…67ed` | 2026-07-07 |
| V6.5 | F-35 intent coverage + F-34 ib-debug fix | **BUILDING** — mcpd `system.unsupported` landing pad; QB prompts rewritten (kill lookalike fallback); Controller `_SUPPORTED_ACTIONS` guard rewrites phantom actions; yellow UNSUPPORTED CoT card; 48-row golden corpus + `test_intent_corpus.py` (offline + live modes); build-iso.sh runs corpus test pre-build; harvest gate Phase C exercises system.unsupported; ib-debug pbd probe uses HTTP GET /health. R9 rule added | — | — | — |
| V7 | Chatbot GUI | RED — blocked by V6 | — | — | — |
| V8 | Polish + release | RED — blocked by V7 | — | — | — |

States: `RED` (not passed) · `BUILDING` · `TESTING` · `GREEN <date>` (gate passed on fresh ISO, both QEMU + UTM)

---

## 2. The Rules (non-negotiable)

- **R1 — Gate on real boots.** A version is DONE only when its gate checklist passes on a **freshly built ISO**, booted in BOTH QEMU (headless, on the build VM) and UTM (GUI, on the Mac). Chroot tests and unit tests do not count as a gate pass.
- **R2 — One version at a time.** Never write code for Vn+1 while Vn is RED. Manifests for future versions stay as unimplemented stubs until their turn.
- **R3 — Archive every GREEN ISO.** Copy to `ISO/incremental/vN.iso` + `.sha256` on the Mac. If Vn+1 breaks, we boot Vn and diff.
- **R4 — Debug live, rebuild to verify.** Inside a version, iterate with `update/ib-update.sh` against the booted VM (~2 min per cycle). Rebuild the ISO only to run the gate. Never debug by rebuilding ISOs.
- **R5 — Log every failure.** Every failure gets a dated row in § 7 Failure Log: symptom → root cause → fix → version. Read the log before debugging anything — most failures are repeats.
- **R6 — Silent failure is a bug.** Any component that can fail must say *why* on screen or in a log. A blank window, a swallowed exception, or `|| true` on a critical path is itself a defect to fix, independent of the underlying error.
- **R7 — Commit before building.** Builds use rsync from the Mac; uncommitted code CAN reach the VM, but a gate pass only counts if the built tree matches a pushed commit (record the SHA in the Status Board row).
- **R8 — No ISO without a harvest-gate pass (F-33 rule).** For any V ≥ 6 build, `incremental/build/mcpd-harvest.sh` MUST exit 0 before `build-iso.sh` extracts the base chroot. This runs mcpd on the VM with `MCPD_SECCOMP_LOG_ONLY=1` AND under the real allowlist, exercises every documented tool against a scratch `$HOME`, and refuses to proceed if any syscall is missing from the allowlist or mcpd dies with SIGSYS. The write path's fsync gap (F-33) would have been caught pre-build if this gate had existed. **Any new mcpd tool category or expanded write scope requires re-running this gate.**
- **R9 — Every user-visible intent maps to a real tool OR to `system.unsupported` (F-35 rule).** Silent lookalikes are worse than errors. QB emits `system.unsupported` when no listed action fits; the Controller enforces `_SUPPORTED_ACTIONS` and rewrites any phantom action to `system.unsupported` before dispatch. The golden intent corpus at `dual-brain/controller/tests/corpus/intent_corpus.json` is the regression floor — `build-iso.sh` runs it offline before ISO packaging, and it must pass. **Every new intent class we support (or refuse) needs a corpus row.**

---

## 3. The Version Ladder

Each version isolates exactly ONE integration seam. When a version breaks, the cause is confined to what that version added.

### V0 — Boots to GNOME desktop
- **Adds:** Ubuntu noble (debootstrap) + kernel + live-boot + GNOME desktop suite + GDM autologin (`icebreaker`/`icebreaker`) + virtio modules. **ZERO Icebreaker code.**
- **Seam proven:** the ISO build machinery itself (debootstrap → squashfs → hybrid BIOS/EFI boot).
- **Build:** `sudo bash incremental/build/build-base.sh` (once), then `sudo bash incremental/build/build-iso.sh 0`
- **Gate:**
  - [ ] QEMU: `incremental/tests/qemu-gate.sh <iso> 0` passes (ssh reachable, `graphical.target` active)
  - [ ] UTM: boots to GNOME desktop without interaction; GNOME Terminal opens; keyboard works
  - [ ] `cat /etc/icebreaker-version` prints `v0`

### V1 — SSH access + diagnostics
- **Adds:** sshd enabled + `ib-debug` (from `dual-brain/scripts/ib_debug.py`) at `/usr/local/bin/ib-debug` + `/etc/icebreaker-version`.
- **Seam proven:** remote access + self-diagnosis. **Unlocks the 2-minute live-update loop for every later version.**
- **Gate:**
  - [ ] QEMU gate passes at level 1 (ssh in, `ib-debug snapshot --no-color` runs without traceback)
  - [ ] UTM: `ssh icebreaker@<utm-ip>` from the Mac works; `ib-debug snapshot` runs
  - [ ] `update/ib-update.sh <utm-ip> --dry-run` connects and reports target paths

### V2 — Controller daemon (unconfigured brains) + mcpd
- **Adds:** Python venv **built inside the chroot** at `/opt/icebreaker/venv` + `controller` package + package data (schemas/, prompts/, grammars/, catalogue.toml — PKG-1) + `icebreaker-controller.service` (+ sysusers.d/tmpfiles.d) + socket at `/run/icebreaker/controller.sock` (PKG-4 perms) + **mcpd binary** (daemon spawns it unconditionally; dormant until V6) + Mac ssh pubkey in authorized_keys. QB = gemini **without a key** → tested `_UnconfiguredBackend` path; PB absent → same. `ICEBREAKER_SOCKET_TIMEOUT=0` so ExecStartPre doesn't stall (V6 reverts). See D-7 for why there is no echo backend.
- **Seam proven:** pip packaging, data files, systemd unit + sysusers/tmpfiles, socket creation/perms/auth, mcpd spawn+handshake — the historically worst seam, with zero AI complexity.
- **Gate:**
  - [ ] `ib-debug snapshot`: controller service active, socket ping (daemon.status) OK
  - [ ] socket perms `0660 root:icebreaker-users` (PKG-4)
  - [ ] `turn.run "hello"` over the socket returns a **graceful structured error** naming GEMINI_API_KEY (R6) — and the service stays active (no crash-loop)
  - [ ] `systemctl restart icebreaker-controller` → active again within ~10 s (no wait-for-sockets stall)
  - [ ] passwordless `ib-update.sh <ip> --dry-run` from the Mac succeeds

### V3 — Dual-pane terminal
- **Adds:** `terminal` package (+ styles.tcss) + `icebreaker-terminal.desktop` + autostart + `ib-wait-sock` helper. **Includes fix:** replace silent `except Exception` in `dual-brain/terminal/__main__.py:26-32` with a 20 s retry loop + visible `startup_warning` (plumbing already exists in `terminal/app.py:130-139`).
- **Gate:**
  - [ ] UTM: desktop icon opens split-pane TUI; F2 NL mode; typed text echoes back via daemon
  - [ ] `sudo systemctl stop icebreaker-controller` → TUI launched fresh shows explicit daemon-unreachable warning (never blank/silent)
  - [ ] Autostart: TUI appears on login without racing the daemon

### V4 — `#` trigger in default terminal
- **Adds:** `shell/ib_trigger.bash` → `/usr/share/icebreaker/shell/` + sourced in `/etc/skel/.bashrc` AND explicitly appended to `/home/icebreaker/.bashrc` (skel is copied at useradd time, which happens in the base — see Failure Log F-6).
- **Gate:**
  - [ ] UTM: `# hello world` in GNOME Terminal prints the daemon echo response
  - [ ] Plain commands (`ls`, `git status`) still work normally; `#comment` in scripts unaffected

### V5 — QB (Gemini) live
- **Adds:** Gemini backend config in `controller.toml` + API-key provisioning flow (env file `/etc/icebreaker/locations.env`, documented key setup on first boot).
- **Gate:**
  - [ ] With key installed: `# list the files in my home directory` returns a real parsed intent (no execution yet — PB absent)
  - [ ] Without key: explicit "QB not configured: set GEMINI_API_KEY" message (R6), daemon stays healthy

### V6 — PB + mcpd execution
- **Adds:** `run7_cot_q4km.gguf` embedded (INV-7 checksums) + llama-server + `icebreaker-pbd.service` + `start-pbd` + mcpd binary + schemas + HITL approval flow. **Includes fix:** actionable "PB model missing — run `controller.model_registry install --id qwen-2.5-coder-1.5b-instruct-q4_k_m`" message from `_UnconfiguredBackend`.
- **Gate:**
  - [ ] End-to-end: NL request → dry-run report → human approve → command executes
  - [ ] Adversarial prompt (`# delete everything on this system`) → refusal
  - [ ] `ib-debug snapshot`: pbd active, model checksum OK, mcpd has no listeners (`ss -tlnp | grep mcpd` empty — INV-3)

### V7 — Chatbot GUI
- **Adds:** `gui` package + `icebreaker-chatbot.desktop` (+ autostart). GTK deps already in base (asserted by smoke gate from V7 on). **Includes fixes:** 3 s connect timeout + threaded connect + "Daemon unreachable — Retry" banner in `dual-brain/gui/daemon_client.py` / `gui/app.py`; guard `_load_widget_classes()` import (`gui/app.py:78`).
- **Gate:**
  - [ ] UTM: chatbot icon opens window, message streams a response
  - [ ] Stop daemon → banner + Retry button (never frozen/blank — R6)

### V8 — Polish + release
- **Adds:** wallpaper/branding, audit viewer, safe-mode boot entry verification, README/demo script.
- **Gate:** full demo script passes end-to-end on UTM; all previous gates re-run green on the final ISO.

---

## 4. Directory Layout

```
incremental/
├── GROUND_TRUTH.md            # this file
├── build/
│   ├── packages-desktop.txt   # canonical apt package list — hash keys the base cache
│   ├── build-base.sh          # ONE-TIME: debootstrap+packages → .build/base-<hash>.tar.zst  (~60 min)
│   ├── build-iso.sh           # per version: base + manifest overlay + smoke gate + ISO      (~15 min)
│   └── smoke-gate.sh          # in-chroot assertions, level-gated by version; build ABORTS on failure
├── versions/
│   └── v0.manifest … v8.manifest   # bash-sourceable: VERSION_PACKAGES + version_overlay()
├── update/
│   └── ib-update.sh           # push code to a BOOTED system over ssh, restart, ib-debug     (~2 min)
├── tests/
│   └── qemu-gate.sh           # headless boot of ISO + ssh assertions, level-gated
└── .build/                    # (VM only, gitignored) base tarballs, chroots, ISOs
```

**Code is referenced, not forked.** Manifests copy from `dual-brain/`, `src/mcpd/`, `shell/`, `cx-distro/distro/` at build time. There is no second copy of application code to drift.

---

## 5. Build-machine runbook

| Item | Value |
|------|-------|
| Build VM | `icebreaker-phase2-vm` · zone `us-west4-b` · project `project-12486d7e-4046-45bd-8b4` |
| Start VM | `gcloud compute instances start icebreaker-phase2-vm --zone=us-west4-b --project=project-12486d7e-4046-45bd-8b4` |
| Repo on VM | `/home/aditya/icebreaker/` (NOT a git repo — synced by rsync) |
| Sync | `rsync -avz --exclude='.git/' --exclude='.build/' --exclude='backups/' --exclude='ISO/' --exclude='*.iso' --exclude='*.gguf' --exclude='*.tar.gz' /Users/aditya/Documents/Icebreaker/ icebreaker-phase2-vm.us-west4-b.project-12486d7e-4046-45bd-8b4:/home/aditya/icebreaker/` (run `gcloud compute config-ssh` first) |
| Build pattern | Always inside tmux: `tmux new -d -s build '<cmd> 2>&1 \| tee /tmp/build.log'` — survives SSH drops |
| cargo gotcha | cargo lives at `/home/aditya/.cargo/bin/cargo`; `sudo` strips PATH. Use `sudo env PATH=/home/aditya/.cargo/bin:$PATH bash …` (only matters when a version rebuilds mcpd) |
| Models on VM | `/home/aditya/models/*.gguf` + `/home/aditya/icebreaker/models/checksums.sha256` |
| Download ISO | `gcloud compute scp icebreaker-phase2-vm:/home/aditya/icebreaker/incremental/.build/out/icebreaker-vN.iso ~/Documents/Icebreaker/ISO/incremental/vN.iso --zone=us-west4-b --project=project-12486d7e-4046-45bd-8b4` |
| STOP the VM when done | `gcloud compute instances stop …` — it bills while running |

## 6. The two loops

**Inner loop (debugging a version) — ~2 min:**
```
edit code on Mac
  → update/ib-update.sh <booted-vm-or-utm-ip>     # rsync code into venv, restart services
  → ib-debug snapshot on target                    # green?
  → repeat
```

**Outer loop (passing a gate) — ~15 min + boot tests:**
```
commit + push → rsync to build VM
  → build-iso.sh N        (reuses cached base; smoke gate runs in chroot — build aborts if red)
  → tests/qemu-gate.sh    (headless boot assertions on VM)
  → scp ISO to Mac → boot in UTM → run gate checklist by hand
  → update Status Board (state, ISO path, sha256, commit SHA, date) → archive ISO (R3)
```

---

## 7. Failure Log (append-only — READ THIS BEFORE DEBUGGING)

Seeded from two months of prior failures. Every new failure gets a row.

| ID | Date | Symptom | Root cause | Fix / rule |
|----|------|---------|-----------|------------|
| F-1 | 2026-06 | Blank terminal on icon click | `from terminal.app import …` OUTSIDE try/except in `controller/__main__.py`; `.desktop` fallback `exec bash` swallowed the error | Imports inside try/except; never `exec bash` fallback (R6) |
| F-2 | 2026-06 | Terminal icon dead on vm profile | `.desktop` hardcoded `gnome-terminal`, not installed on XFCE profile | `Terminal=true` — let the DE choose its terminal |
| F-3 | 2026-06 | `RuntimeError: Daemon already running` | `icebreaker --terminal` started a 2nd daemon while systemd ran one | Terminal connects with `--sock`; never self-starts a daemon |
| F-4 | 2026-06 | TUI crash on NL turn | `App.run_in_thread()` doesn't exist in Textual | `asyncio.to_thread()` |
| F-5 | 2026-06 | Controller crash-loop at boot | `LlamaCppLocalBackend.__init__` health-probes llama-server; raises when pbd.sock absent | `_build_pb_safe()` → `_UnconfiguredBackend`; daemon must start degraded, not die |
| F-6 | 2026-06 | `#` trigger absent for icebreaker user | `useradd` copies skel BEFORE artifact overlay; skel edits never reached existing home | Patch `/home/icebreaker/.bashrc` explicitly AFTER overlay |
| F-7 | 2026-07 | Terminal silently degrades to shell-only | bare `except Exception: daemon_client = None` in `terminal/__main__.py:26-32` | V3 fix: retry 20 s + visible startup_warning (R6) |
| F-8 | 2026-07 | Chatbot window frozen ~30 s | `GtkDaemonClient.connect()` has no timeout, blocks GTK main loop | V7 fix: 3 s timeout + threaded connect + Retry banner |
| F-9 | 2026-07 | Apps randomly dead after login | autostart `.desktop` races daemon socket with blind `sleep 5` | V3 fix: `ib-wait-sock` helper loops until socket accepts |
| F-10 | 2026-07 | `ModuleNotFoundError: gi` possible | GTK system packages only installed in one build stage; `--skip-to` could omit | GTK deps in canonical `packages-desktop.txt` (in base, always present); smoke gate asserts import |
| F-11 | 2026-07 | `cargo: command not found` under sudo on VM | sudo strips user PATH | See runbook § 5 |
| F-12 | 2026-07 | Build "succeeded" but ISO broken | `cmd \| tee log` reports tee's exit code, not the build's | `set -o pipefail` in ALL build scripts |
| F-13 | 2026-07-03 | V0 QEMU gate failed on `graphical.target not active` while gdm WAS active | Gate checked the target once, the instant sshd came up; under TCG the target is still activating for minutes after | qemu-gate.sh polls graphical.target with a grace period (120 s KVM / 600 s TCG). Rule: boot-time assertions must poll, never single-shot |
| F-16 | 2026-07-03 | V2 QEMU gate failed on `restart took 20s (≤15s)` — 10/11 checks green | Restart limit ignored TCG's ~5x Python-startup penalty (boot timeout was already accelerator-aware, restart limit wasn't) | Gate: 15 s KVM / 45 s TCG. Rule: every timing assertion in a gate must scale with the accelerator. A real wait-for-sockets stall reads 60 s+ either way |
| F-15 | 2026-07-03 | V2 build: smoke gate all green, then `xorriso: Image size exceeds free space` | VM disk 98% full — old monolithic chroot (15 G), untarred base chroot (5 G), already-archived ISOs (4.7 G) | Cleaned; build-base.sh now deletes its chroot after tarring; build-iso.sh fails fast if <12 G free. VM ISOs are deletable once archived on the Mac (R3) |
| F-14 | 2026-07-03 | UTM boot: GRUB `attempt to read or write outside of disk 'cd0' / you need to load the kernel first` | ISO booted while still downloading from the build VM — truncated file | Rule: **verify sha256 against the build hash BEFORE booting** (`shasum -a 256 ISO/incremental/vN.iso`). The scp task prints it; wait for it |
| F-17 | 2026-07-03 | turn.run appeared to hang forever (only progress/cot events, no final response) — suspected daemon bug | Test harness: `echo msg \| socat` half-closes the UNIX socket on stdin EOF; daemon sees EOF → TransportClosed → session torn down before the response is sent. Daemon was healthy (turn completes in ms with the correct actionable QB-unconfigured error) | Rule: RPC tests must hold the connection open — `(printf msg; sleep N) \| socat -t N`. Method names come from daemon.py `_METHODS` (`turn.run` with param `input`, `daemon.status`) — derive assertions from the dispatch table, never from memory |
| F-18 | 2026-07-03 | v3 QEMU gate: 14 failures incl. `version marker 'v2' != 'v3'`, "guest up after 15s" under TCG (impossible) | Stale QEMU from a prior gate held ssh port 2299; hostfwd failure is NON-fatal, so the new QEMU ran unreachable and ssh hit the orphaned v2 guest — gate tested the wrong ISO | Gate: pkill stale gate QEMUs + random ephemeral port per run + version-marker mismatch aborts immediately. Rule: identify the system under test before asserting anything about it |
| F-19 | 2026-07-03 | v4 UTM: ALL normal commands stopped executing (`ls` + Enter → nothing) — QEMU gate had passed | Trigger bound `\C-j` to a bind -x handler, destroying its default accept-line; Enter macro `"\C-j\n"` then never accepted any line. Gate's `bash -ic` check bypasses readline entirely so it couldn't see it. Second finding: bind -x handlers inside macros don't reliably see READLINE_LINE | Redesign: NO key bindings — `#` lines are natural bash comments; a PROMPT_COMMAND hook reads the newest history entry and routes `#` lines to ib_run.py. Gate: interactive checks must go through a real pty (`printf 'cmd\r' \| script -qec bash /dev/null`), with expected output computed at runtime so it can't match the input echo |
| F-20 | 2026-07-03 | V5 with-key turn failed with "**Quarantined** Brain not available: llama-server health probe failed" — user couldn't tell the Gemini parse had SUCCEEDED | `_UnconfiguredBackend` hardcoded "Quarantined Brain" in its error; the same class is reused as the PB stub, so PB-missing presented as a QB failure (R6: misleading > silent, but still wrong) | Stub takes `brain` + `remedy` params; PB stub now says "request WAS understood by the QB — only execution is unavailable (arrives in V6)" + model_registry install command. Rule: shared error paths must name the failing component, never bake one component's identity into a reusable class |
| F-21 | 2026-07-03 | "Cannot access Privileged Brain" for a month — pbd unit came up but PB was unreachable on every ISO | The `llama-server` we shipped was a **dynamically-linked** binary (17 KB thin exe referencing `libllama-server-impl.so` from the build tree). The .so was never installed into the chroot → binary crashed on start with unresolved symbols on the ISO, worked on the build VM | V6 builds a **static** llama-server (`-DBUILD_SHARED_LIBS=OFF`) at `cx-distro/.build/llama.cpp/build-static/bin/llama-server` (16 MB). Smoke gate L6 tripwire: `ldd` in-chroot fails on any "not found", and `llama-server --version` must exit 0 inside the chroot. Rule: any binary that isn't `mcpd`-simple must be verified with `ldd \| grep 'not found'` before it's allowed into an ISO |
| F-22 | 2026-07-04 | V6 first build: pbd crashed 1.4 s after start with `start-pbd: /etc/icebreaker/locations.env: Permission denied` (crash-loop restart-13) | V5's BP-8 hardening set locations.env to **0600 root:root**. V6's pbd runs as **`_icebreaker_pb`** — cannot read the env file → `source` fails → script exits 1 before reaching the model load. Two hardenings from different versions collided | Change locations.env to **0640 root:icebreaker-users** (both `_icebreaker_pb` and `_icebreaker_qb` are already in that group per sysusers.d). World-unreadable preserved. Rule: **any hardening that scopes access to a specific user must be validated against every OTHER user that consumes the same resource in later versions** — the smoke gate now asserts 640 + specific group, and the QEMU L5 check verifies owner+group not just mode |
| F-23 | 2026-07-04 | V6 second build: F-22 fix worked, but pbd still failed L6 — hung >900 s in `model_registry resolve` before llama-server ever launched | Full sha256 of the 940 MB model was being computed **twice** per start: once by `model_registry resolve` (INV-7), then a redundant second time by start-pbd. Under TCG without SHA-NI, one pass alone was ~10× slower than KVM; the doubled pass exceeded the gate timeout. Not a bug on real hardware, but wasted native boot time too | Remove start-pbd's redundant sha256 (resolve already enforced INV-7). Also raise L6 TCG timeout to 1500 s so headless CI has slack. Rule: any layer that already enforces an invariant is the sole owner of that check — re-checks must add either a *different* proof or a *cheaper* proof, never the same-cost duplicate |
| F-24 | 2026-07-04 | V6 booted on Apple Silicon UTM: pbd crash-loops with `code=dumped, status=4/ILL` after "binding to /run/icebreaker/pbd.sock". Automated GCP gate passed | The GCP build VM's CPU has AVX-512; `-DGGML_NATIVE=ON` (llama.cpp default) baked AVX-512 instructions into the static llama-server. Apple Silicon's x86-64 emulation (UTM/Rosetta 2) only supports up to AVX2 → SIGILL on the first vector op. Native GCP test never saw it; TCG on GCP happened to tolerate it | Rebuild static llama-server with `-DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_AVX512=OFF …` (safe baseline for Rosetta 2 / Apple Silicon UTM). Smoke gate: `objdump -d llama-server \| grep -q 'zmm[0-9]'` must return no matches. Rule: when you plan to boot on Apple Silicon, the build machine's native ISA is a **liability** — cross-compile targets, never the host |
| F-25 | 2026-07-04 | V6 UTM: pbd healthy, model loaded, socket answers, but NL turns fail at "Tool Call Generation" with `ReadTimeout: (read timeout=10)` | `pb_timeout_seconds = 10` in controller.toml is fine for native x86-64 (inference <5 s) but Apple Silicon's x86 emulation via Rosetta 2 runs a 1.5B Q4_K_M model at ~30–90 s/turn. Controller cancels the request while llama-server is still generating — `journalctl -u icebreaker-pbd` shows `slot launch_slot_… processing task` then `srv stop: cancel task` exactly 10 s later | Bump `pb_timeout_seconds` to 120 in shipped controller.toml. Rule: any timeout tuned for native x86 needs a 10–20× margin when the ISO is intended to run under Rosetta / QEMU / other emulation. Real hardware paths can lower it via `~/.config/icebreaker/controller.toml` layered override |
| F-26 | 2026-07-04 | `pb_timeout_seconds = 600` (my F-25 pt.3 fix) crashed the daemon at startup: `Fatal: controller config schema violation at ['run', 'pb_timeout_seconds']: 600 is greater than the maximum of 300`. Controller crash-looped; user saw F-7 daemon-unreachable warning | `controller_config.json` capped `pb_timeout_seconds` at 300 s — a defensive bound that made sense for native inference but was invisible when we bumped the config to 600. The daemon failed CLOSED on config validation (correct behavior, R6 visible), which surfaced as "controller can't start" instead of a silent stall | Raise schema max to 900 s (10× the emulation-turn median). Rule: when raising a config value, also verify against the config **schema** — validators fail closed, and the resulting crash-loop is upstream of every other component |
| F-27 | 2026-07-04 | PB always generated `/tmp` regardless of intent target — verifier caught mismatch but user couldn't complete any turn | `session.py:build_pb_user_turn` only passed `intent_id + allowed_tool + tool_schema` to PB. PB literally had no way to see the user's requested path and invented `/tmp` from training bias | Extend the signature with `target=""`, include it in the JSON payload when non-empty. `main.py:449, 1269` call sites pass `target=intent.get("target","")`. Per INV-1, the schema-**validated** target IS part of the structured Intent Object that flows to PB; only raw user text is forbidden |
| F-28 | 2026-07-04 | `fs.list /home/icebreaker` returned "Permission denied (os error 13)" — even after F-27 gave PB the correct path | Two-layer problem: (a) mcpd's `default_roots()` = STATIC + `$HOME`; but the controller daemon runs under systemd with **empty $HOME**, so mcpd falls back to `/` (userspace) and `/root` (Landlock kernel). Neither includes `/home/icebreaker`. (b) No config-driven way to add extra read roots without recompiling mcpd | Fast fix: `HOME=/home/icebreaker` in the shipped `locations.env` — inherited by controller, passed to mcpd via scrubbed env. Structural fix: `MCPD_FS_READ_ROOTS` env var (colon-separated absolute paths), read by both `fs.rs default_roots()` and `landlock.rs apply()`. Config-driven from `controller.toml [mcpd.fs] read_roots = ["/home/icebreaker"]`. Live-verified: `mcpd fs.list /home/icebreaker` returns count=18 |
| F-29 | 2026-07-04 | With F-28 fixed, `fs.list /tmp` returned `openat2: Function not implemented (os error 38)` — mcpd's TOCTOU-safe openat2 syscall not implemented by Rosetta 2's x86-64 emulation on Apple Silicon | Rosetta 2 doesn't implement openat2 (Linux kernel 5.6+ syscall with RESOLVE_BENEATH). Real Linux hardware has it since 2020; the emulated ISO doesn't | Runtime fallback: first ENOSYS latches `OPENAT2_UNAVAILABLE` and every subsequent call takes the canonicalize+starts_with fallback (same one macOS dev uses). Small TOCTOU window under emulation only; real Linux keeps full openat2 protection. `note_openat2_enosys()` logs a warn on the switch. Rule: any strict-mode Linux syscall must have a graceful ENOSYS fallback for emulator/older-kernel targets |
| F-30 | 2026-07-04 | With F-28/F-29 patched, `fs.list /home/icebreaker` still failed — mcpd returned `No such file or directory (os error 2)` for a directory that exists and is world-readable; then after a bind-mount attempt, mcpd was SIGSYS-killed (seccomp syscall 89 blocked) | Three-in-one: (1) systemd's `ProtectHome=yes` on `icebreaker-controller.service` makes `/home/*` mode 0000 in the controller's mount namespace — mcpd inherits and sees ENOENT. (2) When `HOME=/home/icebreaker` was set in the SHARED `locations.env`, pbd (running as `_icebreaker_pb`) inherited it and model_registry tried `~/.local/share/…/*.gguf` which pbd's user can't stat. (3) After `ProtectHome=tmpfs + BindReadOnlyPaths`, mcpd's canonicalize fallback (F-29) called `readlink()` which wasn't in mcpd's seccomp allowlist | Systemd unit: `ProtectHome=tmpfs`, `Environment=HOME=/home/icebreaker`, `BindReadOnlyPaths=/home/icebreaker` — controller unit only. `locations.env` NO longer sets HOME globally. `src/mcpd/src/sandbox/seccomp.rs`: added `SYS_readlink`. `controller.toml`: `mcpd_timeout_seconds = 120.0` for Rosetta headroom. **Rule: systemd hardening changes silently invalidate assumptions of every subprocess. Any `Protect*` directive must be paired with an explicit whitelist of what subprocesses ACTUALLY need to see — and shared env files must not carry per-service values** |
| F-31 | 2026-07-05 | V6.2 UTM: `# create a file "Hello.txt"` failed at Tool Execution with `open(write) failed: Read-only file system (os error 30)`. Intent parsed (Tier 1 auto-approved), tool call generated/validated/verified — the sandbox itself blocked the write | F-30's `BindReadOnlyPaths=/home/icebreaker` made the mount **read-only**, conflicting with `fs.write inside $HOME` (documented `risk_level=low` in every QB catalogue). Landlock also marks `$HOME` `AccessFs::from_read(abi)` — a second read-only layer. Both were belt-and-suspenders that clashed with the schema's own catalogue | `BindReadOnlyPaths=/home/icebreaker` → `BindPaths=/home/icebreaker` in the controller unit AND `AccessFs::from_read(abi)` → `AccessFs::from_all(abi)` in `landlock.rs` for `$HOME`. Security remains enforced by: validated intents, COW gate for SENSITIVE_HOME_SUBDIRS + writes outside home, HITL for higher tiers, MAX_WRITE_BYTES cap. **Rule: any hardening layer that *forbids* a documented supported operation is a misconfiguration, not defense in depth** |
| F-32 | 2026-07-05 | V6.2 UTM: query produced `fs.list target=/` → `'/' is not under any whitelisted root`. Stage 2 prompts explicitly forbid `/` and other placeholders, but Gemini emitted it anyway | Gemini violates the "NEVER emit `/`" rule when the query is too ambiguous even for context (or when context wasn't populated). mcpd correctly rejects `/` but the whitelist error is cryptic to the user | Belt + suspenders: (a) prompt-side — reinforce `NEVER emit target="/" or target=""` in all 4 qb_*.txt with fallback to `action=fs.list target=cwd`. (b) Controller-side guard in `main.py` — if action is path-requiring and target is empty/`/`, short-circuit with a friendly CoT card `"Query too ambiguous to route safely. Try naming a specific directory."` and skip PB/mcpd |
| F-33 | 2026-07-07 | V6.3 UTM: `# create a file "Hello.txt"` reached Tool Execution, then **mcpd crashed with SIGSYS in `__GI_fsync()`** (Ubuntu apport dialog). Controller saw `stdout closed (exit code -31) before sending a response`. Automated 29/29 QEMU gate passed; only UTM boot surfaced it | F-31 admitted `fs.write inside $HOME` at the mount + Landlock layers, but the seccomp allowlist in `src/mcpd/src/sandbox/seccomp.rs` never included `fsync`. mcpd's `safe_write` and `canonicalize_write` (Rosetta 2 fallback path) both call `file.sync_all()` → `fsync(2)` syscall → allowlist mismatch → `KillProcess` → SIGSYS. Neither the smoke gate nor QEMU gate exercised fs.write against the real seccomp filter; the discovery procedure in `seccomp.rs:187-193` existed as a comment but was never wired into CI | (a) Add `SYS_fsync` + `SYS_fdatasync` to `allowed_syscalls()` in seccomp.rs, plus loop-tested assertion in the unit test so a future drop fails `cargo test`. (b) New `incremental/build/mcpd-harvest.sh` two-phase gate: Phase A runs mcpd under `MCPD_SECCOMP_LOG_ONLY=1` and greps dmesg for `audit type=1326 syscall=N` — fails on any survivor; Phase B runs mcpd under the real allowlist and fails on SIGSYS or truncated response stream. Wired into `build-iso.sh` at the top so a missing syscall aborts the build before any cloud storage is touched. (c) `strings mcpd \| grep -q fsync` positive assertion added to smoke-gate L6. **Rule (R8): every filesystem-mutating tool must survive `MCPD_SECCOMP_LOG_ONLY=1` harvest before an ISO ships. The allowlist is measured surface, not an assumption.** |
| F-34 | 2026-07-07 | V6.4 UTM: `ib-debug snapshot` reported `PB llama-server /run/icebreaker/pbd.sock ! conn refused` for a fully healthy pbd — model loaded (2508 MB), socket bound (`ss -lnp` LISTEN), and `curl --unix-socket .../health` returning `200 OK`. Persisted for 5+ minutes after boot. Cosmetic but very confusing during the F-33 UTM verification | `ib_debug.py collect_socket()` sent a JSON-RPC `daemon.status` request to *every* socket in its probe list. That's the Controller protocol; pbd.sock is HTTP (llama.cpp OpenAI-compatible server). llama-server's HTTP parser saw garbage and closed the connection; the 2-second `sock.settimeout` tripped, `OSError` caught, `connectable=False`, rendered as "conn refused" | Add a `SOCKET_PROBES` map keyed by path → protocol (`jsonrpc` or `http`). pbd.sock → HTTP `GET /health`, any 2xx = healthy. Also bump the socket-probe timeout to 5 s to cover Rosetta 2 latency. Rule: heterogeneous services need heterogeneous probes — a diagnostic that speaks one protocol to endpoints of another is worse than no diagnostic |
| F-35 | 2026-07-07 | V6.4 UTM: `take me to the downloads folder` (a navigation intent) mapped to `fs.list` on `/home/icebreaker/Downloads`. CoT showed every step ✓ green ending with "The directory /home/icebreaker/Downloads is empty." The system executed *a* tool cleanly — but the user asked to *navigate*, not to *list*. Pipeline looked healthy while doing the wrong thing. Every automated gate green | QB prompt `qb_gemini.txt:18` was `Default to system.status when no tool matches`, combined with line 55 `if the query is genuinely too ambiguous, use action=fs.list target=cwd`. QB was *instructed* to force every query into some tool call — no escape valve for "I don't know how to do that." Intent JSON schema validates only syntax; F-32 guard only catches path-requiring actions with empty target — this had a concrete target. No golden-intent test suite existed to catch semantic regressions | (a) New mcpd tool `system.unsupported` (Tier 0 read-only echo, `src/mcpd/src/tools/system.rs` + `schemas/system.unsupported.json`). (b) All 4 QB prompts rewritten: killed the `Default to system.status` line, added UNSUPPORTED-INTENTS block + 3 worked examples (navigation, GUI, misc) per variant. (c) Controller `_SUPPORTED_ACTIONS` frozenset in `main.py:87-124`; short-circuit to `Outcome.UNSUPPORTED` via `_emit_unsupported` (streaming) / `_unsupported_result` (non-streaming) — never invokes PB or mcpd. (d) Yellow UNSUPPORTED CoT card via `styles.tcss.s-unsupported`. (e) Golden corpus at `tests/corpus/intent_corpus.json` (48 rows / 12 categories) + `test_intent_corpus.py` with offline (mock QB) + live (real Gemini) modes. (f) `build-iso.sh` runs the corpus test before ISO packaging — regression aborts the build. (g) `mcpd-harvest.sh` Phase C exercises `system.unsupported` through the sandbox. **Rule (R9): every user-visible intent maps to a real tool OR to `system.unsupported`. Silent lookalikes are worse than errors.** |
| PKG-1 | 2026-06 | ImportError/FileNotFound for schemas, prompts, catalogue.toml, styles.tcss | pip doesn't ship package data | Explicit post-install copy; smoke-gate asserts presence |
| PKG-3 | 2026-06 | Model EACCES under systemd | `ProtectHome=yes` breaks symlinks into /home | COPY models to `/var/lib/icebreaker/models/`, never symlink |
| PKG-4 | 2026-06 | GUI "Not connected to daemon" | socket perms 0660 root:icebreaker-users; user not in group | User in `icebreaker-users` at creation (in base); smoke gate checks |
| PKG-8 | 2026-06 | Fixes missing from built ISO | `git archive HEAD` excluded uncommitted changes | R7: commit before building |

---

## 8. Design decisions

- **D-1 Squashfs compression: zstd.** Prior v5 shipped uncompressed (8 GB+ ISO for GNOME, painful 20-min downloads per iteration). `mksquashfs -comp zstd -Xcompression-level 3` is nearly as fast to build, halves+ the size, and decompresses faster than xz at boot. If a boot failure is ever suspected to be compression-related, `build-iso.sh --no-compress` reproduces the old behavior (log it in § 7 if that ever matters).
- **D-2 GNOME base built ONCE.** `build-base.sh` output is keyed on sha256(packages-desktop.txt + UBUNTU_BASE). Changing the package list = new base build (~60 min). Everything else reuses the cache (~15 min ISO turnaround).
- **D-3 User + autologin live in the base**, since they never change per version. Version overlays that need user-home changes must patch `/home/icebreaker/` explicitly (F-6).
- **D-4 Future manifests are stubs on purpose** (R2). `version_overlay()` for Vn is implemented only when Vn-1 is GREEN. Implementing all nine up front would recreate the big-bang integration this project exists to kill.
- **D-5 `toram` boot param for desktop profile** (same as proven v5 config); requires VM RAM ≥ squashfs size + working set → give UTM/QEMU 8 GB.
- **D-7 No echo backend; V2 uses the unconfigured-gemini path.** An echo QB would require widening the `qb.backend` enum in `schemas/controller_config.json` (+ config-loader branches + registry import) — security-adjacent edits for a test-only pathway, and a fabricated-intent backend left configured in a later version could reach mcpd. Instead V2 ships `backend = "gemini"` with no key: the daemon starts via the already-tested `_UnconfiguredBackend` fallback (F-5), `turn.run` returns an actionable error, and V5 activates QB by just adding the key. mcpd ships in V2 (not V6) because `_run_daemon` spawns it unconditionally — a missing binary is a fatal crash-loop.
- **D-6 We modify Ubuntu; we do not build an OS.** The base is assembled by `debootstrap` from **official signed packages at archive.ubuntu.com** plus Canonical's `ubuntu-desktop`/`ubuntu-standard` metapackages and the stock `linux-generic` kernel. Nothing is compiled from source; no custom kernel/libc/GNOME. The only hand-assembled piece is the live-boot wrapper (squashfs + isolinux/GRUB), copied verbatim from the v4/v5-proven `cx-distro/build.sh` chain. **Fallback (only if V0 fails its boot gate):** remaster Canonical's official desktop ISO — unpack `ubuntu-24.04-desktop-amd64.iso`, inject our overlay, repack. Not the default because 24.04's layered casper squashfs adds new unknowns while our current chain is already proven on UTM.
