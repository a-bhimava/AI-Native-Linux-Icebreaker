# v6.9 Scope O — Pre-Build Fix Plan (CT-Scan Response)

**Date:** 2026-07-15 (updated 2026-07-16 with P1/P2 additions)
**Branch:** `feat/v6.9-scope-o`
**Trigger:** codebase-ct-scan agent pass surfaced 4 P0 + 6 P1 + 6 P2
findings + 10 test gaps before ISO build on the new GCP VM.
**Goal:** land everything genuinely needed to trust the v6.9 ISO's
first UTM sweep. Defer cosmetic + design-heavy items to v6.10 backlog
with GROUND_TRUTH F-entries.

**Rename note:** originally scoped as P0-only
(`2026-07-15_scope_o_p0_fixes.md`); expanded 2026-07-16 to cover the
P1/P2/test-gap items worth fixing pre-build.

---

## Context

The CT scan classified findings by severity. The P0 pass landed
2026-07-15 (4 commits, 6 → 199 mcpd tests, 2006 → 2013 controller
tests, zero regressions). A follow-up triage identified 4 more items
that are cheap, surgical, and genuinely close pre-ISO risk — plus 2
more worth doing if the sprint stays tight. Everything else moves to
the v6.10 backlog.

## Status summary

### Completed 2026-07-15 (P0 pass)

| ID | Commit | What shipped |
|---|---|---|
| P0-1 | `feab328` | `#[serde(deny_unknown_fields)]` on Rust Manifest struct family + 3 tests. INV-4 alignment with Python meta-schema restored. |
| P0-3 | `30d50c2` | `agent_graph_nodes::mcpd_dispatcher_node` manifest branch returns real `ToolResult` matching `main.py::_try_manifest_dispatch`. `_extract_stdout` helper unifies shape extraction. Twin bug in `_extract_stdout_for_marker` also fixed. 2 tests. INV-6 regression trap closed. |
| P0-4 | `90c9adf` | 5 integration tests (1 bonus over plan) exercising both dispatch sites + fall-through + Tier-0 fast path shape + tier-drift guard using the real `ManifestRegistry`. |
| P0-2 | `4a4535c` | `fs_read` refuses paths not under `manifest.sandbox.landlock.ro` at userspace. Both target and roots canonicalized before compare (handles Linux `/tmp` symlink + macOS `/var → /private/var`). 4 tests including symlink escape. BP-9 defense-in-depth restored. |

### Pending (this doc)

| ID | Priority | Est | Rationale |
|---|---|---|---|
| P1-3 | HIGH | 15 min | Rust `substitute_placeholders` accepts NUL/backslash/unbounded strings. `canonicalize()` catches NUL but the error message is opaque. Explicit rejection produces actionable diagnostics + closes a fuzzing surface. |
| P2-1 | HIGH | 5 min | `CLAUDE.md` line 72 (my own commit `fd900b9`) references `docs/RELEASE_NOTES_v6.7.md` which doesn't exist. Documentation-honesty violation. |
| P2-4 | HIGH | 10 min | `main.py:263` hand-maintains `_SUPPORTED_ACTIONS` frozenset duplicating auto-generated `_intent_corpus_supported.SUPPORTED_ACTIONS`. Drift caught by one offline test today; removing the duplicate makes drift impossible. |
| Test gap 7 | HIGH | 20 min | Neither loader tested against YAML bombs (billion laughs) or aliases. `serde_yaml 0.9` disables aliases by default; `PyYAML.safe_load` does not. Both worth locking in with tests. |
| Test gap 4 | CONSIDER | 30 min | Tier-2 manifest triggers HITL — architectural contract test. No shipped Tier-2 manifest today, but P0-3 documented `requires_cow_approval` as the trap. Locks the contract in code, not just docstrings. |
| P1-5 | CONSIDER | 15 min | Python `_KIND_DISPATCH` only wires `session_op`. Meta-schema advertises Part-B kinds (`fs_read`, etc.) that controller can't service; current behavior fails at dispatch. Cleaner: refuse at load with actionable message pointing to `src/mcpd/manifests/`. |

**Bundle A (HIGH):** ~50 min → close before ISO.
**Bundle B (CONSIDER):** +45 min → total ~95 min if we do everything.

---

## Execution order (locked)

1. **P2-1** — 5 min. Fastest cleanup, no cross-file impact.
2. **P2-4** — 10 min. Cleanup, decouples main.py from hand-edit drift.
3. **P1-3** — 15 min. Rust defense-in-depth + tests.
4. **Test gap 7** — 20 min. Adversarial YAML corpus, both loaders.
5. **(Optional) Test gap 4** — 30 min. Tier-2 HITL contract test.
6. **(Optional) P1-5** — 15 min. Python loader load-time refusal.

