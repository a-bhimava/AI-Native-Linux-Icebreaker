# arm64 Build Approach — Discussion (July 8, 2026)

**Question**: Ubuntu already publishes an arm64 ISO for Apple Silicon Macs. Why are we building ours from scratch instead of modifying theirs?

This doc explains both approaches — the one we ship today, and the alternative — in **layman terms first**, then the **technical detail**. Read whichever half is useful.

---

## Layman explanation

### What we're actually building

Icebreaker is a Linux operating system. To install it on a Mac (via UTM Virtualize) or a real ARM computer, we need to ship a **single file** — an ISO — that boots the whole OS.

Think of the ISO like a shipping container. Inside it:
- The kernel (the "engine" of Linux)
- All the system tools (Python, GNOME desktop, network drivers)
- Our own stuff (the two AI brains, the terminal, the mcpd sandbox)
- The 940 MB AI model file

To build that shipping container, we have two philosophies:

### Approach 1 — "Build the container from scratch" (what we do today)

Imagine building an IKEA cabinet from raw wood. We start with **nothing**. We pick every screw, every plank, every hinge. We assemble it in the exact shape we want.

**Pros:**
- Every piece inside is one we chose. Nothing extra, nothing wasted.
- We know exactly what's in there — critical because Icebreaker is a security-sensitive AI system.
- Final product is small: 3.3 GB.
- The whole thing "says Icebreaker" — our name on the box, our branding.

**Cons:**
- Hard. Every time Linux updates, we have to update our recipe.
- Every bug in low-level boot code is *our* bug to fix. We already hit three of these this week:
  - "arm64 needs a GRUB helper package we forgot to install"
  - "arm64 kernel is gzip'd and our loader can't read it"
  - "we accidentally shipped the Intel version of a program in an ARM ISO"
- The first-ever build took 5 hours (mostly a computer emulating an ARM chip on an Intel VM).

### Approach 2 — "Buy the container from Costco and add our stuff" (the alternative)

Canonical (the company behind Ubuntu Linux) already builds an arm64 ISO — the same kind Apple Silicon Macs need. It's tested, boots reliably, and gets updates.

We could download that ISO, **open it up**, add our AI stuff on top, close it back up, and ship *that*.

**Pros:**
- Ubuntu already solved the annoying low-level bugs we're hitting.
- No 5-hour first-build cost.
- When Ubuntu 26.04 comes out next April, we get a "free" upgrade — just point at the new ISO.
- Easier for other developers to understand ("we take Ubuntu and add stuff" is a common pattern).

**Cons:**
- Ubuntu's ISO is 5 GB. Ours ends up ~5.5-6.5 GB. That's 2 GB heavier for users to download.
- Ubuntu ships **a lot** of stuff we don't need: LibreOffice, Firefox, Thunderbird, GNOME games. Every one of those is code that could have a security bug.
- The ISO says "Ubuntu" on the boot splash and in `/etc/os-release`. To rebrand it as Icebreaker we'd have to modify Canonical's ISO, which raises trademark questions.
- We're at the mercy of Canonical's decisions. If they change how their live installer works, we have to adapt.

### Which is better?

**Neither is obviously better.** They win on different dimensions.

- If you value **small, clean, controlled** → build-from-scratch (what we do).
- If you value **fast iteration, less low-level work** → Ubuntu-modify.

We already paid the biggest cost of build-from-scratch (the 5-hour setup). Switching now would throw away that investment for a modest benefit. **The right time to reconsider is when Ubuntu 26.04 LTS ships in April 2026** — then the "free upgrade" argument becomes real.

### The key insight

Both approaches have the **same** slow part: installing Python packages (`pip install`) inside an ARM environment on an Intel machine. That's slow no matter which base ISO you start from. We already solved this with a "venv cache" (built once in 55 minutes, reused in 4 seconds forever after). This means the emulation-slowness problem doesn't actually favor either approach.

---

## Technical explanation

### Definitions

- **Build-from-scratch (current)**: `debootstrap --arch=arm64 noble` under `qemu-user-static` → apply `v1..v6.manifest` overlays inside a chroot → `mksquashfs` + `grub-mkstandalone --format=arm64-efi` + `xorriso`. Base cache is a 2.2 GB tar.zst; steady-state rebuild uses the venv cache (`v2.manifest` cache-hit path).
- **Ubuntu-modify (alternative)**: `xorriso -osirrox` extract `ubuntu-24.04.1-desktop-arm64.iso` → `mount casper/filesystem.squashfs` → `chroot` in with `--bind /dev /proc /sys` → apply an adapted overlay → `mksquashfs` → `xorriso` repack preserving Canonical's isohybrid MBR + EFI structure.

