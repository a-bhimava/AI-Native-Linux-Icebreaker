# M2.5 / M2.8 → Phase 6 distro migration map

When this work ships as part of an OS image (Phase 6 — ISO distribution),
every dev-time path / process / config surface here has an explicit
production form. This document maps the two so reviewers can verify
"M2.5 is forward-compatible with the distro" without having to imagine
the translation.

Every row below is a **configuration flip**, not a code rewrite. That's
the acceptance criterion for plug-and-play.

## Files & paths

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| `dual-brain/controller/catalogue.toml` | `/usr/share/icebreaker/models/catalogue.toml` | Ships in the signed OS image. Admin can extend via `/etc/icebreaker/catalogue.d/*.toml` drop-ins. |
| `models/` (repo-relative search dir) | `/var/lib/icebreaker/models/` (system pool) + `~/.local/share/icebreaker/models/` (user pool) | Registry searches all configured `search_dirs` in order, first match wins. Already plumbed in M2.5 via `[paths].model_search_dirs`. |
| `models/checksums.sha256` | `/var/lib/icebreaker/models/checksums.sha256` (system) + per-user equivalent | Append-only; written by `install` subcommand. |
| `dual-brain/controller/grammars/qb_intent.gbnf` | `/usr/share/icebreaker/grammars/qb_intent.gbnf` | Read-only at runtime; part of the OS image. |
| `dual-brain/controller/schemas/intent.json` | `/usr/share/icebreaker/schemas/intent.json` | Same. |
| `dual-brain/scripts/locations.env` | `/etc/icebreaker/locations.env` | Admin-editable; sourced by systemd unit `EnvironmentFile=`. |
| `dual-brain/scripts/start_qb_local.sh` | `/usr/libexec/icebreaker/start-qbd` (called by `icebreaker-qbd.service`) | Script body unchanged — only the location and the runner change. |
| `dual-brain/scripts/start_pb.sh` | `/usr/libexec/icebreaker/start-pbd` | Same. |
| `controller.toml.example` | `/etc/icebreaker/controller.toml` (system defaults) + `~/.config/icebreaker/controller.toml` (user overrides) | XDG-correct in M2.5 already. |

## Processes & UIDs

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| `llama-server` started by `bash scripts/start_qb_local.sh &` | `icebreaker-qbd.service` (`User=_qb`, systemd unit) | Dedicated unprivileged service user. Restart on crash with exponential backoff. Cgroup-bounded memory/cpu/gpu. |
| Same for PB | `icebreaker-pbd.service` (`User=_pb`) | Same shape, different user, different port. |
| User runs `python -m controller` directly | User runs `icebreaker` (CLI entry point) or via desktop launcher | The Controller still runs as the calling user — no privilege elevation. |
| mcpd is a subprocess of the Controller | `icebreaker-mcpd@.service` (per-user systemd unit, runs as `$USER`) | mcpd outlives any single Controller invocation. Faster cold start. |
| HITL is inline in Controller stdin/stdout | `icebreaker-hitl-agent.service` (per-user, multi-modal: TTY / desktop modal / SSH-aware / headless-deny) | Decided in M2.9. |