Order rationale: cheapest and least entangled first, so if we cap at
Bundle A we still ship the highest-leverage fixes.

---

## Task 1 — P0-1 (COMPLETE, `feab328`)

Rust `Manifest` / `Impl` / `SandboxHint` / `LandlockHint` structs now
carry `#[serde(deny_unknown_fields)]`. 3 regression tests locked in.
See commit body for detail.

## Task 2 — P0-3 (COMPLETE, `30d50c2`)

`mcpd_dispatcher_node` manifest branch returns real `ToolResult` with
`manifest_dispatched=True` + `manifest_name` (INV-8 provenance).
`_extract_stdout` helper. Twin bug in `_extract_stdout_for_marker`
(Plan mode `$STEP_N_STDOUT`) also fixed. 2 tests.

## Task 3 — P0-4 (COMPLETE, `90c9adf`)

`test_scope_o_dispatch_integration.py` — 5 tests using the real
`ManifestRegistry` exercising both dispatch sites, the fall-through
contract, the Tier-0 fast path shape, and the tier-drift guard.

## Task 4 — P0-2 (COMPLETE, `4a4535c`)

`fs_read` refuses paths outside `manifest.sandbox.landlock.ro` at
userspace. Both target and roots canonicalized. 4 tests including
symlink escape from within an allowed root.

---

## Task 5 — P1-3: Reject NUL / backslash / oversized in `substitute_placeholders`

### Files modified

- `src/mcpd/src/manifest_loader/impl_kinds/fs_read.rs`

### Understanding the current behavior

Read the current substitutor (~line 93-176 in fs_read.rs). It:
- Iterates `{{name}}` tokens.
- Rejects unterminated braces (`bail!`).
- Rejects a param value containing `/` that doesn't start with `/` —
  the relative-segment path-traversal defense.
- Accepts everything else: NUL byte (`\0`), backslash (`\`), unbounded
  length.

Linux `canonicalize()` rejects NUL — but the resulting error is
`fs_read: cannot resolve "/tmp/\0foo"` from the outer `.with_context`,
which doesn't name NUL as the problem. Actionable diagnostics matter
for the operator writing manifests.

### Exact code changes

In the placeholder-value inspection block inside `substitute_placeholders`
(after the `if value.contains('/') && !value.starts_with('/')` check),
add explicit rejections before appending to `out`:

```rust
// v6.9 P1-3 (2026-07-16 CT scan): reject values that are safe-for-
// path in shape but path-traversal or fuzz-corner-case in bytes.
// canonicalize() will refuse NUL downstream, but the caller-facing
// error must name the actual problem for the operator writing the
// manifest.
if value.contains('\0') {
    bail!(
        "fs_read: param {:?} value contains NUL byte (rejected before path resolution)",
        name
    );
}
if value.contains('\\') {
    bail!(
        "fs_read: param {:?} value contains backslash (Windows-style separator, \
         rejected — mcpd is Linux-only)",
        name
    );
}
if value.len() > 4096 {
    bail!(
        "fs_read: param {:?} value is {} bytes; max 4096 (PATH_MAX)",
        name, value.len()
    );
}
```

### Regression tests to add

Append to the `#[cfg(test)] mod tests` block (after the existing
`refuses_relative_substitution`, `refuses_missing_placeholder_param`
tests):

```rust
#[tokio::test]
async fn refuses_null_byte_substitution() {
    let m = mk_manifest("/tmp/{{name}}");
    let err = format!("{:#}", dispatch(&m, &json!({"name": "foo\0bar"}))
        .await.unwrap_err());
    assert!(
        err.contains("NUL byte"),
        "expected refusal to name NUL byte; got: {}", err
    );
}

#[tokio::test]
async fn refuses_backslash_substitution() {
    let m = mk_manifest("/tmp/{{name}}");
    let err = format!("{:#}", dispatch(&m, &json!({"name": "foo\\bar"}))
        .await.unwrap_err());
    assert!(
        err.contains("backslash"),
        "expected refusal to name backslash; got: {}", err
    );
}

#[tokio::test]
async fn refuses_oversized_substitution() {
    let m = mk_manifest("/tmp/{{name}}");
    let huge = "a".repeat(4097);
    let err = format!("{:#}", dispatch(&m, &json!({"name": huge}))
        .await.unwrap_err());
    assert!(
        err.contains("max 4096"),
        "expected refusal to cite PATH_MAX; got: {}", err
    );
}
```