### Executive comparison table

|  | Build-from-scratch | Ubuntu-modify |
|---|---|---|
| Steady-state rebuild (w/ venv cache) | ~10-15 min | ~10-15 min |
| First-ever build | 5 h (debootstrap) + 45 min | 20 min (5 GB download + repack) |
| ISO size | 3.3 GB | 5.5-6.5 GB (~4-4.5 after strip) |
| Kernel + boot chain correctness | Our problem (F-37, F-38 real bugs) | Canonical's problem |
| Package count | ~220 | ~2000 (or ~800-1000 after strip) |
| GNOME/desktop | XFCE (matches amd64) | GNOME 46 (Ubuntu default) — desktop-split risk |
| LTS upgrade cost | Full re-verify chain | Point at new ISO |
| Branding + `/etc/os-release` | Icebreaker end-to-end | Ubuntu-derived; trademark question |
| pip-under-emulation problem | Solved (venv cache) | Same problem, same fix |
| CI artifact size | 3.3 GB | 5.5+ GB |
| Contributor onboarding | Read 5-file build pipeline | "Cubic-like" familiar pattern |

### Dimension-by-dimension analysis

#### 1. Build time
- **First-ever**: Ubuntu-modify wins by 5 hours (skips debootstrap).
- **Rebuild w/ venv cache**: **tie**. Both are bottlenecked by `mksquashfs` (1-2 min) and `xorriso` (1 min). Ubuntu-modify's `mksquashfs` is over a larger tree (5.5+ GB) so slightly slower there.
- **Verdict**: Ubuntu-modify's first-build advantage is **sunk cost** for us — we already paid it.

#### 2. Kernel + boot correctness
Bugs shipped by our current pipeline in the last 24 h:
- **F-37**: `grub-efi-arm64-bin:arm64` not installed on the amd64 build VM (Ubuntu's package archive layout — amd64 archive doesn't include it, must add `ports.ubuntu.com` as multi-arch source).
- **F-38**: Ubuntu ships arm64 `vmlinuz-*` as gzip-compressed. GRUB's `arm64-efi` `linux` loader emits `error: plain image kernel not supported - rebuild with CONFIG_(U)EFI_STUB enabled` unless we `zcat` it first. amd64 `bzImage` GRUB handles gzip transparently, so this only bites cross-arch.
- **F-40**: `v2.manifest` used the unsuffixed `cx-distro/.build/mcpd` path (which is amd64), so the arm64 ISO shipped an x86-64 mcpd binary. Guest hit `[Errno 8] Exec format error: '/usr/libexec/icebreaker/mcpd'` at controller startup.

Ubuntu Desktop arm64 ISO already:
- Ships a correctly-formatted (uncompressed with EFI stub) arm64 `vmlinuz`.
- Ships an isolinux-free UEFI boot chain (`casper/`, `EFI/BOOT/BOOTAA64.EFI`) tested on Apple M-series.
- Correct `initrd.lz` with casper + firmware drivers for M-series Macs (potentially relevant for bare-metal, definitely fine for UTM Virtualize).

**Verdict**: **Ubuntu-modify wins prospectively.** F-37 and F-38 would not have existed. But we've fixed them; value is only future avoidance.

#### 3. Attack surface / package count
Build-from-scratch package count (from `dpkg -l` inside chroot):
- minbase: ~120 packages
- Our additions (Python 3.12, `wmctrl`, xrdp, XFCE core, GNOME components we explicitly pull): ~100 packages
- **Total ~220 packages**

Ubuntu Desktop 24.04 arm64 package count: **~2000 packages**, including:
- Firefox, Thunderbird, LibreOffice (Writer/Calc/Impress/Base/Draw), Rhythmbox
- GNOME Weather, Contacts, Games, Maps, Photos
- Snap Store + snapd daemon
- Full CUPS printing subsystem + PPD drivers
- Bluetooth stack (bluez + obex), NetworkManager plugins for Bluetooth/PPP/OpenVPN
- Various dev tools (git, gcc — sometimes; varies by release)

After `apt-get purge` of the obvious bloat list, realistically **~800-1000 packages**.

**Verdict**: **build-from-scratch wins by ~4×** on trust surface. For a security-critical AI OS (INV-1 through INV-8 all assume a known base), this is real.

