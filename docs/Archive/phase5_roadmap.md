# Phase 5 Roadmap — Production UX, Graduated Determinism & Hardening

## §1 — Context & Status

> **Status: ⬜ PLANNING — not started. Predecessors Phase 0/1/3/4 complete on `main`;
> Phase 2 (Dual-Brain Controller) built, gated (G1–G11), and merged. Phase 5 turns that
> Controller into something installable in a Linux distro: hardened against the prompt-
> injection / spoofing surface, fast and configurable at the keyboard, and plug-and-play so
> the system keeps evolving.**

Phase 5 in the whitepaper (§8, §10 Phase 5) is **User Experience & Graduated Determinism** —
"a user-facing interface that is fast for routine tasks and safe for dangerous ones." The
Tier 0–3 classifier, the HITL gate, the provenance audit log, the pluggable-backend
abstraction, and the `prompt_toolkit` REPL already exist (Phase 2). Phase 5 **completes the
graduated-determinism loop** (Tier 2 LLM review), **hardens the human gate** against the
spoofing/bypass surface, makes the whole UX **user-configurable and plug-and-play**, and
prepares the Controller for **daemon-mode deployment**.

### Build philosophy (non-negotiable for this phase)

1. **Pareto first.** Work is split into **P0 (the ~20% that delivers ~80% of value)**, then P1,
   then P2. P0 alone makes the Controller safe-to-demo and pleasant to use. Ship the vital few
   well before the long tail. *Done > complete.*
2. **Plug-and-play everywhere.** Every load-bearing layer is swappable via config with no code
   edits — extending the repo's existing registry culture (`model_registry.py` +
   `catalogue.toml`, the `BrainBackend` registry, the `HitlPresenter` ABC). New extension
   points (risk-classifier strategy, Tier-2 reviewer strategy, keymap, audit sink) follow the
   same pattern. Every new behavior sits behind a feature flag so it can roll forward/back
   independently (the `[tier2] enabled` / `fs-test-roots` cargo-feature pattern).
3. **Configurable UX.** All keybindings come from config; the numeric/mnemonic set is the
   default, not a hard-coding.

### What already exists that Phase 5 builds on (grounded in code)

| Capability | Where | Phase 5 relevance |
|---|---|---|
| Tier 0–3 rule-based classifier | `dual-brain/controller/risk_classifier.py` | Tier 2 currently auto-executes+notifies; P5 adds the **escalate-only LLM review**; classifier becomes a swappable strategy |
| HITL gate + `HitlPresenter` ABC ("swap for a GUI presenter in Phase 5") | `dual-brain/controller/hitl.py` | Hardened (SF-1/2/3), made keymap-driven; `[E]`/`[M]` stubs (lines 125-127) implemented; `[T]rust` added |
| 14-step orchestration | `dual-brain/controller/main.py` (`Controller.run_turn`) | Inserts Tier-2 review (after step 3), the `[M]odify` reclassify loop, trust-store consult; `_build_presenter()` (line 381) is the GUI seam |
| `intent_store.revise()` | `dual-brain/controller/intent_store.py` | Built but **not wired** — the `[M]odify` path activates it |
| Provenance audit log (JSONL, O_APPEND, fsync, redaction, 0o640) | `dual-brain/controller/audit.py` | Made **tamper-evident** (hash chain) + redaction strengthened + pluggable sink |
| Config loader (secret-key rejection, `SecretRef`, schema-validated TOML) | `dual-brain/controller/config.py` | Gains `[keymap]`, `[risk]`, `[tier2]`, `[audit]`, `[cost]` sections |
| Pluggable model registry / QB backends | `model_registry.py`, `catalogue.toml`, `backends/` | OpenAI + OAuth backends drop in via the existing interface |
| `prompt_toolkit` REPL w/ status line (cost shown) | `dual-brain/controller/repl.py` | Streaming, `/undo`, `/trust`, audit viewer, shared keymap |

---

## §2 — Security Findings in the Current Implementation

The single most important Phase 5 input: a code-verified audit of the human-gate and audit
surfaces. Each finding maps to a milestone and an exit gate. **`hitl.py`,
`risk_classifier.py`, and `audit.py` are security-critical files — every fix needs WF-6 two-
reviewer sign-off.**