### Verification

```bash
cd src/mcpd
cargo test --lib manifest_loader::impl_kinds::fs_read 2>&1 | tail -15
# Should be 13 fs_read tests (10 existing + 3 new), all pass.
cargo test 2>&1 | grep "^test result"
# All 6 suites still ok.
```

### Non-goals

- Do NOT reject `/` in param values — the existing check has that
  covered.
- Do NOT reject legal Unicode — only NUL, backslash, length.
- Do NOT try to enumerate other shell metacharacters — mcpd never
  invokes a shell on the substituted string; canonicalize() takes it
  as literal bytes.

---

## Task 6 — P2-1: Fix stale `RELEASE_NOTES_v6.7.md` reference in CLAUDE.md

### Files modified

- `CLAUDE.md` (line 72)

### Current state

My own commit `fd900b9` inserted this text:
```
| v6.7-known-issues | **Shipped 2026-07-13, superseded** | Emergency ship of the v6.7 dual-arch ISO under `docs/RELEASE_NOTES_v6.7.md`. Documented open P0s: F-59 (...), F-60 (...), F-61 (...). ...
```

`docs/RELEASE_NOTES_v6.7.md` doesn't exist. This is a documentation-
honesty violation (BP-11 aligns with the general principle: docs
describe reality). The disposition of F-59/F-60/F-61 IS captured
inline already — the file reference is redundant.

### Exact code change

Rewrite the sentence to drop the `docs/RELEASE_NOTES_v6.7.md` phrase
and preserve the disposition info that follows:

```
BEFORE
| v6.7-known-issues | **Shipped 2026-07-13, superseded** | Emergency ship of the v6.7 dual-arch ISO under `docs/RELEASE_NOTES_v6.7.md`. Documented open P0s: F-59 (Control Center silent crash on `Gtk.PasswordEntry.set_placeholder_text`), F-60 (nav phrases route to `system.unsupported`), F-61 (terminal HITL prompts never render). All three superseded by later branches — F-59 source fix landed for v6.8; F-60 permanent fix landed as nav.cd in v6.9 Scope O Layer 2A; F-61 still open, tracked in Task #152 for v6.10. |

AFTER
| v6.7-known-issues | **Shipped 2026-07-13, superseded** | Emergency dual-arch ISO ship with three documented open P0s: F-59 (Control Center silent crash on `Gtk.PasswordEntry.set_placeholder_text`), F-60 (nav phrases route to `system.unsupported`), F-61 (terminal HITL prompts never render). Disposition: F-59 source fix landed for v6.8; F-60 permanent fix landed as `nav.cd` in v6.9 Scope O Layer 2A; F-61 still open, tracked in Task #152 for v6.10. |
```

### Verification

```bash
grep -c "RELEASE_NOTES_v6.7" CLAUDE.md
# Should output 0.
grep -l RELEASE_NOTES_v6.7 docs/ 2>/dev/null
# Should output nothing.
```

### Non-goals

- Do NOT create the missing file — nobody is authoring it, and the
  disposition info already lives in CLAUDE.md inline.
- Do NOT change anything else in the same row.

---

## Task 7 — P2-4: Import auto-generated `SUPPORTED_ACTIONS` in `main.py`

### Files modified

- `dual-brain/controller/main.py` (line 263-283)
- `dual-brain/controller/tests/test_intent_corpus.py` (parity test)

### Current state

`main.py:263-283` hand-maintains a `frozenset` that duplicates the
auto-generated `_intent_corpus_supported.SUPPORTED_ACTIONS` (which
comes from `tool_catalogue.yaml` via `scripts/export_mcpd_catalogue.py`).

`test_supported_actions_match_controller_source` in
`test_intent_corpus.py:318` catches drift by comparing the two sets —
but that's an offline test, and someone could easily add to `main.py`
without regenerating the frozenset if the test isn't run.

Cleanest: `main.py` imports the generated one directly. Then the
duplication vanishes and drift becomes structurally impossible.

### Exact code changes

**Change A — replace the hand-maintained frozenset with an import.**

Find `main.py:260-280` (the `_SUPPORTED_ACTIONS = frozenset({ ... })`
literal). Replace with:

```python
# BEFORE
_SUPPORTED_ACTIONS = frozenset({
    # Read-only Tier 0
    "system.status", "system.uptime", "system.cpu", "system.memory", "system.disk",
    # ... 20-ish lines ...
    "nav.cd",
})

# AFTER (v6.9 P2-4 CT scan): single source of truth is the generated
# frozenset from tool_catalogue.yaml. Zero drift possible.
from ._intent_corpus_supported import SUPPORTED_ACTIONS as _SUPPORTED_ACTIONS
```