#### 4. pip-under-emulation
The bottleneck is: our venv contains ~55 arm64 Python packages, several with C extensions (`pydantic-core`, `grpcio`, `jiter`, `protobuf`). On an amd64 build host these must either:
- (a) run pip under `qemu-user-static` inside an arm64 chroot (~55 min per rebuild without cache)
- (b) `pip install --platform manylinux_2_28_aarch64 --only-binary=:all: --target=DIR /tmp/ib-src/` — cross-arch install using pre-built wheels only (fast, but fails if any dependency lacks arm64 wheels)

We've already solved this with a venv cache keyed on `src_hash` (SHA256 over `*.py + pyproject.toml + *.toml`). Cache hit extracts a 41 MB tarball into the chroot in 4 seconds. Cache miss falls back to option (a).

**Verdict**: **irrelevant to the comparison.** Both approaches would benefit equally. Not a differentiator.

#### 5. Desktop stack consistency
Current amd64 pipeline ships **XFCE** with a custom `xfconf` config (`xfce4-desktop.xml`, `xfce4-panel.xml`, `xsettings.xml`). Rationale documented in PKG-5: GSettings has no effect on XFCE, so we standardized on XFCE both arches for consistency.

Ubuntu Desktop 24.04 arm64 ships **GNOME 46** with Ubuntu's dock, theme, and default apps.

If we go Ubuntu-modify on arm64:
- Keep GNOME → amd64 (XFCE) and arm64 (GNOME) diverge → dual-desktop maintenance
- Install XFCE on top and remove GNOME → we're back to build-from-scratch territory anyway
- Standardize both arches on GNOME → rebuild the amd64 pipeline too — bigger scope

**Verdict**: **build-from-scratch wins** for consistency. Ubuntu-modify forces a difficult choice.

#### 6. LTS upgrade path
Ubuntu 26.04 LTS releases April 2026. Post-release we'd rebase either way.

- Build-from-scratch: `UBUNTU_BASE="noble"` → `"the-new-one"` in `build-base.sh`, re-run `debootstrap` (~5 h), verify every manifest still works, hit whatever bugs the new base introduced.
- Ubuntu-modify: download new arm64 ISO, re-test overlay. Canonical may change casper/GNOME defaults, live installer flow, or partition layout.

**Verdict**: **Ubuntu-modify wins by a modest margin** — less code to audit on LTS bump.