| # | Sev | Finding (verified in code) | Fix | Wave |
|---|---|---|---|---|
| **SF-1** | High | **HITL prompt-spoofing via terminal escapes.** `hitl.py:_render` interpolates `action`, `target`, `reason`, `blocked_pattern`, `backend` **raw**; only `cow_summary` is ANSI-stripped (`HitlPrompt.__init__`). `target`/`reason` originate from QB output (influenceable by document/user content). A crafted `target` with ANSI/`\r`/`\n` can redraw the prompt — hide the real path, paint a fake `READ-ONLY` risk label, or forge the `✓ Approved` line — defeating informed consent. | M5.1 | **P0** |
| **SF-2** | High | **3-second lockout (INV-6) bypass via pre-buffered stdin.** After `lockout()`, `read_decision` enters the `select`/`readline` loop with **no stdin flush**. Bytes typed/pasted/injected during the 3 s window stay buffered; the first `readline()` consumes `"A\n"` → instant auto-approve. The lockout exists precisely to stop reflexive/automated approval. | M5.1 | **P0** |
| **SF-3** | Med | **Line-based input, not single keypress.** `read_decision` uses `readline()` (needs Enter) — the UX gap and the enabler of SF-2 (a pre-injected newline confirms). | M5.1 | **P0** |
| **SF-4** | High | **Approval fatigue structurally unmitigated.** No bounded "always allow" path, so repeated identical Tier-2 prompts train rubber-stamping — the exact failure (whitepaper F4 / KPI 5) the architecture must avoid. | M5.1 (`[T]rust`) | **P0** |
| **SF-5** | High | **Audit log append-only but not tamper-evident.** `audit.py` gives O_APPEND + per-line `fsync` + 0o640, but no integrity chain — a same-uid compromise can rewrite/delete past lines undetectably, and the log lives in the user's own `~/.local/state` trust domain. | M5.3 | **P0** |
| **SF-6** | Med | **Secret redaction heuristic + shallow.** `_redact_params` matches key-name substrings / known value prefixes, one level deep; `target`/`reason` are never redacted; unprefixed free-form secrets reach disk. | M5.3 | **P0** |
| **SF-7** | Med | **API keys live in the process environment.** Config refuses to store keys (good) but they remain in `os.environ` — readable via `/proc/self/environ` by same-uid processes and **inherited by the spawned `mcpd` subprocess**, the tool-execution process least entitled to them. | M5.P1-sec | P1 |
| **SF-8** | Low | **REPL history persists all inputs in plaintext** (`FileHistory`, `repl_history`); NL commands may carry secrets/paths. | M5.P1-sec | P1 |
| **SF-9** | Med | **No resource governance.** Unbounded user-input size → QB; no turn-rate limit; cost ceiling still deferred. An injection or runaway loop on an API backend is a cost/DoS bomb. | M5.P1-sec | P1 |
| **SF-10** | Med | **TOCTOU + QB free-text in the security display.** Classifier resolves `realpath` (P2-F12) but mcpd executes later (symlink-swap window); HITL shows QB-authored `reason` next to decision-critical facts. Decision facts must come only from structured sources (intent schema + mcpd COW preview); the resolved path must be what mcpd acts on. | M5.P1-sec | P1 |
| **SF-11** | Med | **Daemon-mode surface (future).** When the Controller becomes long-running, its IPC must be a Unix socket with `SO_PEERCRED` uid check, 0600, **no TCP (INV-3 parity)**, plus a hardened systemd unit; per-session isolation must survive multiplexing. | M5.P2-daemon | P2 |

---

## §3 — Guiding Principles & Extension Points

### Plug-and-play extension points

| Layer | Default → swap mechanism | Status |
|---|---|---|
| Models (QB/PB/draft) | `catalogue.toml` + `model_registry` resolve/verify; edit `model_id`, restart `llama-server` | **exists** (M2.5) |
| QB backends | `BrainBackend` registry; `[qb] backend = "local"|"anthropic"|"gemini"|...` | **exists** |
| **Risk-classifier algorithm** | new `classifier` registry; `[risk] strategy = "rules"` (default) → `"ml"`/`"policy"`, no `main.py` edits | Phase 5 (M5.2) |
| **Tier-2 reviewer** | strategy plugin; `[tier2] strategy = "llm"` (default) → `"rule"`/`"external"` | Phase 5 (M5.2) |
| HITL presenter | `HitlPresenter` ABC; `[hitl] presenter = "terminal"` → `"gtk"`/`"web"` | seam exists (M5.P2-access) |
| **Keymap** | `[keymap]` config (below); numeric/mnemonic defaults, fully overridable | Phase 5 (M5.1) |
| **Audit sink** | `[audit] sink = "file"` (default) → `"journald"`/`"system-appender"` | Phase 5 (M5.3 / daemon) |

### Configurable keymap (default → user-overridable)

```toml
[keymap]                       # raw single-keypress, no Enter; Esc always = deny (reserved)
approve = ["1", "a", "y"]
deny    = ["2", "d", "n"]
modify  = ["3", "m"]
explain = ["4", "e"]
trust   = ["5", "t"]           # bounded: Tier ≤ 2 only, never Tier 3
help    = ["?"]
```

A `Keymap` loader (new `controller/keymap.py`) validates that bindings are single printable
chars, rejects duplicate keys across actions, and keeps `Esc`/deny reserved. `?` always prints
the **active** (possibly customized) legend, so customization never costs discoverability. The
same `Keymap` instance drives the HITL gate, the REPL, and the audit viewer.

---

## §4 — P0: the vital few (ship the core promise — safe + fast + graduated)

Three flag-gated milestones. This wave closes every High-severity finding and delivers the
Phase-5 headline (graduated determinism with a snappy, configurable, spoof-proof gate).

### M5.1 — Hardened, configurable, single-keypress HITL gate  *(security-critical; SF-1/2/3/4)*