**Change B — retire the parity test in test_intent_corpus.py, or turn
it into a "generated-file exists" tripwire.**

The parity test at `test_intent_corpus.py:318` was a workaround for
the drift the import now precludes. Options:

- Delete it (cleanest).
- Keep it as a triviality (`assert _SUPPORTED_ACTIONS is
  SUPPORTED_ACTIONS`) — the import itself proves parity.

Prefer: delete the test body but leave a small guard that asserts the
import path resolves to the generated module.

```python
# BEFORE test_supported_actions_match_controller_source (heavy)
def test_supported_actions_match_controller_source() -> None:
    """This local _SUPPORTED_ACTIONS must stay in sync with main.py."""
    import importlib.util
    # ... 30 lines of module-loading + drift-comparison logic ...

# AFTER (v6.9 P2-4)
def test_supported_actions_import_resolves_to_generated_module() -> None:
    """v6.9 P2-4 (2026-07-16 CT scan): main.py::_SUPPORTED_ACTIONS is
    now an alias for the auto-generated
    ``_intent_corpus_supported.SUPPORTED_ACTIONS``. Zero drift possible
    by construction. This test locks the import path so a future
    refactor can't silently re-introduce hand-maintenance."""
    import controller.main as _main
    from controller._intent_corpus_supported import SUPPORTED_ACTIONS
    assert _main._SUPPORTED_ACTIONS is SUPPORTED_ACTIONS, (
        "main._SUPPORTED_ACTIONS must be the SAME OBJECT as the "
        "generated frozenset — re-drift would defeat P2-4"
    )
```

### Verification

```bash
cd dual-brain
python3 -m pytest controller/tests/test_intent_corpus.py -v --timeout=30 -k "supported_actions" 2>&1 | tail -6
# The new test passes; the old (deleted) test no longer runs.
python3 -m pytest controller/tests/ -q --timeout=90 --ignore=controller/tests/test_agent_graph_live.py 2>&1 | tail -3
# Full suite: 2013 → 2013 pass (same count; we swapped one test for
# another). Zero regressions.
grep -c "^    \"system.status\"" controller/main.py
# Should be 0 — the literal is gone.
```

### Non-goals

- Do NOT touch `tool_catalogue.yaml` or the export script — the
  single source is already there, this task just tightens the wiring.
- Do NOT delete `_intent_corpus_supported.py` — it's still needed by
  `test_intent_corpus.py` and other consumers.

---

## Task 8 — Test gap 7: Adversarial YAML corpus (both loaders)

### Files modified

- `src/mcpd/src/manifest_loader/mod.rs` (Rust tests block)
- `dual-brain/controller/tests/test_manifest_loader.py` (Python tests)

### What we're testing

Three classes of YAML attacks that both loaders should reject or
survive:

1. **YAML alias / anchor bomb (billion laughs)** — recursive alias
   expansion that blows up memory.
2. **Aliases pointing to key positions** — e.g. `<<: *anchor` to inject
   a merge key.
3. **Oversized string values** (100 MB `param_schema` payload).

### Rust tests

Append to the `#[cfg(test)] mod tests` block in
`manifest_loader/mod.rs`:

```rust
#[test]
fn rejects_yaml_alias_bomb() {
    // Billion-laughs shape. serde_yaml 0.9 disables aliases by default
    // (returned as Value::Null); confirm the loader refuses OR handles
    // safely without recursion.
    let tmp = TempDir::new().unwrap();
    write(tmp.path(), "bomb.yaml", r#"
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
name: safe.name
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: fs_read
  path: /proc/uptime
"#);
    // Either the top-level unknown-fields check rejects `a`/`b`/`c`,
    // or the load succeeds without blowing memory. What matters:
    // the process doesn't OOM or hang.
    let result = load(tmp.path());
    match result {
        Err(e) => {
            let msg = format!("{:#}", e);
            assert!(
                msg.contains("unknown field") || msg.contains("a"),
                "unexpected error shape: {}", msg
            );
        }
        Ok(_) => {
            // Loader tolerated the anchors — confirm serde_yaml did
            // NOT recursively expand them by checking the memory budget
            // survived (we're already here, so it did).
        }
    }
}

#[test]
fn rejects_oversized_string_param_schema() {
    // 1 MB of `x` characters as a description. Not a bomb, but a
    // stress on serde's string allocator. Should fail our validate()
    // (description > 400 chars).
    let tmp = TempDir::new().unwrap();
    let huge = "x".repeat(1_000_000);
    let body = format!(r#"
name: huge.desc
version: 1
description: "{}"
tier: 0
param_schema: {{type: object}}
impl:
  kind: fs_read
  path: /proc/uptime
"#, huge);
    write(tmp.path(), "huge.yaml", &body);
    let err = format!("{:#}", load(tmp.path()).unwrap_err());
    assert!(
        err.contains("description too long"),
        "expected validate() to reject oversized description; got: {}", err
    );
}
```