#### 7. Branding + legal
- Build-from-scratch: `/etc/os-release`, `os-release.NAME`, boot splash, `/etc/issue`, GRUB menu title — all say Icebreaker. Full brand ownership.
- Ubuntu-modify: base ISO is Ubuntu; we'd modify `/etc/os-release` and swap splashes but the derivation is visible. Canonical's [trademark policy](https://ubuntu.com/legal/intellectual-property-policy) permits redistribution of unmodified Ubuntu but requires re-branding for modified derivatives. Doable but non-trivial paperwork.

**Verdict**: **build-from-scratch wins** for brand integrity.

#### 8. Debuggability
- Build-from-scratch: bugs are traceable to a specific manifest (v2 for venv, v3 for terminal, v6 for mcpd/PB, etc). Each manifest is <150 lines. Root causes localize quickly.
- Ubuntu-modify: bugs could originate in Canonical's live installer, casper hooks, GNOME session startup, snapd, or our overlay. Larger surface = harder to bisect.

**Verdict**: **build-from-scratch wins.** When something breaks we know where.

#### 9. Contributor onboarding
- Build-from-scratch: reading `build-base.sh` + `build-iso.sh` + `v[1-6].manifest` + `qemu-gate.sh` — takes an experienced engineer ~2 h to understand.
- Ubuntu-modify: "download ISO, modify squashfs, repack" is a widely-known pattern (Cubic, `livefs-editor`, remastersys history). Many contributors have done this before.

**Verdict**: **Ubuntu-modify wins** for onboarding new contributors — this is a real benefit if we ever open-source or need external help.

### Where Ubuntu-modify clearly wins
1. **Every ~2 years on LTS bump** — free kernel + userland upgrade with less audit surface.
2. **Installable ISO (not just live)** — Ubuntu's Subiquity/Ubiquity installer is battle-tested. Building an installer from minbase would be a large project.
3. **Bare-metal Apple Silicon** — Canonical ships firmware and Apple-hardware kernel modules we'd have to hand-curate.
4. **Contributor accessibility** — familiar pattern.

### Where build-from-scratch wins
1. **Every byte accounted for** — critical for the AI-OS security model.
2. **No external base drift** — Ubuntu can change casper/GNOME defaults between minor releases; our base is frozen until we rebuild.
3. **Cheaper CI/CD** — 3.3 GB artifacts vs 5.5+ GB.
4. **Brand integrity** — end-to-end Icebreaker.
5. **Fast bisection** — bugs localize to a specific manifest.

### What v6.6 needs right now

v6.6 arm64 is nearly done. This session has:
- ✅ Refactored 5 scripts to be `$ARCH`-aware
- ✅ Cross-compiled `mcpd-arm64` (ELF ARM aarch64, 6.7 MB) + `llama-server-arm64` (14 MB, 1634 NEON opcodes)
- ✅ Built and cached the arm64 base (`base-desktop-9c8cebbc7ce3-arm64.tar.zst`, 2.2 GB, one-time)
- ✅ Fixed F-37 (grub-efi-arm64-bin:arm64 multi-arch install)
- ✅ Fixed F-38 (arm64 vmlinuz `zcat` decompress before ISO staging)
- ✅ Fixed F-39 (cross-arch QEMU-gate timing — SOCK_MAX and L4_TIMEOUT to 300s)
- ✅ Fixed F-40 (per-arch mcpd install in v2.manifest + `file(1)` cross-check)
- ✅ Introduced venv cache in v2.manifest (55 min → 4 sec on rebuild)

Nothing here argues for pausing v6.6 mid-flight to switch approaches.

### Recommendation

**Ship v6.6 on the current build-from-scratch pipeline. Do not switch approaches now.**

For v6.7+ (after Ubuntu 26.04 releases in April 2026):
1. Re-evaluate: does the new Ubuntu 26.04 arm64 desktop ISO actually save us the audit work we projected?
2. Prototype a `remaster-ubuntu.sh` in a separate branch, off the critical path.
3. Ship v7 on whichever measures better on: **rebuild time**, **ISO size after strip**, and **post-rebase test-fix count**.
4. Until then, keep the venv cache pattern (proven 55 min → 4 sec) and consider extending it to a "post-apt-install snapshot" (would save another ~25 min per rebuild on the wmctrl+man-db trigger phase).

### If we later prototype Ubuntu-modify

**New files**:
- `incremental/build/remaster-ubuntu.sh` — analog of `build-iso.sh` for the Ubuntu-modify path.
- `incremental/build/ubuntu-iso-cache.sh` — download + SHA256-verify Canonical's ISO to a local cache.
- `incremental/versions/vN-ubuntu.manifest` — overlay adapted from `vN.manifest` for the Ubuntu chroot (main change: no need for GNOME/XFCE install, no need for kernel handling).

**Files we could reuse verbatim**:
- `incremental/tests/qemu-gate.sh` — arch-neutral; runs against any bootable ISO.
- `incremental/build/smoke-gate.sh` — chroot-neutral; runs against any staged rootfs.
- All of `dual-brain/**` (controller, terminal, GUI — pure Python, arch-neutral).
- All of `src/mcpd/**` (Rust; cross-compiled binaries just get copied in).

**Files we would NOT modify**:
- `config/archs/{amd64,arm64}.conf` — remains authoritative for cross-compile choices.
- `Makefile` — new target `iso-ubuntu-arm64` alongside `iso-arm64`.

**Verification checklist**:
1. `remaster-ubuntu.sh` produces a bootable ISO with our overlay in <20 min on the amd64 GCP VM.
2. `qemu-gate.sh --arch arm64` L0-L6 pass on the resulting ISO under cross-arch TCG.
3. UTM Virtualize boot succeeds; end-to-end `# create Hello.txt` completes in <15 s.
4. `dpkg -l` diff vs current build-from-scratch enumerates the delta Ubuntu ships.
5. Measure final ISO size after `apt-get purge` of the obvious bloat list.

---

## Summary in one paragraph

We build our ISO from scratch because we want a small, controlled, security-audited base with every kilobyte accounted for. Modifying Ubuntu's arm64 ISO would save us some low-level boot bugs but cost us ~2 GB of extra size, 4× the package count, brand ambiguity, and a new abstraction layer. The main "iteration speed" argument for Ubuntu-modify is moot because we've already solved the slow-pip problem with a venv cache. The right time to reconsider is when Ubuntu 26.04 LTS ships in April 2026; until then, keep going.