## IPC

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| llama-server on `http://127.0.0.1:8080` (PB) and `:8081` (QB) | UNIX sockets at `/run/icebreaker/pbd.sock` and `/run/icebreaker/qbd.sock`, mode `0660`, group `icebreaker-users` | `LocalBackend.transport="unix"` branch lands. `SO_PEERCRED` authenticates the calling user. |
| mcpd via stdio (Controller's subprocess) | UNIX socket at `/run/user/$UID/icebreaker/mcpd.sock`, mode `0600` | One mcpd per user. |
| HITL via Controller's own stdin | UNIX socket at `/run/user/$UID/icebreaker/hitl.sock` | HITL daemon owns the 3s lockout, not the Controller. |

## Audit & logging

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| `~/.local/state/icebreaker/controller-audit.log` | `/var/log/icebreaker/$USER/controller.audit.jsonl` | Owned by `_audit:adm`, mode 0640. Written via a setuid helper that prepends `prev_hash` and signs the row. User can READ (group `adm`), not modify. |
| `~/.pb_audit.jsonl` (V1 shell trigger, unrelated) | Untouched in M2.5/M2.8; out of scope | — |
| mcpd's audit log (existing) | `/var/log/mcpd/audit.log`, `_mcpd:adm` | Per-user file too; `_audit` user reads both. |
| `KeyRedactionFilter` on root logger | Same | Already installed at `config.load()`. journald picks up scrubbed records. |

## Config layering

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| Single `controller.toml.example` in repo | Hierarchical merge: `/etc/icebreaker/controller.toml` ← `/etc/icebreaker/controller.toml.d/*.conf` ← `~/.config/icebreaker/controller.toml` ← `$ICEBREAKER_CONFIG` env var ← CLI flags | Standard sshd/sudo pattern. Already foundationally compatible — M2.4's `load()` takes a single path; layering wraps it. |
| `[paths].model_search_dirs` is repo-relative | Distro defaults: `["/var/lib/icebreaker/models", "~/.local/share/icebreaker/models"]` | Config field already exists. |

## Catalogue & signing

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| Catalogue ships in repo, sha256 receipts written at `install` time | Catalogue is part of the signed OS image (OSTree commit or dpkg signature) | `verify` subcommand re-checks signatures + sha256. |
| `install` downloads from HuggingFace | Same — but optional offline channel via admin-uploaded `/var/lib/icebreaker/models/incoming/` | The `install` source is the catalogue's `source` field; admins can add a `[model.source] type="file"` entry pointing at a local file pool. |
| License-acceptance gate | Same flow; UI installer surfaces the license text | Already enforced in `install` via `accept_license` flag. |

## Update / rollback

| M2.5 (dev) | Phase 6 (distro) | Notes |
|---|---|---|
| Manual `git pull` + edit + restart | `rpm-ostree update` (OSTree) or `apt upgrade` (dpkg) | Atomic. New catalogue + new grammar + new mcpd ship as one commit. |
| Model upgrade = edit `model_id` + `start_qb_local.sh &` | `icebreaker models use qb <id>` (CLI) atomic-updates config + restarts unit | Phase 5/6 deliverable. Foundation is in M2.5 (the registry resolver works the same). |

## What's tested today vs verified by Phase 6 work

### Tested today (M2.5+M2.8)

* Catalogue parses, looks up, filters by hardware, refuses unknown ids, refuses unverified files
* `install` flow works against a mocked download (real-HF in optional smoke)
* GBNF ⊆ jsonschema parity holds over 10K hypothesis fuzz
* `LocalBackend` honors `transport="http"`, fails fast on `transport="unix"`, attaches grammar, emits no `tools` key, passes auditor, retries on schema failure
* Config schema rejects raw filenames in local section; requires `model_id`
* Ops scripts are syntactically valid bash with no interactive prompts

### Verified at Phase 6 land (NOT in M2.5)

* Service users (`_pb`, `_qb`, `_audit`) and dedicated cgroups
* UNIX socket transport with `SO_PEERCRED`
* `_audit`-owned tamper-resistant log rotation
* OSTree commit signature verification
* HITL daemon as a separate service
* Multi-user concurrent sessions on shared inference daemons

## Acceptance criterion the plan committed to

> "Phase 6 migration is config-only" — documented in `m25_production_migration.md`; every code path supports both transport modes already.

Verification of this criterion:

```bash
# In M2.5 code, search for any code path that hardcodes a transport choice:
grep -rn 'transport\s*=\s*["'"'"']http' dual-brain/controller/ --include='*.py'
# Expected: only the BackendConfig field default (transport: str = "http")
# and the M2.5 NotImplementedError branch for "unix".

# Search for any hardcoded GGUF filename in Python code:
grep -rn '\.gguf' dual-brain/controller/ --include='*.py' \
  | grep -v test_ | grep -v catalogue.toml
# Expected: empty (the catalogue is the only place gguf filenames live).
```

If either grep returns Controller-code matches that aren't tests or the
catalogue, the plug-and-play guarantee has regressed.