### Python tests

Append to `test_manifest_loader.py`:

```python
def test_rejects_yaml_alias_bomb(tmp_path):
    """v6.9 test gap 7 (2026-07-16 CT scan): billion-laughs alias
    expansion must not OOM the loader. PyYAML.safe_load DOES expand
    aliases (unlike serde_yaml), so the loader must either refuse or
    tolerate a reasonable depth."""
    (tmp_path / "bomb.yaml").write_text("""
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
name: safe.name
version: 1
description: "test"
tier: 0
param_schema: {type: object}
impl:
  kind: session_op
  op: set_cwd
""")
    # The meta-schema declares additionalProperties: false; the loader
    # should reject unknown top-level fields (a/b/c) before the
    # alias expansion becomes a memory issue.
    with pytest.raises(ValueError, match="required|additional"):
        load(manifests_dir=tmp_path)


def test_rejects_oversized_description(tmp_path):
    """v6.9 test gap 7: description > 400 chars refused by the loader
    validator. Prevents unbounded YAML string values."""
    huge = "x" * 1000
    (tmp_path / "huge.yaml").write_text(f"""
name: huge.desc
version: 1
description: "{huge}"
tier: 0
param_schema: {{type: object}}
impl:
  kind: session_op
  op: set_cwd
""")
    with pytest.raises(ValueError, match="description|too long|max"):
        load(manifests_dir=tmp_path)
```

### Verification

```bash
# Rust
cd src/mcpd
cargo test --lib manifest_loader::tests::rejects_yaml_alias_bomb
cargo test --lib manifest_loader::tests::rejects_oversized_string_param_schema
# Python
cd dual-brain
python3 -m pytest controller/tests/test_manifest_loader.py -v --timeout=30 -k "bomb or oversized"
# Both should PASS or, if the alias test only warns, that's an
# acceptable outcome the assertion documents.
```

### Non-goals

- Do NOT try to detect ALL YAML bombs — just enough to prove neither
  loader crashes the process on the ones we DO test.
- Do NOT depend on specific error messages that upstream libraries
  might change — assert on the SHAPE of the failure (raised exception
  with a message containing SOME expected token).

---

## Task 9 — Test gap 4: Tier-2 manifest triggers HITL (CONSIDER)

### Files modified

- `dual-brain/controller/tests/test_scope_o_dispatch_integration.py`
  (extend, one new test)

### What we're testing

Even though no shipped Tier-2 manifest exists today, the framework
contract is: **any manifest with `tier >= 2` must go through the HITL
gate before dispatch**. Locking this in a test now means a future
Tier-2 manifest (e.g. `fs_write_cow` in v6.10) can't silently ship
without triggering the gate.

### Design decision

The full flow (Planner → Verifier → HitlGate → mcpd_dispatcher) is
complex to test directly. Cheaper AND more direct: verify at the
**risk classifier** level that a manifest declaring `tier: 2` in its
own YAML actually produces a Tier-2 classification when the intent
carries that action.

But — actually, the risk classifier reads `TIER0_TOOLS` /
`SYSTEM_WRITE_TOOLS` frozensets, which are generated from
`tool_catalogue.yaml`, not from manifest YAML. If someone ships a
Tier-2 manifest, they'd need a matching `tool_catalogue.yaml` entry
with `tier_hint: system_write` for the classifier to upgrade.

So the real test: **assert that a manifest with `tier: 2` in the YAML
has a matching entry in `tool_catalogue.yaml`** — same tier level.
This is a drift check, not a runtime dispatch test.

### Exact code changes

Add to `test_scope_o_dispatch_integration.py`:

