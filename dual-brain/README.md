# dual-brain/ — Phase 2 deployable bundle

This directory is the **self-contained Phase 2 source tree**. Everything that needs to land on the GCP VM for the Dual-Brain Controller lives here. When we ship to the VM, we tar this directory and scp the tarball.

## Why this directory exists

Phase 1 (mcpd) stayed under `src/mcpd/` because it was built and tested locally with `cargo`. Phase 2 (Controller) is Python, runs against API providers (Anthropic, Gemini) and a local llama-server, and is dev'd + verified on the VM only (per scope decision 2026-06-05). Keeping Phase 2 isolated in `dual-brain/` makes the deploy story trivial: **tar dual-brain → scp → extract on VM → run.**

## Layout

```
dual-brain/
├── controller/                  # The Python package — runtime code
│   ├── __init__.py
│   ├── __main__.py              # python -m controller entry point
│   ├── main.py                  # Controller orchestrator (14-step pipeline)
│   ├── config.py                # TOML config loader + dataclasses
│   ├── schemas/intent.json      # Intent Object JSON Schema (whitepaper §3)
│   ├── intent_store.py          # Thread-safe UUID → Intent store, TTL=300 s
│   ├── risk_classifier.py       # Tier 0–3 classification + pluggable registry
│   ├── backends/                # QB/PB brain backends (local, anthropic, gemini)
│   ├── session.py               # Multi-turn session state + cost tracking
│   ├── audit.py                 # Append-only JSONL audit log (INV-8, hash-chain)
│   ├── audit_viewer.py          # Interactive TUI audit log viewer
│   ├── hitl.py                  # Hardened HITL approval gate (SF-1/SF-2/SF-3)
│   ├── keymap.py                # Configurable keybindings
│   ├── trust_store.py           # Session-scoped trust grants
│   ├── tier2_review.py          # Tier-2 escalate-only review
│   ├── repl.py                  # Interactive multi-turn REPL
│   ├── mcpd_client.py           # JSON-RPC 2.0 client for mcpd (env-scrubbed)
│   ├── prompts/                 # System prompts for QB/PB/verifier/reviewer
│   ├── ci.sh                    # Exit-gate verification (G1–G11, G5.1–G5.P1b)
│   └── tests/                   # 1060+ tests (pytest + hypothesis)
├── scripts/                     # Operator scripts for VM deployment
│   ├── export_mcpd_catalogue.py
│   ├── start_pb.sh
│   └── start_qb_local.sh
├── docs/phase2/                 # Phase 2 planning docs
├── requirements.txt             # Python deps
├── .gitignore
└── README.md                    # This file
```

## Deploy workflow (for any milestone)

```bash
# From repo root, on dev machine:
cd "/Users/aditya/Documents/Icebreaker"
tar czf /tmp/dual-brain.tar.gz \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='*.pyc' \
  dual-brain/

gcloud compute scp /tmp/dual-brain.tar.gz \
  instance-20260528-030421:~/dual-brain.tar.gz \
  --zone=us-central1-a

# On VM:
ssh ... 'cd ~ && rm -rf dual-brain && tar xzf dual-brain.tar.gz && cd dual-brain && pip install -r requirements.txt && pip install -e .'
```

## Where the live mcpd binary lives

mcpd was built and tested in Phase 1 and lives at `~/icebreaker/src/mcpd/target/release/mcpd` on the VM. The Controller's `McpdClient` (M2.1) spawns that binary as a subprocess. The Controller and mcpd are deployed independently — the Controller does not need to rebuild mcpd.

## What stays under `src/` in the main repo

- `src/mcpd/` — Phase 1, locked. Already on main.
- `shell/pb_*` — V1 shell trigger, locked. Coexists with the Dual-Brain Controller as a separate UX path.

## Backups

The `backups/jun4/` directory at the repo root has a snapshot of the VM as of 2026-06-05 (immediately after Phase 1 merged). See `backups/jun4/MANIFEST.md` for what's in it and how to restore.
