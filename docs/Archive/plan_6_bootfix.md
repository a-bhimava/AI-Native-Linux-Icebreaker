# Phase 6 Bootfix — Hybrid BIOS+EFI ISO Boot

> **Prerequisites:** Phase 6 complete (1446 tests, PRs #15–#21 merged to `main`).
> ISO built with manual debootstrap + mksquashfs + xorriso pipeline (PR #19 `build.sh`
> Stage 5 rewrite, replacing broken `live-build` 3.0).
>
> **Owner:** DevOps engineer (`cx-distro/`).

## §0 — Status snapshot

| Milestone | Status | PR | Notes |
|---|---|---|---|
| M6-BF.1 Hybrid BIOS+EFI ISO | ⬜ Not started | — | Dockerfile + build.sh + test |

## §1 — Problem statement

The ISO produced by `cx-distro/build.sh` only boots in BIOS (legacy) mode. When loaded
in a UEFI VM (VirtualBox on macOS, QEMU with OVMF, most post-2012 physical hardware),
the firmware drops to the **UEFI Interactive Shell** instead of booting the OS.

**Symptoms:** "UEFI Interactive Shell v2.2 / EDK II / Shell>" prompt, no boot menu.

**Root cause:**

1. Stage 5f sets up **isolinux only** (BIOS boot via `isolinux.bin`, `isohdpfx.bin`,
   syslinux modules `ldlinux.c32`, `vesamenu.c32`).
2. Stage 5g calls `xorriso -as mkisofs` with only BIOS El Torito flags
   (`-b isolinux/isolinux.bin -no-emul-boot -boot-load-size 4 -boot-info-table`).
3. There is **no EFI System Partition**, no GRUB EFI binary, and no GPT partition
   entry on the ISO.
4. UEFI firmware looks for `/EFI/BOOT/BOOTX64.EFI` on any accessible FAT partition.
   Finding none, it falls through to the built-in UEFI Shell.

**Impact:** The ISO cannot boot on any machine or VM using UEFI firmware, which includes
VirtualBox on macOS (always UEFI), modern QEMU defaults, and virtually all hardware
manufactured after ~2012.

## §2 — Architecture decision

| ADR | Decision | Key constraint |
|---|---|---|
| ADR-BF1 | **Hybrid BIOS+EFI ISO** | Single ISO image boots in both BIOS and UEFI mode. Existing BIOS/isolinux path unchanged. |
| ADR-BF2 | **`grub-mkstandalone`** for EFI binary | Embeds modules + `grub.cfg` into a single monolithic EFI binary. Simpler and more robust than `grub-mkimage` + manual module tree. |
| ADR-BF3 | **mtools for FAT image** (no loopback mount) | Uses `dd` + `mkfs.fat` + `mmd`/`mcopy` to build FAT ESP image. Avoids `mount -o loop` which is fragile in containers. `mtools` and `dosfstools` already in `Dockerfile.build`. |
| ADR-BF4 | **No Secure Boot** | Development/live ISO. `shim-signed` deferred to Phase 7 hardening. Users with Secure Boot must disable it for now. |
| ADR-BF5 | **Volume label linkage** | GRUB `search --label ICEBREAKER` matches xorriso `-V "ICEBREAKER"`. Single source of truth for the label. |

## §3 — Files touched

### `cx-distro/Dockerfile.build`

| Action | Line | Change |
|---|---|---|
| MOD | 3–9 (apt-get) | Add `grub-efi-amd64-bin` after `isolinux syslinux-common` |

`grub-efi-amd64-bin` provides:
- `/usr/bin/grub-mkstandalone` — builds standalone EFI binaries
- `/usr/lib/grub/x86_64-efi/` — GRUB module files for x86_64-efi target

Already present (no change needed): `mtools` (provides `mmd`, `mcopy`), `dosfstools`
(provides `mkfs.fat`).

### `cx-distro/build.sh`

Two modifications within Stage 5:

#### New sub-stage 5f2: Set up GRUB EFI bootloader

Insert between stage 5f (isolinux setup) and stage 5g (xorriso assembly).

```bash
# ── 5f2: Set up GRUB EFI bootloader ───────────────────────────────────
info "Setting up GRUB EFI bootloader..."

command -v grub-mkstandalone >/dev/null 2>&1 || \
    die "grub-mkstandalone not found — install grub-efi-amd64-bin"

GRUB_CFG_DIR="${ISO_WORK}/grub-embed"
mkdir -p "${GRUB_CFG_DIR}"

cat > "${GRUB_CFG_DIR}/grub.cfg" <<'GRUBCFG'
search --no-floppy --set=root --label ICEBREAKER

set default=0
set timeout=5

menuentry "Icebreaker AI-Native OS (Live)" {
    linux /live/vmlinuz boot=live toram quiet splash
    initrd /live/initrd
}

menuentry "Safe Mode" {
    linux /live/vmlinuz boot=live toram single nomodeset
    initrd /live/initrd
}
GRUBCFG

mkdir -p "${ISO_STAGING}/boot/grub"
grub-mkstandalone \
    --format=x86_64-efi \
    --output="${ISO_STAGING}/boot/grub/BOOTX64.EFI" \
    --modules="part_gpt part_msdos fat iso9660 search search_label linux normal all_video test" \
    "boot/grub/grub.cfg=${GRUB_CFG_DIR}/grub.cfg"
[ -f "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ] || \
    die "grub-mkstandalone failed to produce BOOTX64.EFI"
info "GRUB EFI binary: $(du -h "${ISO_STAGING}/boot/grub/BOOTX64.EFI" | awk '{print $1}')"

EFI_IMG="${ISO_STAGING}/boot/grub/efi.img"
EFI_IMG_SIZE_KB=3584  # 3.5 MB

dd if=/dev/zero of="${EFI_IMG}" bs=1K count="${EFI_IMG_SIZE_KB}" 2>/dev/null
mkfs.fat -F 12 "${EFI_IMG}" >/dev/null
mmd -i "${EFI_IMG}" ::EFI
mmd -i "${EFI_IMG}" ::EFI/BOOT
mcopy -i "${EFI_IMG}" "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ::EFI/BOOT/BOOTX64.EFI

info "EFI image: $(du -h "${EFI_IMG}" | awk '{print $1}')"
```

**Key design notes:**

- `search --no-floppy --set=root --label ICEBREAKER` finds the ISO9660 filesystem by
  its volume label, which must match the `-V "ICEBREAKER"` flag in the xorriso call.
- `grub-mkstandalone` with `--modules` preloads essential modules so they don't need
  to be found on disk at runtime. Module set covers: partition tables (`part_gpt`,
  `part_msdos`), filesystems (`fat`, `iso9660`), search (`search`, `search_label`),
  and boot (`linux`, `normal`, `all_video`, `test`).
- FAT image built with `dd` + `mkfs.fat` + mtools (`mmd`/`mcopy`), avoiding
  loopback `mount` which is fragile in containers.
- `EFI/BOOT/BOOTX64.EFI` is the standard UEFI fallback path — firmware searches
  this location when there is no NVRAM boot entry.

#### Modified stage 5g: xorriso call

Append 4 flags to the existing xorriso invocation:

```bash
xorriso -as mkisofs \
    -isohybrid-mbr "$ISOHDPFX" \
    -c isolinux/boot.cat \
    -b isolinux/isolinux.bin \
    -no-emul-boot \
    -boot-load-size 4 \
    -boot-info-table \
    -eltorito-alt-boot \
    -e boot/grub/efi.img \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    -V "ICEBREAKER" \
    -o "${ISO_FILE}" \
    "${ISO_STAGING}/" 2>&1 | tail -5
```

| New flag | Purpose |
|---|---|
| `-eltorito-alt-boot` | Starts a second El Torito boot catalog entry (first is isolinux for BIOS) |
| `-e boot/grub/efi.img` | Points second entry to the EFI System Partition image |
| `-no-emul-boot` | EFI image is raw FAT, not floppy/HDD emulation |
| `-isohybrid-gpt-basdat` | Writes a GPT partition entry (type "Basic Data") for the EFI image. UEFI firmware reads GPT, finds FAT, loads `BOOTX64.EFI` |

Existing BIOS flags are **completely unchanged**.

### `cx-distro/tests/test_build_output.sh`

| Action | Section | Change |
|---|---|---|
| MOD | Static checks | Add: `check "Dockerfile installs grub-efi-amd64-bin" grep -q 'grub-efi-amd64-bin' "${CX_DIR}/Dockerfile.build"` |

## §4 — Boot flow after fix

### BIOS boot (unchanged)

```
MBR (isohdpfx.bin)
  → isolinux.bin (El Torito entry 1)
    → isolinux.cfg (vesamenu)
      → "Icebreaker AI-Native OS (Live)"
        → /live/vmlinuz + /live/initrd  boot=live toram quiet splash
```

### UEFI boot (new)

```
GPT (isohybrid-gpt-basdat)
  → FAT partition (efi.img, El Torito entry 2)
    → /EFI/BOOT/BOOTX64.EFI (grub-mkstandalone)
      → embedded grub.cfg
        → search --label ICEBREAKER → finds ISO9660
          → "Icebreaker AI-Native OS (Live)"
            → /live/vmlinuz + /live/initrd  boot=live toram quiet splash
```

Both paths load the **same kernel** with the **same parameters**.

## §5 — Build flow

```
Stage 5f:   Set up isolinux (BIOS) .............. unchanged
Stage 5f2:  Set up GRUB EFI .................... NEW
  5f2.1:    Write grub.cfg (embedded config)
  5f2.2:    grub-mkstandalone → BOOTX64.EFI
  5f2.3:    dd + mkfs.fat + mcopy → efi.img (3.5 MB FAT12)
Stage 5g:   xorriso BIOS + EFI flags ........... MODIFIED (+4 flags)
```

ISO size impact: +3.5 MB (the EFI image).

## §6 — Verification

| # | Check | Command / method |
|---|---|---|
| 1 | Syntax | `bash -n build.sh` |
| 2 | Docker build | `docker build -t icebreaker-build -f Dockerfile.build ..` succeeds |
| 3 | ISO build | Full Stage 5 completes with `--no-models` |
| 4 | El Torito entries | `xorriso -indev icebreaker.iso -report_el_torito as_mkisofs` → 2 entries |
| 5 | BIOS boot | `qemu-system-x86_64 -m 8G -boot d -cdrom icebreaker.iso -enable-kvm` → isolinux menu |
| 6 | UEFI boot | `qemu-system-x86_64 -m 8G -cdrom icebreaker.iso -enable-kvm -bios /usr/share/OVMF/OVMF_CODE.fd` → GRUB menu |
| 7 | VirtualBox EFI | Create VM with EFI enabled, attach ISO → GRUB menu appears, Live entry boots |

## §7 — Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `grub-mkstandalone` not in Noble | Very low | Blocks build | `grub-efi-amd64-bin` is in Ubuntu Noble `main` |
| R2 | Missing GRUB module causes boot hang | Low | No EFI boot | `--modules` preloads all essential modules; tested module set covers full boot path |
| R3 | Volume label mismatch | Low | GRUB can't find ISO | Both use `ICEBREAKER` — xorriso `-V` flag unchanged, grub.cfg `search --label` matches |
| R4 | FAT image too small | Very low | `mcopy` fails | 3.5 MB >> typical 2.5 MB GRUB binary; `mcopy` fails loudly if insufficient |
| R5 | Secure Boot rejection | Medium | No boot on SB-enforced hardware | Out of scope — document in release notes; add `shim-signed` in Phase 7 |
| R6 | BIOS boot regression | Very low | Existing users lose boot | isolinux setup (5f) untouched; xorriso BIOS flags unchanged; EFI is additive only |
| R7 | INV-7 violation | None | Security regression | No model paths touched; checksum verification in Stage 0 and first-boot unchanged |

## §8 — Security review notes

`build.sh` is listed in CLAUDE.md **Security-Critical Files**. Per WF-6, any change
requires human review from the module owner AND a second reviewer.

This change:
- Does NOT modify sandbox, Landlock, seccomp, or any runtime security policy
- Does NOT modify model checksum verification (INV-7)
- Does NOT modify mcpd, Controller, or any brain code
- Does NOT add network listeners (INV-3)
- Is strictly limited to ISO boot infrastructure (bootloader chain)
- Adds a second boot path alongside existing one — both load identical kernel + params

Despite being low-risk from a security perspective, human review is required per policy.

## §9 — Execution plan

This fix requires rebuilding the ISO on the GCP VM (`icebreaker-phase2-vm`). Steps:

1. Push `cx-distro/Dockerfile.build` and `cx-distro/build.sh` changes to feature branch
2. Pull on GCP VM
3. Rebuild Docker image: `docker build -t icebreaker-build -f Dockerfile.build ..`
4. Run build: `docker run --privileged -v $(pwd)/..:/build icebreaker-build --no-models`
   (or full build with models if needed)
5. Download ISO (chunk-based SCP — see previous session notes)
6. Test in VirtualBox (EFI mode) — GRUB menu should appear
7. Test in VirtualBox (BIOS mode, if available) — isolinux menu should appear
8. If both pass, merge to main

---

*Written: June 2026. Addresses ISO boot failure discovered during VirtualBox testing
of the Phase 6 ISO.*