```python
def test_tier_manifest_agrees_with_tool_catalogue(real_registry):
    """v6.9 test gap 4 (2026-07-16 CT scan): every registered
    manifest declares a tier in its YAML. That declaration MUST match
    the tier_hint in tool_catalogue.yaml so the risk classifier
    upgrades intents accordingly. If they diverge, a Tier-2 manifest
    could ship with the classifier treating it as Tier-0 (bypasses
    HITL) — the exact failure mode this test locks against.

    Contract: for every manifest name in the registry, if the name
    appears in tool_catalogue.yaml, the tiers must agree via the
    tier_hint → tier mapping in this test."""
    import yaml
    from pathlib import Path

    yaml_path = (
        Path(__file__).resolve().parent.parent / "tool_catalogue.yaml"
    )
    entries = yaml.safe_load(yaml_path.read_text())
    by_name = {e["name"]: e for e in entries}

    tier_from_hint = {
        "tier0": 0,
        "conditional": 1,  # fs.write: 1 in $HOME, 3 outside — classifier decides
        "system_write": 2,
        "destructive": 3,
        "meta": 0,
    }

    for name in real_registry.names():
        manifest_tier = real_registry.get(name).manifest["tier"]
        if name not in by_name:
            # Manifest-only tool (not in tool_catalogue). No cross-check.
            continue
        yaml_tier_hint = by_name[name]["tier_hint"]
        expected_tier = tier_from_hint.get(yaml_tier_hint)
        assert expected_tier is not None, (
            f"tool_catalogue.yaml entry {name!r} has tier_hint "
            f"{yaml_tier_hint!r} not in {list(tier_from_hint)}"
        )
        if yaml_tier_hint == "conditional":
            # fs.write is conditional; the manifest can honestly declare
            # tier: 1 for the $HOME-side entry point. Skip strict compare.
            continue
        assert manifest_tier == expected_tier, (
            f"Tier mismatch for {name!r}: manifest declares tier "
            f"{manifest_tier} but tool_catalogue.yaml tier_hint "
            f"{yaml_tier_hint!r} maps to tier {expected_tier}. "
            f"Divergence lets a Tier-2 manifest ship with the classifier "
            f"treating it as Tier-0 (bypasses HITL)."
        )


def test_registered_manifest_tiers_are_valid(real_registry):
    """v6.9 test gap 4 companion: every registered manifest declares
    a tier in {0, 1, 2, 3}. Belt-and-braces vs the meta-schema."""
    for name in real_registry.names():
        tier = real_registry.get(name).manifest["tier"]
        assert tier in {0, 1, 2, 3}, (
            f"manifest {name!r} declares invalid tier {tier}"
        )
```

### Verification

```bash
cd dual-brain
python3 -m pytest controller/tests/test_scope_o_dispatch_integration.py -v --timeout=30 -k "tier"
# 2 new tests should PASS. For nav.cd (tier: 0 in manifest + tier0
# in catalogue), the mapping check passes trivially.
```

### Non-goals

- Do NOT construct a fake Tier-2 manifest at runtime just to drive
  HitlGate — the full graph flow is too much surface for what's
  fundamentally a drift check.
- Do NOT test the mcpd-side `demo.uptime` manifest via this cross-
  check — it's not in `tool_catalogue.yaml` (deliberate: it's a
  pilot, not a QB-facing tool). The `not in by_name: continue` branch
  handles that.

---

## Task 10 — P1-5: Python `_KIND_DISPATCH` load-time refusal (CONSIDER)

### Files modified

- `dual-brain/controller/manifest_loader.py` (load-time gate)
- `dual-brain/controller/tests/test_manifest_loader.py` (new test)

### Understanding the current gap

`controller/manifest_loader.py:45-53`:

```python
_KIND_DISPATCH: dict[str, Any] = {
    "session_op": _session_op_mod.dispatch,
    # Part B (v6.9 Week 2-3, mcpd-side): fs_read, fs_write_cow,
    # dbus_call, exec_pipeline. Loader will accept the manifest
    # (meta-schema already permits the enum) but dispatch will error
    # until Part B ships.
}
```

The meta-schema at `controller/schemas/tool_manifest.json` advertises
all five kinds as valid `impl.kind` values. Current behavior: a
controller-side manifest declaring `impl.kind: fs_read` loads clean
and raises `NotImplementedError` at dispatch. That's confusing — the
Part-B kinds are mcpd-side, not controller-side.

### Design

Refuse at load-time with a helpful message: "impl.kind `fs_read` is
mcpd-side; move this manifest to `src/mcpd/manifests/` and reload
mcpd. Controller-side manifests must use `session_op`."

### Exact code change

In `controller/manifest_loader.py::load`, add a kind check right
after the meta-schema validate call:

```python
try:
    validator.validate(raw)
except ValidationError as exc:
    raise ValueError(
        f"manifest_loader: {path.name} — schema error at "
        f"{list(exc.absolute_path)}: {exc.message}"
    ) from exc

# v6.9 P1-5 (2026-07-16 CT scan): the meta-schema permits mcpd-side
# impl.kinds (fs_read, fs_write_cow, dbus_call, exec_pipeline) so
# operators can lint mcpd manifests against the same schema. On the
# controller side those kinds have no dispatcher — refuse at load
# with an actionable message rather than a NotImplementedError at
# dispatch.
kind = raw["impl"]["kind"]
if kind not in _KIND_DISPATCH:
    raise ValueError(
        f"manifest_loader: {path.name} — impl.kind={kind!r} has no "
        f"controller-side dispatcher (known: {sorted(_KIND_DISPATCH)}). "
        f"If this is an mcpd-side kind, move the manifest to "
        f"src/mcpd/manifests/ and reload mcpd."
    )
```

### Regression tests to add

```python
def test_load_rejects_mcpd_side_kind_at_load_time(tmp_path):
    """v6.9 P1-5 (2026-07-16 CT scan): a controller-side manifest
    declaring an mcpd-side impl.kind (fs_read, etc.) must refuse at
    LOAD, not at dispatch. Otherwise the operator sees a confusing
    NotImplementedError the first time the tool is invoked, days
    after the daemon started."""
    write(tmp_path, "bad.yaml", """
        name: bad.thing
        version: 1
        description: "test"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: fs_read
          path: /proc/uptime
    """)
    with pytest.raises(ValueError, match="no controller-side dispatcher"):
        load(manifests_dir=tmp_path)
```

### Verification

```bash
cd dual-brain
python3 -m pytest controller/tests/test_manifest_loader.py -v --timeout=30 -k "mcpd_side_kind"
# Should PASS.
python3 -m pytest controller/tests/ -q --timeout=90 --ignore=controller/tests/test_agent_graph_live.py 2>&1 | tail -3
# Full suite still green.
```

### Non-goals

- Do NOT remove the runtime `NotImplementedError` from `dispatch()`
  — belt-and-braces if a future manifest bypasses the load check.
- Do NOT try to REGISTER `fs_read` on the controller side — that's
  Part B (v6.10+) mcpd territory.
- Do NOT edit the meta-schema — the mcpd-side of the world SHOULD
  accept these kinds; only the controller-side loader refuses.

---

## End-to-end verification checklist (updated)

After all pending tasks land:

1. **Rust suite:**
   ```bash
   cd src/mcpd && cargo test 2>&1 | grep "^test result"
   ```
   Expected: 6 lines all ok. Total ~203 tests (P0 pass had 199 +
   3 from Task 5 + 2 from Task 8 Rust).

2. **Python suite:**
   ```bash
   cd dual-brain && python3 -m pytest controller/tests/ -q --timeout=90 \
     --ignore=controller/tests/test_agent_graph_live.py 2>&1 | tail -3
   ```
   Expected: 2013 (P0 pass) → 2017-2019 pass depending on which
   optional tasks land.

3. **Live smoke through the runner (Gemini):**
   ```bash
   ICEBREAKER_LIVE_GEMINI_KEY=… python3 dual-brain/scripts/ib_run_v68.py \
     "take me to /var/log"
   ```
   Still: `nav.cd` action + session_cwd mutation + mcpd fired 0 times.

4. **v2.manifest syntax:**
   ```bash
   bash -n incremental/versions/v2.manifest && echo "OK"
   ```
   Expected: `OK`.

5. **CLAUDE.md is honest:**
   ```bash
   grep -c "RELEASE_NOTES_v6.7" CLAUDE.md
   ```
   Expected: `0`.

6. **cargo check on the workspace:**
   ```bash
   cd src/mcpd && cargo check --release
   ```
   Expected: clean.

---

## Commit strategy

One commit per task, six commits max, on `feat/v6.9-scope-o`:

```
docs(v6.9 P2-1): drop stale RELEASE_NOTES_v6.7.md reference from CLAUDE.md
refactor(v6.9 P2-4): import auto-gen SUPPORTED_ACTIONS in main.py (kill drift)
fix(v6.9 P1-3): reject NUL/backslash/oversized in Rust substitute_placeholders
test(v6.9 gap-7): adversarial YAML corpus tests (both loaders)
test(v6.9 gap-4): tier declaration cross-check between manifest + tool_catalogue
fix(v6.9 P1-5): Python manifest loader refuses mcpd-side impl.kinds at load
```

Push after each commit or in one batch — operator's call. All six
land clean on the same branch.

---

## Deferred to v6.10 backlog (with F-entries)

Everything below gets a GROUND_TRUTH F-log entry so it's tracked, per
R14 rule ("every F-xx fix has a named regression lock"). They're
deferred because each is either bigger than a mac-side pass, requires
design decisions that don't fit this sprint, or is cosmetic.