**Files:** `dual-brain/controller/hitl.py` (mod), new `dual-brain/controller/keymap.py`, new
`dual-brain/controller/trust_store.py`, `dual-brain/controller/config.py` (+ `[keymap]`,
extend `HitlConfig`), `dual-brain/controller/schemas/controller_config.json` (mod),
`dual-brain/controller/main.py` (mod — trust consult + `[M]` reclassify loop), new tests
`tests/test_keymap.py`, `tests/test_trust_store.py`, `tests/test_hitl_sanitize.py`,
`tests/test_hitl_rawinput.py`, `tests/test_hitl_actions.py`, new corpora
`tests/corpus/hitl_spoof.json`, `tests/corpus/lockout_bypass.json`.

#### M5.1a — `keymap.py` (new module)

```python
class Action(str, Enum):
    APPROVE = "approve"; DENY = "deny"; MODIFY = "modify"
    EXPLAIN = "explain"; TRUST = "trust"; HELP = "help"

@dataclass(frozen=True)
class Keymap:
    bindings: Mapping[str, Action]          # single printable char -> Action
    def resolve(self, key: str) -> Optional[Action]: ...
    def keys_for(self, action: Action) -> tuple[str, ...]: ...
    def legend(self) -> str:                # rendered by '?' and the HITL footer
        ...

DEFAULTS: dict[Action, tuple[str, ...]] = {
    Action.APPROVE: ("1", "a", "y"), Action.DENY: ("2", "d", "n"),
    Action.MODIFY:  ("3", "m"),      Action.EXPLAIN: ("4", "e"),
    Action.TRUST:   ("5", "t"),      Action.HELP: ("?",),
}

def load_keymap(cfg: "KeymapConfig") -> Keymap:
    # 1. start from DEFAULTS; 2. overlay user bindings from [keymap];
    # 3. validate: each key is exactly one printable, non-control char;
    #    no key bound to two Actions (raise BrainConfigError on collision);
    #    DENY must retain at least one binding; ESC (\x1b) is reserved -> always DENY
    #    and may not be remapped. Return frozen Keymap.
```

`Esc` is hard-reserved to DENY (cannot be remapped) so a hostile/incorrect keymap can never
produce an un-exitable prompt (P5-F11).

#### M5.1b — `hitl.py` hardening

- **SF-1 — sanitize all rendered fields.** Add module function:
  ```python
  _ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
  _CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0/C1 minus \t, plus DEL
  def _sanitize_display(s: str, *, max_len: int = 256) -> str:
      s = _ANSI.sub("", s); s = _CTRL.sub("", s)
      s = s.replace("\r", " ").replace("\n", " ")
      return s[:max_len] + ("…" if len(s) > max_len else "")
  ```
  Apply in `HitlDisplayData` construction to **every** string field (`action`, `target`,
  `reason`, `blocked_pattern`, `backend`) — not just `cow_summary`. Add an optional
  `_confusable_warn(target)` that appends `⚠ contains non-ASCII/look-alike chars` when the
  target mixes scripts (cheap `str.isascii()` + Unicode script check), so homoglyph paths are
  surfaced rather than silently rendered.
- **SF-2/3 — flush + single raw keypress.** Replace the line-reader. New presenter method:
  ```python
  def read_decision(self, keymap, timeout_seconds) -> Decision:
      if not sys.stdin.isatty(): return Decision.NON_TTY
      termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)   # discard pre-buffered bytes
      with _cbreak(sys.stdin):                                # tty.setcbreak save/restore
          deadline = time.monotonic() + timeout_seconds
          while True:
              remaining = deadline - time.monotonic()
              if remaining <= 0: return Decision.TIMEOUT
              r,_,_ = select.select([sys.stdin],[],[], min(remaining,1.0))
              if not r: continue
              ch = os.read(sys.stdin.fileno(), 1).decode("utf-8","ignore")
              if ch == "\x1b": return Decision.DENIED          # ESC reserved
              action = keymap.resolve(ch)
              if action: return _action_to_decision(action)    # may be EXPLAIN/MODIFY/TRUST
              # unknown key: re-render the footer legend, keep waiting
  ```
  The `tcflush` happens **after** `lockout()` returns, closing SF-2; the single `os.read(…,1)`
  closes SF-3. INV-6 lockout enforcement in `HitlPrompt.ask()` (the `time.monotonic()` floor)
  is unchanged.
- **New decisions.** Extend `Decision` with `MODIFY`, `EXPLAIN`, `TRUST`. `EXPLAIN` loops back
  into the same prompt after showing the explanation; `MODIFY`/`TRUST`/`APPROVE`/`DENY` are
  terminal for `ask()`.
- **Footer** rendered from `keymap.legend()` so it always reflects the active bindings.

#### M5.1c — `trust_store.py` (new module, SF-4)

```python
@dataclass(frozen=True)
class TrustGrant:
    action: str; target_prefix: str; max_tier: int   # always <= 2
    session_id: str; granted_at: float; expires_at: float

class TrustStore:                       # in-memory, per-session; persistence OFF by default
    def grant(self, intent, tier: int, session_id, ttl_s: int) -> TrustGrant:
        if tier >= Tier.HIGH: raise ValueError("Tier 3 is never trustable")
        ...                              # key = (action, realpath-prefix of target)
    def is_trusted(self, intent, tier: int, session_id) -> bool:
        if tier >= Tier.HIGH: return False   # hard floor — defense in depth
        ...                              # match + not expired + same session
    def revoke(self, index_or_key) -> None: ...
    def list(self, session_id) -> list[TrustGrant]: ...
```

