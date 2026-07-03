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
| V2 | Daemon (echo) | **IN PROGRESS** | — | — | — |
| V3 | Dual-pane terminal | RED — blocked by V2 | — | — | — |
| V4 | `#` trigger | RED — blocked by V3 | — | — | — |
| V5 | QB (Gemini) | RED — blocked by V4 | — | — | — |
| V6 | PB + mcpd execution | RED — blocked by V5 | — | — | — |
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