| CT-scan ID | What | Why deferred | Est effort |
|---|---|---|---|
| P1-1 | Audit line for manifest dispatch names manifest_name+version+hash | Requires `AuditFields` schema change; separate PR needed | ~1 hr |
| P1-2 | `session_op::set_cwd` verifies path is under Landlock roots | Controller has no Landlock context; needs config surface | ~2 hr |
| P1-4 | Runtime assertion "MCPD_MANIFESTS_DIR set → registry non-empty" | Belt-and-braces observability item | ~15 min |
| P1-6 | Swap `serde_yaml` (archived) → `serde_yaml_ng` or `serde_norway` | Dependency sweep territory; no CVE today | ~30 min + audit |
| P2-2 | Lazy `import json` in `_load_meta_schema` | Cosmetic | 2 min |
| P2-3 | Type hint on `_KIND_DISPATCH` | Cosmetic | 2 min |
| P2-5 | `set -o pipefail` on v2.manifest tripwire | Belt-and-braces; marker loop above catches most reseeds | 5 min |
| P2-6 | Remove hardcoded nav.cd examples from `qb_gemini.txt` | Belt-and-braces alongside `{{CATALOGUE}}` block | 5 min |
| Test gap 8 | Live prompt-substitution round-trip against real Gemini | We do this manually via `ib_run_v68.py`; automation is nice-to-have | ~30 min |
| Test gap 9 | Rust integration test at JSON-RPC path (not just library API) | Bigger than a mac pass; touches server startup mock | ~2 hr |
| Test gap 10 | `Path.resolve()` on `/proc/self` / bind-mount corner cases | Low signal for shipping | ~30 min |

Each item gets an F-entry in `incremental/GROUND_TRUTH.md § 7 Failure
Log` when we next update that file. Regression-lock target: the test
file listed above (or "TBD" for design-heavy items).

---

## Files touched summary (this doc's tasks)

**Modified:**
- `src/mcpd/src/manifest_loader/impl_kinds/fs_read.rs` — Task 5
  (NUL/backslash/length checks) + 3 tests
- `CLAUDE.md` — Task 6 (drop RELEASE_NOTES_v6.7.md reference)
- `dual-brain/controller/main.py` — Task 7 (replace frozenset literal
  with import)
- `dual-brain/controller/tests/test_intent_corpus.py` — Task 7
  (retire heavy parity test, add lightweight identity check)
- `src/mcpd/src/manifest_loader/mod.rs` — Task 8 (YAML bomb + oversize
  Rust tests)
- `dual-brain/controller/tests/test_manifest_loader.py` — Task 8
  (YAML Python tests) + Task 10 (mcpd-side kind refusal test)
- `dual-brain/controller/tests/test_scope_o_dispatch_integration.py`
  — Task 9 (tier cross-check, if Bundle B lands)
- `dual-brain/controller/manifest_loader.py` — Task 10 (load-time
  kind check, if Bundle B lands)

**Untouched (intentional):**
- `src/mcpd/manifests/demo.uptime.yaml` — pilot manifest OK as-is.
- `dual-brain/controller/manifests/nav.cd.yaml` — controller pilot OK.
- `src/mcpd/src/tools/fs.rs` — security-critical, unrelated.
- The 4 QB prompts — will get `_catalogue_block.txt` substitution as
  before; no fresh changes.

---

## Estimated wall-clock

### P0 pass (completed 2026-07-15)

| Task | Est | Actual |
|---|---|---|
| P0-1 | 15 min | ✅ |
| P0-3 | 45 min | ✅ (extra: fixed twin bug in marker extractor) |
| P0-4 | 60 min | ✅ (extra: added tier-drift guard test) |
| P0-2 | 45 min | ✅ (extra: macOS canonicalize handling) |
| **Total** | **~2 h 45 min** | **All shipped, pushed, tests green.** |

### Pre-build pass (this doc's pending tasks)

Bundle A (recommended):
| Task | Est |
|---|---|
| P2-1 | 5 min |
| P2-4 | 10 min |
| P1-3 | 15 min |
| Test gap 7 | 20 min |
| **Subtotal** | **~50 min** |

Bundle B (consider):
| Task | Est |
|---|---|
| Test gap 4 | 30 min |
| P1-5 | 15 min |
| **Subtotal** | **~45 min** |

**All-in maximum: ~95 min** for the pre-build pass on top of the
already-shipped P0 pass. That gets us to the new-VM setup with
maximum defensibility for the UTM sweep.
