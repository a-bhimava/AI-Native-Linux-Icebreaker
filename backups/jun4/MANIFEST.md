# VM backup — 2026-06-05

Snapshot of GCP VM `instance-20260528-030421` (us-central1-a) before Phase 2 implementation begins. Captured immediately after the Phase 1 mcpd PR landed (commit `741bd3c`).

## Contents

| Tarball | Source on VM | Extracted to | Size | Notes |
|---|---|---|---|---|
| `icebreaker-jun4.tar.gz` | `~/icebreaker/` + `~/mcpd-phase1.tar.gz` | `icebreaker/` | 162 KB | source tree minus `target/` build artifacts and `Cargo.lock` |
| `audit-logs-jun4.tar.gz` | `/var/log/mcpd/` + `/var/log/audit/audit.log` | `var-log/` | 18 KB | Phase 1 audit log (442 entries) + system audit log (sigsys records from verification runs) |

## What's worth keeping in here

- **`icebreaker/icebreaker/src/mcpd/fuzz/corpus/validate/`** — 69 distinct interesting inputs. The repo only committed the 12 seeds; the additional 57 are libFuzzer's discoveries during the canonical 60 s run. Useful seed for future fuzz runs.
- **`var-log/var/log/mcpd/audit.log`** — 442 JSONL entries from Phase 1 testing. Contains the only on-VM record of every JSON-RPC dispatch during exit-gate verification.
- **`var-log/var/log/audit/audit.log`** — kernel auditd records. Includes the `sig=31` (SIGSYS) records from the strict-mode seccomp verification (where `epoll_wait` and `dup2` were caught before being added to the allowlist).

## What's intentionally NOT included

- `target/` directories (regenerable via `cargo build`)
- `~/.cargo/`, `~/.rustup/` (toolchain, regenerable via `rustup`)
- `~/llama.cpp/` (~500 MB build, regenerable)
- `~/conversion/`, `~/training/`, `~/data/`, `~/eval/`, `~/models/` (Phase 4 artifacts tracked separately under `privileged-brain/` in the main repo; PB GGUF model regenerable from the SFT/DPO pipeline)

## Restoration

```bash
# Reupload to a fresh VM:
cd backups/jun4
gcloud compute scp icebreaker-jun4.tar.gz <new-vm>:~/ --zone=<zone>
ssh <new-vm> tar xzf ~/icebreaker-jun4.tar.gz

# Restore the audit log (Phase 1 reference)
gcloud compute scp audit-logs-jun4.tar.gz <new-vm>:/tmp/ --zone=<zone>
ssh <new-vm> sudo tar xzf /tmp/audit-logs-jun4.tar.gz -C /
```

## Companion: `dual-brain/` staging directory

Phase 2 work happens in `../../dual-brain/` (top-level of the repo). When ready to push to the VM, tar that directory and scp it — it's self-contained.