Hard rule encoded in **two** places (`grant` raises, `is_trusted` returns False) so a Tier-3
op can never be auto-approved even if a grant were forged. Grant and each trusted-skip are
audited with a distinct outcome (`TRUST_GRANTED` / `TRUST_APPLIED`).

#### M5.1d — `main.py` wiring

- After **Step 3 (classify)**, before Step 4: if `cls.tier == Tier.MEDIUM` and
  `trust_store.is_trusted(intent, tier, session_id)` → skip the notification prompt, audit
  `TRUST_APPLIED`, proceed.
- **Step 4 (HITL)** now receives the `Keymap`; on `Decision.TRUST` → `trust_store.grant(...)`
  then proceed; on `Decision.MODIFY` → call `intent_store.revise(intent_id, edits)` →
  `intent_schema.validate` → `classify` again → re-enter the gate (bounded loop, max 3
  modify-cycles, then deny); on `Decision.EXPLAIN` handled inside `ask()`.
- New helper `_qb_explain(intent, cls)` → one `BrainBackend.complete()` with a "explain the
  consequence of this action in 2-3 plain sentences" system prompt (schema
  `{explanation: str}`); reuses the `_qb_summarise` plumbing.

#### M5.1 config additions

```toml
[hitl]
lockout_seconds = 3        # INV-6 (unchanged default)
timeout_seconds = 30
presenter = "terminal"
trust_ttl_seconds = 1800   # session-trust lifetime; 0 disables [T]rust entirely

[keymap]                   # all optional; defaults shown in §3
approve = ["1","a","y"]
deny    = ["2","d","n"]
modify  = ["3","m"]
explain = ["4","e"]
trust   = ["5","t"]
```

#### M5.1 test plan

| Test | Asserts |
|---|---|
| `test_hitl_sanitize.py` | every field in a 30-payload spoof corpus (ANSI cursor moves, `\r` overwrite, fake `✓ Approved`, newline injection) renders with zero escape bytes; `_render` output passes a `no-control-char` assertion |
| `test_hitl_rawinput.py` | bytes written to a PTY *during* the lockout are flushed and ignored; only a keypress *after* the flush decides; ESC → DENY; timeout → TIMEOUT; non-TTY → NON_TTY |
| `test_keymap.py` | defaults load; user overlay applies; duplicate-key collision raises; multi-char/control-char binding rejected; ESC cannot be remapped; `legend()` lists active keys |
| `test_trust_store.py` | grant for Tier ≤ 2 works; `grant`/`is_trusted` both refuse Tier 3; expiry honored; wrong-session miss; revoke removes |
| `test_hitl_actions.py` | `[E]` shows explanation and re-prompts; `[M]` → revise→reclassify→re-gate; modify-loop cap enforced; all outcomes audited |

**Acceptance / G5.1–G5.4:** spoof corpus 0/N escapes; pre-buffered stdin discarded; keymap
overrides load and `?`/footer show them; `[E]/[M]/[T]` work; `[T]` provably cannot cover
Tier 3; trust grant + every trusted-skip + every decision is in the audit log.

### M5.2 — Tier 2 LLM review pass, escalate-only  *(the Graduated-Determinism headline)*

**Files:** new `dual-brain/controller/tier2_review.py`, `dual-brain/controller/risk_classifier.py`
(mod — pluggable strategy registry), `dual-brain/controller/main.py` (mod — insert review
between Step 3 and Step 5), `config.py` (+ `[risk]`, `[tier2]`),
`dual-brain/controller/prompts/tier2_review.txt` (new), new tests `tests/test_tier2_review.py`,
`tests/test_classifier_registry.py`, corpus `tests/corpus/tier2_escalation.json`.

#### M5.2a — pluggable classifier registry (`risk_classifier.py`)

