# Plan: M2.5 + M2.8 — LocalBackend + `qb_intent.gbnf` + plug-and-play model registry

## Prior context — already shipped

M2.0–M2.4, M2.6, M2.7 are on `feature/phase2-controller` (commit `3edbe26`). M2.5 (LocalBackend) and M2.8 (GBNF grammar) close the QB-backend trio.

## What changed in this revision

Two iterations have happened on this plan inside this conversation:

1. **First pass:** dev-VM-shaped, with `Phi-4-mini-instruct-Q4_K_M` hardcoded as the QB model
2. **Second pass:** swapped Phi-4-mini → Qwen 2.5 1.5B-Instruct + 0.5B draft (Phi-4-mini at ~3 GB is too slow on Apple M4; ~2,800 ms per intent versus the 500 ms G9 budget). Added speed-profile shorthands.
3. **This pass (the user's ask):** the model itself must be **plug-and-play** — selectable at first run, swappable as the user upgrades hardware, upgradable as better small models ship. **No model hardcoded in code or in shell scripts.** Everything routes through a catalogue.

The user's specific instruction:
> "we need to make the model plug and play - we need to be able to change the model as we upgrade the computer - and also select the starting model"

This means the design has to support:
- **First-run model selection** (hardware probe → recommendation → user confirms)
- **Runtime swap** (user picks a different model later, no code change, no manual file shuffling)
- **Hardware-upgrade adaptation** (more RAM → larger/smarter model becomes available)
- **Multiple coexisting models** (user can keep two on disk, switch between them)
- **Per-role independence** (QB model and PB model selected separately)

## Architectural shape — model registry as a first-class subsystem

```
                                                                                 
   /usr/share/icebreaker/models/catalogue.toml      ◄── declarative catalogue   
       Lists every supported model with sha256,                                 
       file_name, size, RAM/disk requirements,                                  
       license, source URL, hardware tags                                       
                          │                                                     
                          ▼                                                     
   controller/model_registry.py                                                 
       Parses catalogue, resolves model_id → file path,                         
       verifies checksums, filters by hardware,                                 
       recommends defaults, manages downloads                                   
                          │                                                     
                ┌─────────┼─────────┬──────────────┐                            
                ▼         ▼         ▼              ▼                            
            list     install    resolve       recommend                        
          (CLI)      (CLI)      (lookup)      (first-run)                      
                                                                                 
   ───── consumed by ─────                                                      
                                                                                 
   controller.toml         scripts/start_qb_local.sh    config.py              
       model_id = "..."        looks up file path        BackendConfig         
                               via registry              .model_id             
                                                                                 
   Files on disk live in EITHER:                                                
     - System pool: /var/lib/icebreaker/models/  (admin-installed)              
     - User pool:   ~/.local/share/icebreaker/models/  (user-installed)         
     - Repo dev:    models/                      (this session's CWD)           
                                                                                 
   The registry searches all three; first match wins.                           
```

The Controller never sees a file path in code. It only sees `model_id`. The registry handles every concrete-file detail.

## The catalogue file — `controller/catalogue.toml`

A declarative, machine-readable list of supported models. Ships with the project; can be extended by admins (drop-ins in `/etc/icebreaker/catalogue.d/*.toml` for Phase 6).

```toml
schema_version = "1"

# ─── QB candidates ──────────────────────────────────────────────────────────

[[model]]
id = "qwen-2.5-1.5b-instruct-q4_k_m"
display_name = "Qwen 2.5 1.5B Instruct"
role = "qb"
family = "qwen2.5"
file_name = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
parameter_count_b = 1.5
quantization = "Q4_K_M"
size_bytes = 986_500_000
sha256 = "fill-in-from-trusted-mirror"
license = "Apache-2.0"
min_ram_mb = 2000
min_disk_mb = 1000
tags = ["default", "balanced", "small-hardware"]
description = "Balanced QB. Strong instruction-following at modest size."

[model.source]
type = "huggingface"
repo = "lmstudio-community/Qwen2.5-1.5B-Instruct-GGUF"
file = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"

[model.throughput_estimates]
m4 = 70                # tokens/sec generation
rtx_4060 = 110
cpu_only = 12

# ---

[[model]]
id = "qwen-2.5-0.5b-instruct-q4_k_m"
display_name = "Qwen 2.5 0.5B Instruct"
role = "qb"
family = "qwen2.5"
file_name = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
parameter_count_b = 0.5
quantization = "Q4_K_M"
size_bytes = 400_000_000
sha256 = "fill-in"
license = "Apache-2.0"
min_ram_mb = 600
min_disk_mb = 500
tags = ["fast", "low-ram", "experimental-as-qb"]
description = "Fast QB. Best when GBNF + small action surface keeps the task narrow."
[model.source]
type = "huggingface"
repo = "lmstudio-community/Qwen2.5-0.5B-Instruct-GGUF"
file = "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"

# ---

[[model]]
id = "phi-4-mini-instruct-q4_k_m"
display_name = "Phi-4 Mini Instruct"
role = "qb"
parameter_count_b = 3.8
quantization = "Q4_K_M"
file_name = "phi-4-mini-instruct-q4_k_m.gguf"
size_bytes = 3_000_000_000
sha256 = "fill-in"
license = "MIT"
min_ram_mb = 4000
min_disk_mb = 3500
tags = ["quality", "large-hardware"]
description = "Strong instruction-following; slow on CPU-only or < 8 GB systems."
[model.source]
type = "huggingface"
repo = "microsoft/Phi-4-mini-instruct"
file = "Phi-4-mini-instruct-Q4_K_M.gguf"

# ---

[[model]]
id = "gemma-3-2b-it-q4_k_m"
display_name = "Gemma 3 2B-IT"
role = "qb"
parameter_count_b = 2.0
quantization = "Q4_K_M"
file_name = "gemma-3-2b-it-q4_k_m.gguf"
size_bytes = 1_600_000_000
sha256 = "fill-in"
license = "Gemma-Terms-of-Use"
min_ram_mb = 3000
tags = ["balanced", "google"]
[model.source]
type = "huggingface"
repo = "google/gemma-3-2b-it-gguf"
file = "gemma-3-2b-it-q4_k_m.gguf"

# ─── PB (kept; this is the whitepaper-locked default) ─────────────────────

[[model]]
id = "qwen-2.5-coder-1.5b-instruct-q4_k_m"
display_name = "Qwen 2.5 Coder 1.5B Instruct"
role = "pb"
file_name = "run7_cot_q4km.gguf"          # already on disk!
parameter_count_b = 1.5
sha256 = "4c3c4628f193e32c6e7f0fed61ff6007fcc8f3d4ff518c60b214cf28c7fcad7c"
size_bytes = 940_000_000
license = "Apache-2.0"
min_ram_mb = 2000
tags = ["default", "whitepaper-locked"]
description = "Privileged Brain — fine-tuned on bash command emission. Locked by whitepaper."

# ─── Draft models (used for speculative decoding) ─────────────────────────

[[model]]
id = "qwen-2.5-coder-0.5b-instruct-q4_k_m"
display_name = "Qwen 2.5 Coder 0.5B (draft)"
role = "draft"
file_name = "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"
parameter_count_b = 0.5
sha256 = "fill-in"
size_bytes = 300_000_000
license = "Apache-2.0"
tags = ["draft", "speculative-decoding"]
description = "Speculative-decoding draft. Shared by PB and QB inference servers."
[model.source]
type = "huggingface"
repo = "lmstudio-community/Qwen2.5-Coder-0.5B-Instruct-GGUF"
file = "Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf"
```

## `controller/model_registry.py` — the resolver

```python
# Public API:
class ModelRegistry:
    def __init__(self, catalogue_path: Path,
                 search_dirs: list[Path]) -> None: ...
    def get(self, model_id: str) -> ModelEntry: ...
    def list_by_role(self, role: str) -> list[ModelEntry]: ...
    def filter_by_hardware(self, available_ram_mb: int,
                           available_disk_mb: int) -> list[ModelEntry]: ...
    def resolve_to_file(self, model_id: str) -> Path:
        """Search the configured dirs in order; return first match.
        Verifies sha256 before returning. Raises ModelNotInstalledError
        if the file isn't present anywhere."""
    def verify(self, model_id: str) -> bool: ...
    def installed_ids(self) -> set[str]: ...

# CLI subcommands (python -m controller.model_registry ...):
#   list [--role <role>] [--installed-only]
#   resolve --id <model_id>
#   verify --id <model_id>
#   install --id <model_id> [--dir <pool>]    (downloads if absent, verifies)
#   recommend                                  (hardware probe + suggested ids)
```

A `download.py` helper handles HuggingFace fetches with progress bar + SHA-256 verification; the `install` subcommand wraps it. Networked path (download) is opt-in; the registry never auto-downloads at runtime — only on explicit `install` invocation.

## `controller.toml.example` — model selection by ID

```toml
[qb]
backend = "local"

[qb.local]
model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
draft_model_id = "qwen-2.5-coder-0.5b-instruct-q4_k_m"  # optional
endpoint = "http://127.0.0.1:8081"
transport = "http"
grammar_path = "dual-brain/controller/grammars/qb_intent.gbnf"
max_tokens = 512
timeout_seconds = 30

[pb]
model_id = "qwen-2.5-coder-1.5b-instruct-q4_k_m"
draft_model_id = "qwen-2.5-coder-0.5b-instruct-q4_k_m"
endpoint = "http://127.0.0.1:8080"
# ... (PB config; M2.5 adds the [pb] section since M2.4 only had [qb.*])

[paths]
# Where the registry searches for installed model files
model_search_dirs = [
    "models",                              # repo-relative (dev)
    "~/.local/share/icebreaker/models",    # user pool
    "/var/lib/icebreaker/models",          # system pool (Phase 6)
]
catalogue_path = "dual-brain/controller/catalogue.toml"
```

## First-run experience

```
$ python3 -m controller.model_registry recommend
Hardware probe:
  RAM:      16 GB (15.6 GB usable)
  Disk:     45 GB free in $HOME
  GPU:      none detected
  CPU:      Apple M4 (10 cores)
Recommended for this hardware:
  QB role  → qwen-2.5-1.5b-instruct-q4_k_m    (1.5B, ~70 tok/s)
  PB role  → qwen-2.5-coder-1.5b-instruct-q4_k_m (already installed)
  Draft    → qwen-2.5-coder-0.5b-instruct-q4_k_m

Models to install (1.25 GB total):
  - qwen-2.5-1.5b-instruct-q4_k_m (950 MB, Apache-2.0)
  - qwen-2.5-coder-0.5b-instruct-q4_k_m (300 MB, Apache-2.0)

Run `python3 -m controller.model_registry install --id <id>` for each, then add to
~/.config/icebreaker/controller.toml:

  [qb.local]
  model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
  draft_model_id = "qwen-2.5-coder-0.5b-instruct-q4_k_m"
```

(Interactive wizard / GUI selector is Phase 5; the M2.5 surface is the CLI above.)

## Runtime swap — what happens when the user changes `model_id`

1. User edits `~/.config/icebreaker/controller.toml` (or runs `... install --id <new>` then edits)
2. User runs `systemctl --user restart icebreaker-qb-local` (Phase 6) OR re-runs `bash scripts/start_qb_local.sh` (today)
3. `start_qb_local.sh` reads `model_id`, queries the registry, gets the file path + checksum, verifies, launches llama-server pointed at the new file
4. Controller next call: `LocalBackend.__init__` health-probes the new server, succeeds, ready
5. No code change. No restart of the Controller required (next call refreshes the connection naturally on health-probe failure).

Hardware upgrade flow:
1. User upgrades RAM / installs GPU
2. User reruns `recommend`; it now suggests a larger/faster model
3. User runs `install` + edits config + restart — same as runtime swap above

## llama-server startup script — fully registry-driven

`scripts/start_qb_local.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Resolve paths from environment (dev) or /etc/icebreaker/locations.env (Phase 6)
: "${ICEBREAKER_CONFIG:?missing $ICEBREAKER_CONFIG}"
: "${ICEBREAKER_PORT_QB:=8081}"

# Pull model_id + draft_model_id + grammar_path from the active config
read -r MODEL_ID DRAFT_ID GRAMMAR_PATH <<EOF
$(python3 -c "
import sys, pathlib
if sys.version_info >= (3,11): import tomllib
else: import tomli as tomllib
cfg = tomllib.loads(pathlib.Path('$ICEBREAKER_CONFIG').read_text())
qb = cfg['qb']['local']
print(qb['model_id'], qb.get('draft_model_id',''), qb['grammar_path'])
")
EOF

# Registry resolves model_id → on-disk file path, verifying sha256
MODEL_FILE=$(python3 -m controller.model_registry resolve --id "$MODEL_ID")
DRAFT_ARGS=""
if [ -n "$DRAFT_ID" ]; then
    DRAFT_FILE=$(python3 -m controller.model_registry resolve --id "$DRAFT_ID")
    DRAFT_ARGS="--draft-model $DRAFT_FILE --draft 8"
fi

exec llama-server \
    --model "$MODEL_FILE" \
    $DRAFT_ARGS \
    -ngl 99 \
    --port "$ICEBREAKER_PORT_QB" \
    --host 127.0.0.1 \
    --grammar-file "$GRAMMAR_PATH" \
    --no-warmup
```

Identical pattern for `start_pb.sh` (different `model_id`, different port). The script has zero hardcoded model names — change the config, restart, done.

## `LocalBackend` — unchanged from the previous revision

`LocalBackend` doesn't see `model_id` at all. It only knows there's a llama-server at `config.endpoint`. The model swap happens *outside* the Controller (in the ops script + registry). This is the cleanest separation:

```python
@register_backend("local")
class LlamaCppLocalBackend(BrainBackend):
    __slots__ = ("_endpoint", "_grammar", "_session")
    backend_name = "local"
    # ... same as previous plan revision
```

The only `BackendConfig` change: `model` field renames to `model_id` (so it's clear it's a registry lookup, not a filename), and the registry resolves it to a friendly display name for audit-row `model` field:

```python
@dataclass(frozen=True)
class BackendConfig:
    name: str
    model_id: str                         # ← NEW: catalogue key
    display_name: str                     # ← resolved at load(): registry.get(model_id).display_name
    max_tokens: int
    timeout_seconds: int
    endpoint: str | None = None
    transport: str = "http"
    grammar_path: Path | None = None
    draft_model_id: str | None = None
    api_key: SecretRef | None = None
```

The `BrainResponse.model` field becomes the `display_name` (e.g., `"Qwen 2.5 1.5B Instruct"`) so audit rows are human-readable.

## Files

| Path | New / Modified |
|---|---|
| `dual-brain/controller/catalogue.toml` | **New** — the model registry source of truth |
| `dual-brain/controller/model_registry.py` | **New** — parser, resolver, CLI, downloader |
| `dual-brain/controller/tests/test_model_registry.py` | **New** — catalogue parsing, lookup, filter-by-hardware, sha256 verify, download mock |
| `dual-brain/controller/backends/llama_local_backend.py` | **New** — LocalBackend (HTTP transport, grammar attached) |
| `dual-brain/controller/grammars/qb_intent.gbnf` | **New** — M2.8 grammar |
| `dual-brain/controller/config.py` | Modified — `model_id`, `draft_model_id`, `transport`, `display_name` (resolved at load) |
| `dual-brain/controller/schemas/controller_config.json` | Modified — schema accepts new fields; rejects raw filename in `model` field |
| `dual-brain/controller/controller.toml.example` | Modified — uses `model_id` |
| `dual-brain/scripts/locations.env` | **New** — `ICEBREAKER_CONFIG`, `ICEBREAKER_MODELS_DIR`, ports |
| `dual-brain/scripts/start_pb.sh` | **New** — PB llama-server launcher, registry-driven |
| `dual-brain/scripts/start_qb_local.sh` | **New** — QB llama-server launcher, registry-driven |
| `dual-brain/controller/tests/test_grammar_parity.py` | **New** — GBNF ⊆ jsonschema differential test + 10K fuzz |
| `dual-brain/controller/tests/test_llama_local_backend.py` | **New** — mocked llama-server |
| `dual-brain/docs/phase2/phase2_roadmap.md` + mirror | Modified — M2.5 model line + G9 budget caveat + plug-and-play paragraph |
| `dual-brain/docs/phase2/m25_production_migration.md` | **New** — Phase 6 migration map (registry paths, daemon UIDs, dm-verity model dir) |

**Not modified:** `backends/{base,auditor,sanitize,_api_common,anthropic_backend,gemini_backend}.py`, `backends/registry.py` (different "registry" — module-level dict for backends, distinct from model registry).

## Test plan

### `test_model_registry.py` (~14 tests)

1. `test_catalogue_loads_and_validates`
2. `test_get_by_id_returns_entry`
3. `test_get_unknown_id_raises`
4. `test_list_by_role_filters_correctly` (qb / pb / draft)
5. `test_filter_by_hardware_respects_min_ram` (8 GB system shows balanced models; 4 GB hides large ones)
6. `test_resolve_to_file_finds_in_first_search_dir`
7. `test_resolve_to_file_falls_through_to_second_dir`
8. `test_resolve_to_file_verifies_sha256` (mismatched file → `ChecksumError`)
9. `test_resolve_to_file_missing_everywhere_raises_ModelNotInstalledError`
10. `test_installed_ids_returns_only_present`
11. `test_recommend_hardware_probe_picks_balanced_on_16gb`
12. `test_recommend_picks_fast_on_4gb_no_gpu`
13. `test_download_helper_streams_with_progress` (mocked HF response)
14. `test_download_rejects_on_sha256_mismatch`

### `test_grammar_parity.py` (M2.8, ~10 tests)

Same as previous revision — 200 corpus + 100 malformed + 10K hypothesis fuzz. GBNF ⊆ jsonschema CI gate.

### `test_llama_local_backend.py` (~12 tests)

Same as previous revision — health probe at construction, transport="unix" raises `NotImplementedError`, grammar attached to request, cost=None, retry round-trip, sanitize on connection error, cross-backend shape match.

### Live integration smoke (GCP VM)

```bash
# 1. Hardware probe + recommend
python3 -m controller.model_registry recommend

# 2. Install recommended QB + draft
python3 -m controller.model_registry install --id qwen-2.5-1.5b-instruct-q4_k_m
python3 -m controller.model_registry install --id qwen-2.5-coder-0.5b-instruct-q4_k_m

# 3. Verify checksums
python3 -m controller.model_registry verify --id qwen-2.5-1.5b-instruct-q4_k_m
python3 -m controller.model_registry verify --id qwen-2.5-coder-0.5b-instruct-q4_k_m

# 4. Start servers (registry-driven)
ICEBREAKER_CONFIG=~/.config/icebreaker/controller.toml \
    bash scripts/start_pb.sh &
ICEBREAKER_CONFIG=~/.config/icebreaker/controller.toml \
    bash scripts/start_qb_local.sh &
sleep 30

# 5. Run 10-intent corpus through LocalBackend
PYTHONPATH=. python3 -m controller.bench.tier0_corpus --backend local --n 10

# 6. SWAP MODEL test (proves plug-and-play works):
#    Edit controller.toml: model_id = "qwen-2.5-0.5b-instruct-q4_k_m"
#    Restart llama-server-qb
#    Re-run corpus
#    Compare latencies (should be faster); compare success rates (might dip)
```

The swap test is the explicit proof that the user's "change as we upgrade" requirement works.

## Phase 6 — what changes when this ships in a distro

The migration document (`m25_production_migration.md`) maps every M2.5 path to its Phase 6 form:

| M2.5 (dev) | Phase 6 (distro) |
|---|---|
| `dual-brain/controller/catalogue.toml` | `/usr/share/icebreaker/models/catalogue.toml` (admin can extend via `/etc/icebreaker/catalogue.d/*.toml`) |
| `models/` (repo-relative search dir) | `/var/lib/icebreaker/models/` (system) + `~/.local/share/icebreaker/models/` (user) |
| `scripts/start_qb_local.sh` | `icebreaker-qbd.service` (`ExecStart=/usr/libexec/icebreaker/start-qbd`, runs as `_qb` user) |
| `~/.config/icebreaker/controller.toml` | Same — XDG layout already correct |
| `python3 -m controller.model_registry install --id X` | `icebreaker models install X` (admin) / GUI installer (user-facing) |
| HTTP `http://127.0.0.1:8081` | UNIX `unix:///run/icebreaker/qbd.sock` (transport="unix" flips; LocalBackend's `unix` branch lands in Phase 6) |

Each row is *config*, not code. That's the test of plug-and-play: every change above is a setting flip, not a refactor.

## Risks reviewer should focus on

1. **Catalogue maintenance.** Each catalogue entry has a `sha256` that must match the upstream file. If HuggingFace re-uploads, the sha changes and `verify` breaks. Mitigation: pin to immutable releases (commit-tagged or release-tagged files); document the catalogue-update process.

2. **License compliance per model.** Gemma has its own ToU; Phi-4 is MIT; Qwen is Apache. The catalogue carries the license field; the `install` subcommand prints it and requires `--accept-license` for non-permissive entries (Gemma). Important for shipping an OS.

3. **Multi-model coexistence on disk.** Three QB candidates × ~1 GB each + draft + PB = ~4 GB. Documented. The CLI's `prune` subcommand (deferred to Phase 5) lets users reclaim disk.

4. **The 0.5B draft as both PB and QB draft.** Same file mmap'd by two llama-server processes. OS page cache shares it; no double RAM cost. But two processes hold the file open — `lsof` will show both. Documented.

5. **Hardware probe portability.** Apple Silicon vs x86 vs ARM Linux servers all report RAM/GPU differently. `recommend` uses cross-platform Python (`psutil`) for RAM/disk; GPU detection uses `subprocess` to query `system_profiler` (mac) / `nvidia-smi` / `rocm-smi` (Linux) / `lspci` (fallback). All paths return "no GPU" rather than failing, so the probe always returns something usable.

6. **Catalogue file injection.** A compromised catalogue could point users at a malicious `.gguf`. Mitigation today: the catalogue file ships in the repo (tracked, code-reviewed). Phase 6 mitigation: catalogue file is part of the signed OS image; drop-ins in `/etc/icebreaker/catalogue.d/` require admin write access.

7. **No auto-download at runtime.** `LocalBackend.__init__` does NOT call `install`. If the model is missing, the backend fails fast with `ModelNotInstalledError` and a clear message saying how to install. Avoids surprise network traffic from a security-critical component.

8. **Swap requires llama-server restart.** Not seamless. Documented. Phase 6 systemd unit makes this a single `systemctl --user restart icebreaker-qb-local.service`.

## Out of scope (explicitly deferred)

- **Interactive setup wizard / GUI installer** — Phase 5 UX
- **`icebreaker models swap qb <id>` one-liner that edits config + restarts daemon atomically** — Phase 5/6
- **Hardware-change auto-detection** ("you upgraded your RAM, want to switch to a better model?") — Phase 5
- **Custom model registration** (user adds their own GGUF to the catalogue) — Phase 5
- **Model deletion / `prune`** — Phase 5
- **Per-user model overrides on shared systems** — Phase 6 (depends on system vs user model pools)
- **Cryptographic catalogue signing** — Phase 7
- **Two-phase COW commit + journal replay** — Phase 7
- **Multi-modal HITL daemon** — Phase 5/6
- **UNIX-socket transport for llama-server** — Phase 6 (LocalBackend has the branch as `NotImplementedError`)

## Task breakdown

1. Author `catalogue.toml` with the four QB candidates + PB + draft (sha256 fields placeholder for now; populated after first download verifies)
2. Implement `model_registry.py` (`ModelEntry` dataclass, parser, lookup, hardware filter, sha256 verify, download helper)
3. Write `test_model_registry.py` (14 tests, all offline with mocked filesystem + mocked HTTP)
4. Author `qb_intent.gbnf` (M2.8) by seeding from `json-schema-to-grammar.py` + hand-tightening
5. Write `test_grammar_parity.py` (10 tests including 10K fuzz)
6. Extend `BackendConfig` + `controller_config.json` schema with `model_id`, `draft_model_id`, `transport`, `display_name` (resolved)
7. Update `controller.toml.example` to use `model_id` everywhere
8. Author `scripts/locations.env`, `scripts/start_pb.sh`, `scripts/start_qb_local.sh` (all registry-driven, zero hardcoded model names)
9. Write `llama_local_backend.py` (`LocalBackend`)
10. Write `test_llama_local_backend.py` (12 tests using `requests-mock` or a tiny `http.server` fake)
11. Run M2.5+M2.8 unit suite + full controller regression — must stay green
12. Author `dual-brain/docs/phase2/m25_production_migration.md` (the Phase 6 migration map)
13. Update `phase2_roadmap.md` (both mirrors) — M2.5 line, §6 deps, G9 caveat
14. Update `models/checksums.sha256` with new entries (filled in from actual download)
15. **Live VM smoke**, including the *model swap test* (the user's explicit acceptance criterion)
16. Commit + push to `feature/phase2-controller`

## Acceptance criteria

| Requirement | How verified |
|---|---|
| User can select starting model | `recommend` subcommand → suggestion → user edits config → start servers → working session |
| User can change model | Edit `model_id` in config → restart llama-server → next session uses new model (no code change) |
| User can upgrade as hardware changes | Run `recommend` again → it suggests larger/faster model → user installs + switches |
| Models verified before use | `resolve_to_file` checks sha256; `start_*.sh` calls registry which calls verify |
| Catalogue is the only place that maps id → file | No filename appears in code or ops scripts |
| First-run works without any config edit | Default catalogue + auto-detected hardware → reasonable defaults; user can confirm |
| Phase 6 migration is config-only | Documented in `m25_production_migration.md`; every code path supports both transport modes already |