Keep the existing `classify()` as the default `"rules"` strategy; add a thin registry so the
algorithm is swappable (principle #2) without touching `main.py`:

```python
Classifier = Callable[[dict], ClassificationResult]
_REGISTRY: dict[str, Classifier] = {"rules": classify}
def register_classifier(name: str, fn: Classifier) -> None: ...
def get_classifier(name: str) -> Classifier:           # "rules" (default) | "ml" | "policy"
    return _REGISTRY[name]
```

#### M5.2b — `tier2_review.py` (new module)

```python
TIER2_SCHEMA = {                       # forced JSON shape for every reviewer backend
  "type":"object","additionalProperties":False,
  "required":["escalate","reason"],
  "properties":{"escalate":{"type":"boolean"},
                "reason":{"type":"string","maxLength":300}}}

@dataclass(frozen=True)
class Tier2Decision: escalate: bool; reason: str

class Tier2Reviewer(ABC):
    @abstractmethod
    def review(self, intent: dict, cls: ClassificationResult) -> Tier2Decision: ...

class LlmTier2Reviewer(Tier2Reviewer):           # default strategy="llm"
    def __init__(self, qb_backend, prompt_loader): ...
    def review(self, intent, cls):
        # structured-only: send action/target/params/reason fields, NEVER raw user text
        resp = self._qb.complete(system=self._prompt, user=json.dumps({...}),
                                 schema=TIER2_SCHEMA, max_retries=2)
        return Tier2Decision(bool(resp.content_json["escalate"]),
                             resp.content_json["reason"])

class RuleTier2Reviewer(Tier2Reviewer): ...      # strategy="rule" — deterministic offline
_REVIEWERS = {"llm": LlmTier2Reviewer, "rule": RuleTier2Reviewer}
def get_reviewer(strategy, **deps) -> Tier2Reviewer: ...
```

The reviewer receives **only structured intent fields** (action/target/params/reason), never
the raw user message — INV-1/INV-2 hold; the reviewer is a QB-side check, not a PB input.

#### M5.2c — `main.py` wiring + the escalate-only invariant

Between Step 3 (classify) and Step 5 (store):

```python
if cls.tier == Tier.MEDIUM and self._cfg.tier2.enabled:
    decision = self._tier2.review(intent, cls)
    if decision.escalate:
        cls = replace(cls, tier=Tier.HIGH,            # ONLY upward
                      reason=f"Tier-2 review escalated: {decision.reason}")
    # else: tier stays MEDIUM (existing notify path)
assert cls.tier >= original_tier   # hard guard: review can never lower a rule-based tier
```

The `assert cls.tier >= original_tier` makes the escalate-only rule a runtime invariant, not
just a convention. A reviewer that returns malformed/garbage output (caught by `TIER2_SCHEMA`
+ retry) fails safe → no escalation change, op stays at its rule-based tier.

#### M5.2 config

```toml
[risk]
strategy = "rules"        # pluggable classifier algorithm; default rule-based
[tier2]
enabled = true
strategy = "llm"          # "llm" (default) | "rule" | "external"
max_retries = 2
```

#### M5.2 test plan

| Test | Asserts |
|---|---|
| `test_tier2_review.py` | escalating decision routes a Tier-2 intent into the HITL gate; non-escalating proceeds to notify; malformed reviewer output fails safe (no change); reviewer sees no raw user text |
| `test_classifier_registry.py` | `"rules"` default resolves; a registered mock strategy swaps in; unknown strategy raises |
| escalate-only parity | the existing Tier corpus reclassified with `[tier2] enabled` yields **identical** Tier 0/1/3 results; only some Tier-2 entries move to Tier 3, never the reverse |

**Acceptance / G5.5:** flagged medium-risk ops escalate to HITL; Tier 0/1/3 rule-based output
unchanged; `assert` guard holds under fuzzed reviewer output; reviewer swappable via config.

### M5.3 — Tamper-evident audit + stronger redaction  *(security-critical; SF-5/6)*

**Files:** `dual-brain/controller/audit.py` (mod), new `dual-brain/controller/__main__`-style
`python -m controller.audit --verify` entry, `config.py` (+ `[audit]`), new tests
`tests/test_audit_chain.py`, `tests/test_audit_redaction.py`.

#### M5.3a — hash chain (SF-5)

Each line gains two fields appended to the canonical entry (kept last so existing readers
ignore them gracefully):

```
seq        int   monotonic per-log counter, starts 0
prev_hash  str   hex SHA-256 of the *previous* line's canonical bytes ("GENESIS" for seq 0)
```

`AuditLog` changes:
- on `_open()`, read the tail line (if any) to recover `(_last_seq, _last_hash)`; on a fresh
  file start at `seq=0, prev_hash="GENESIS"`.
- in `write()`, under the existing `self._lock`: set `seq`/`prev_hash`, serialize canonically
  (`json.dumps(..., sort_keys=True, separators=(",",":"))`), `os.write` + `os.fsync` (INV-8
  unchanged), then update `_last_hash = sha256(line_bytes)` and `_last_seq += 1`.
- new classmethod:
  ```python
  @staticmethod
  def verify_chain(path) -> tuple[bool, Optional[int]]:
      # recompute prev_hash for every line; return (ok, first_bad_seq_or_None).
      # detects edits (hash mismatch), deletions/reordering (seq gap), truncation.
  ```
- CLI: `python -m controller.audit --verify [path]` prints OK or the first bad seq and exits
  non-zero (wired into `ci.sh`).

Note in §Cross-Cutting: the chain detects tampering but a same-uid attacker who recomputes the
whole chain could still rewrite history — full non-repudiation needs the **root-owned
system-appender** sink (below / daemon mode), where the unprivileged session can append but
not rewrite. The hash chain is the in-scope P0 floor; the system sink is the P2 ceiling.

#### M5.3b — redaction hardening (SF-6)

- Extend `_redact_params` with an entropy gate:
  ```python
  def _high_entropy(s: str, *, min_len=20, bits=3.5) -> bool: ...  # Shannon bits/char
  ```
  redact any string value that is long + high-entropy even when key/prefix look innocuous.
- Per-tool field allowlist — only known-safe fields are logged verbatim for sensitive tools:
  ```python
  _TOOL_FIELD_ALLOWLIST = {            # tool -> fields safe to log raw; others -> <REDACTED>
      "fs.write": {"path"},            # never log 'content'
      "package.install": {"name"},
      ...
  }
  ```
- For credential-class tools, redact `target` too (not only `params`).

#### M5.3c — pluggable sink

```python
class AuditSink(ABC):
    @abstractmethod
    def write_line(self, data: bytes) -> None: ...
class FileSink(AuditSink): ...           # current behavior — O_APPEND|fsync|0o640 (default)
class JournaldSink(AuditSink): ...       # forwards to systemd-journald (daemon)
class SystemAppenderSink(AuditSink): ... # root-owned setgid appender (P2, non-repudiation)
```
Selected by `[audit] sink = "file"` (default). `FileSink` preserves every current INV-8
property; the chain logic lives above the sink so all sinks inherit tamper-evidence.

#### M5.3 config

```toml
[audit]
sink = "file"                  # "file" (default) | "journald" | "system-appender"
path = "~/.local/state/icebreaker/controller-audit.log"
redact_entropy_bits = 3.5
```

#### M5.3 test plan

| Test | Asserts |
|---|---|
| `test_audit_chain.py` | `verify_chain` OK on an untouched log; flags the exact `seq` after an edited line, a deleted line (seq gap), reordering, and truncation; genesis handled; tail-recovery across reopen continues the chain |
| `test_audit_redaction.py` | high-entropy unprefixed secret redacted; `fs.write` `content` never logged; credential-tool `target` redacted; non-secret fields preserved; existing M2.3 redaction corpus still passes |

**Acceptance / G5.6:** any edit/deletion of a prior line is detected; strengthened-redaction
corpus shows zero secrets on disk; INV-8 (O_APPEND, fsync, 0o640, not model-writable) intact.

### P0 exit gates (Phase-5 wave-1 definition-of-done)

| Gate | Assertion |
|---|---|
| G5.1 | HITL renders no attacker-controlled escape/control sequences (SF-1 spoof corpus 0/N) |
| G5.2 | Lockout un-bypassable: input buffered during the 3 s window is discarded; single keypress required after flush (SF-2/3, INV-6) |
| G5.3 | Keymap is config-driven and user-overridable; `?` prints the active legend; invalid/duplicate bindings are rejected at load |
| G5.4 | `[E]xplain`/`[M]odify`/`[T]rust` work; `[M]` routes through `intent_store.revise()`+reclassify; `[T]` cannot grant Tier 3; all audited (SF-4) |
| G5.5 | Tier-2 LLM review escalates flagged ops and **never downgrades** a rule-based tier |
| G5.6 | Audit hash-chain verifies; tampering with any prior line is detected; redaction corpus clean; INV-8 holds (SF-5/6) |

---

## §5 — P1: second wave (high-value, sequenced after P0)

- **M5.P1-sec — Credential & resource hygiene (SF-7/8/9/10).** Scrub the `mcpd` subprocess env
  of API keys + move key material to systemd `LoadCredential=` / a keyring; secure (and
  optionally ephemeral) REPL history; input-size cap + turn-rate limit + **per-session cost
  ceiling** (accumulated in `SessionState`, surfaced in `/status` and the REPL status line);
  pass mcpd the realpath-resolved target and ensure HITL decision facts derive only from
  structured sources.
- **M5.P1-undo — `/undo` (and `u`)** over mcpd's existing COW rollback — a large safety/UX win;
  the reversal is itself audited.
- **M5.P1-stream — token streaming + multi-step progress + cancel**, extending the existing
  spinner; Ctrl+C already cancels a turn.
- **M5.P1-viewer — keyboard-navigable audit viewer** (`controller/audit_viewer.py` +
  `python -m controller.audit_viewer`): filter by `session_id`/`backend`/`tier`/`outcome`/
  action/time; `j/k` navigation, `/` search via the shared keymap; reads across rotated
  segments; never displays redacted secrets.

---

## §6 — P2: third wave (breadth / deployment)

- **M5.P2-backends — OpenAI backend + OAuth-subscription scaffolding** (`backends/openai_backend.py`,
  native JSON mode, `transform_schema_for_provider` reuse; OAuth token-refresh + browser
  callback behind the existing `BrainBackend` interface) and **QB-verifier majority voting
  (k=3)** in `Controller._qb_verify` behind `[verify] k`.
- **M5.P2-daemon — daemon mode (SF-11).** Long-running Controller as a hardened systemd *user*
  service (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, seccomp);
  `AF_UNIX` socket 0600 + `SO_PEERCRED` uid check; **no TCP listener (INV-3 parity,
  CI-asserted as for mcpd)**; log rotation (from M5.3); multiplexed sessions that preserve
  per-session INV-1/INV-2 isolation and per-session audit. (Full Unix-socket *brain* transport
  — the `transport="unix"` seam reserved in `llama_local_backend.py` — remains Phase 6.)
- **M5.P2-access — accessibility & GUI/web presenter.** No-color / no-Unicode / screen-reader
  plain mode (seeded by `TerminalPresenter`'s `isatty` checks); a GUI/web `HitlPresenter`
  implementation via the existing ABC + `HitlConfig.presenter` seam.

---

## §7 — Release-readiness (closes the phase)

- **M5.V1 — Red-team the new surfaces:** HITL-spoofing corpus, lockout-bypass corpus,
  trust-store abuse (attempt to cover Tier 3 / escalate scope), cost-bomb / rate-abuse. Extends
  the Phase-2 adversarial harness; assertion remains *0 unintended dispatch on any backend*
  (G6 preserved).
- **M5.V2 — Usability simulation → KPI 5:** simulate ~1 week of usage; derive Tier-3 frequency
  and approval latency **from the existing audit log** (no new logging — `audit.py` already
  records `tier`, `outcome`, `duration_ms`). Targets: **Tier 3 < 5/day, 0 rubber-stamped, NL
  task completion**.
- **M5.V3 — Exit gates + closeout:** extend `dual-brain/controller/ci.sh` with the P0 (and,
  when reached, P1/P2) gates; write §Closeout with per-gate evidence, mirroring Phase 2.

---

## §8 — Files Added or Modified (across all waves)

| Path | Action | Wave |
|---|---|---|
| `dual-brain/controller/hitl.py` | Modify — sanitize all fields, flush+raw keypress, keymap-driven, `[E]/[M]/[T]` | P0 (M5.1) |
| `dual-brain/controller/keymap.py` | New — config-driven keymap loader/validator | P0 (M5.1) |
| `dual-brain/controller/trust_store.py` | New — bounded, audited, revocable session trust | P0 (M5.1) |
| `dual-brain/controller/tier2_review.py` | New — escalate-only Tier-2 LLM review (pluggable) | P0 (M5.2) |
| `dual-brain/controller/risk_classifier.py` | Modify — pluggable classifier/tier2 strategy registry | P0 (M5.2) |
| `dual-brain/controller/audit.py` | Modify — hash-chain + `verify_chain()` + redaction++ + pluggable sink | P0 (M5.3) |
| `dual-brain/controller/main.py` | Modify — Tier-2 insert, trust consult, `[M]` reclassify loop | P0 |
| `dual-brain/controller/config.py` | Modify — `[keymap]`,`[risk]`,`[tier2]`,`[audit]`,`[cost]` | P0/P1 |
| `dual-brain/controller/audit_viewer.py` | New — keyboard-navigable viewer | P1 |
| `dual-brain/controller/backends/openai_backend.py` | New — OpenAI + OAuth scaffolding | P2 |
| `dual-brain/scripts/icebreaker-controller.service` (+ socket) | New — hardened systemd unit | P2 |
| `dual-brain/controller/ci.sh` | Modify — Phase 5 gates | all |
| `dual-brain/controller/tests/*` | New (`test_*`) + corpora | all |
| `~/.config/icebreaker/controller.toml.example` | Modify — new sections incl. `[keymap]` | P0/P1 |
| `docs/phase5_roadmap.md` | This file | planning |
| `CLAUDE.md` | Phase Status reconcile (Phase 2 → Complete; Phase 5 → this doc) | planning |
| `shell/pb_*`, `src/mcpd/*` | **Do not modify** in Phase 5 | — |

---

## §9 — Failure Modes Register (seeded from the security findings)

| # | Failure | Mitigation |
|---|---|---|
| P5-F1 | HITL prompt spoofed via terminal escapes (SF-1) | `_sanitize_display()` on every rendered field; spoof corpus in CI (G5.1) |
| P5-F2 | 3 s lockout bypassed by pre-buffered input (SF-2/3) | `tcflush` + single raw keypress after lockout; timing + buffered-input tests (G5.2) |
| P5-F3 | Bounded trust escalated to cover Tier 3 / widened scope (SF-4) | Trust store hard-caps at Tier ≤ 2, keyed by `(action, target-prefix)`, session-scoped, audited, revocable; red-team in M5.V1 |
| P5-F4 | Tier-2 reviewer downgrades a rule-based tier | Escalate-only invariant; classifier strategy cannot lower the rule floor; parity test (G5.5) |
| P5-F5 | Audit history rewritten/deleted undetected (SF-5) | SHA-256 `prev_hash` chain + `verify_chain()`; INV-8 perms (G5.6) |
| P5-F6 | Free-form / `target` secrets leak to disk (SF-6) | Entropy + per-tool field allowlists; never log raw `target` for credential-class tools |
| P5-F7 | API key leaks into mcpd subprocess / history / logs (SF-7/8) | Scrub mcpd env; systemd `LoadCredential`/keyring; secure/ephemeral history (P1) |
| P5-F8 | Cost / DoS bomb via injection or runaway loop (SF-9) | Input-size cap + turn-rate limit + per-session cost ceiling (P1) |
| P5-F9 | Symlink swap between classify and execute (SF-10) | Pass resolved realpath to mcpd; structured-only HITL facts; mcpd Landlock/COW backstop |
| P5-F10 | Daemon IPC reachable by another uid / over TCP (SF-11) | `AF_UNIX` 0600 + `SO_PEERCRED`; no TCP (INV-3, CI-asserted); systemd hardening (P2) |
| P5-F11 | Keymap override creates an un-exitable / duplicate binding | Loader rejects duplicates, reserves `Esc`/deny, requires single printable chars (G5.3) |

---

## §10 — Deferred to Phase 6 / 7

| Item | Phase | Why |
|---|---|---|
| Unix-socket **brain** transport (`transport="unix"`) | 6 | Tied to the system-wide install layout / socket perms |
| ISO embedding of Controller + systemd units + models | 6 | Distribution engineering |
| End-to-end pen-test of the full installed stack | 7 | Hardening/release |
| `schema_version` negotiation | 7 | No live drift problem yet |

---

## §11 — References

- `AI_Native_OS_Whitepaper.md` §8 (UX & Graduated Determinism — Tier 0–3, HITL design), §10 Phase 5, §12 F4, §13 KPI 5
- `docs/implementation_plan.md` §0 (authoritative phase status)
- `dual-brain/docs/phase2/phase2_roadmap_CLOSED_2026-06-11.md` §10 (deferred-to-Phase-5 list), §5 (cross-cutting decisions)
- `dual-brain/controller/hitl.py` (SF-1/2/3 — `_render`, `read_decision`, `lockout`)
- `dual-brain/controller/audit.py` (SF-5/6 — append-only, `_redact_params`)
- `dual-brain/controller/config.py` (`SecretRef`, `[hitl] presenter` seam, secret-key rejection)
- `dual-brain/controller/risk_classifier.py`, `main.py`, `intent_store.py`, `repl.py`
- `CLAUDE.md` § INV-1…INV-8, § Test-Only Knobs (feature-flag pattern)

---

## §12 — Implementation Appendix

### Consolidated additive `controller.toml` (Phase 5 sections)

All sections are optional and default to current behavior when absent — so an existing
Phase-2 config keeps working, and each feature is independently flag-gated.

```toml
[hitl]
lockout_seconds   = 3          # INV-6 (unchanged)
timeout_seconds   = 30
presenter         = "terminal" # "terminal" | "gtk" | "web"  (P2)
trust_ttl_seconds = 1800       # session-trust lifetime; 0 disables [T]rust

[keymap]                       # raw single-keypress; Esc=deny reserved
approve = ["1","a","y"]
deny    = ["2","d","n"]
modify  = ["3","m"]
explain = ["4","e"]
trust   = ["5","t"]

[risk]
strategy = "rules"             # pluggable classifier; "rules" default

[tier2]
enabled  = true
strategy = "llm"               # "llm" | "rule" | "external"
max_retries = 2

[audit]
sink = "file"                  # "file" | "journald" | "system-appender" (P2)
redact_entropy_bits = 3.5

[cost]                         # P1
session_ceiling_usd = 1.00     # 0 = no ceiling
warn_fraction       = 0.8

[limits]                       # P1
max_input_chars   = 4000
max_turns_per_min = 30
```

### New / modified module map (P0)

| Module | New? | Public surface |
|---|---|---|
| `controller/keymap.py` | new | `Action`, `Keymap`, `load_keymap()`, `DEFAULTS` |
| `controller/trust_store.py` | new | `TrustGrant`, `TrustStore.{grant,is_trusted,revoke,list}` |
| `controller/tier2_review.py` | new | `Tier2Decision`, `Tier2Reviewer`, `get_reviewer()`, `TIER2_SCHEMA` |
| `controller/hitl.py` | mod | `_sanitize_display`, raw-keypress `read_decision`, `Decision.{MODIFY,EXPLAIN,TRUST}` |
| `controller/risk_classifier.py` | mod | `register_classifier`, `get_classifier` (default `"rules"`=`classify`) |
| `controller/audit.py` | mod | `seq`/`prev_hash` chain, `verify_chain()`, `AuditSink`/`FileSink`, redaction++ |
| `controller/main.py` | mod | trust consult, Tier-2 escalate-only insert, `[M]` reclassify loop, `_qb_explain` |
| `controller/config.py` + `schemas/controller_config.json` | mod | `[hitl]+`, `[keymap]`, `[risk]`, `[tier2]`, `[audit]`, `[cost]`, `[limits]` |

### New `Outcome` values (audit.py)

`TRUST_GRANTED`, `TRUST_APPLIED`, `TIER2_ESCALATED`, `MODIFIED` — so the usability sim
(M5.V2) and the audit viewer (M5.P1-viewer) can distinguish these flows from existing
outcomes when deriving KPI-5 metrics.

### Build sequencing (per-PR, each behind its flag)

```
PR-1  keymap.py + config [keymap]                 (no behavior change until wired)
PR-2  hitl.py SF-1 sanitize + SF-2/3 raw input    (security-critical — 2 reviewers)
PR-3  trust_store.py + [T]rust wiring in main.py   (depends PR-1, PR-2)
PR-4  hitl [E]/[M] actions + intent_store.revise   (depends PR-2)
PR-5  classifier registry + tier2_review.py + wire (depends nothing in M5.1)
PR-6  audit.py hash-chain + redaction++ + verify    (security-critical — 2 reviewers)
PR-7  ci.sh P0 gates G5.1–G5.6 + closeout stub
```

PR-2 and PR-6 touch security-critical files → WF-6 (two human reviewers, explicit `LGTM`).
PR-1/PR-5/PR-6 are independent and can land in parallel; PR-3/PR-4 gate on PR-2.

---

*Created June 2026 — Phase 5 planning. Living document; update as milestones land and gates pass.*
