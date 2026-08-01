# Daily Devil's Advocate Report - 2026-08-01

> **Reviewer posture:** Senior Principal Engineer. No praise. Only flaws.
> Reference document `docs/linux_complete_guide.md` was listed as the authoritative best-practices source. **It does not exist.** Every finding below is therefore judged against CLAUDE.md's Architectural Invariants, Engineering Best Practices (BP-1 through BP-13), and the source code itself.

---

### Issue #1: The Referenced "Source of Truth" Document Does Not Exist

* **Category:** Pedagogy
* **Location:** `docs/` (file `linux_complete_guide.md` is absent; confirmed by directory listing)
* **The Flaw:** The scheduled review task explicitly instructs every reviewer to "read the project's source of truth for best practices located at `docs/linux_complete_guide.md`" and to "treat the rules in that document as absolute law." That file does not exist anywhere in the repository. Any automated or human reviewer who follows this instruction will either fail silently (if the read is best-effort) or crash immediately. An onboarding engineer following the documented review workflow hits a dead end on step 1. The existing `CLAUDE.md`, `AI_Native_OS_Whitepaper.md`, and `docs/IMPLEMENTATION_PLAN.md` are the actual reference documents — but none of them are named as the target. This is a process failure, not a minor typo: the whole review pipeline is built on a document that was never written or was deleted without updating the prompt.
* **The Fix:** Either create `docs/linux_complete_guide.md` with the intended Linux-specific best practices (file permissions model, syscall safety, namespace isolation, etc.) or update every caller that references it — including this scheduled task prompt — to point at the correct file (`CLAUDE.md` or a newly consolidated reference). The absence must be tracked as a failing CI gate so it cannot recur.

---

### Issue #2: `$HOME` Fallback to `/` Creates a Filesystem-Wide Security Bypass

* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/tools/fs.rs:57–62` (the `home()` function) and `fs.rs:242–253` (the `is_tier_1_safe()` function)
* **The Flaw:** `home()` is defined as:
  ```rust
  PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/".into()))
  ```
  If `$HOME` is unset — which happens under stripped systemd sandboxes, CI containers, or `ProtectHome=yes` units that clear the environment — every call to `home()` returns `/`. This cascades in two directions simultaneously. First, `/` is pushed into `default_roots()`, meaning `validate("/var/lib/mysql/data")` or `validate("/root/.ssh/id_rsa")` succeeds — any absolute path passes whitelist validation. Second, `is_tier_1_safe()` checks `if v.root != *home()` and returns `true` for *every* path, including `/etc/nginx/nginx.conf` and `/boot/grub/grub.cfg`. These paths would then execute via `safe_write()` without triggering the COW gate — a direct violation of INV-6. The only guard is Landlock, but that is a kernel-level sandbox applied at startup, not a substitution for the userspace path classifier that now classifies `/root/.ssh/authorized_keys` as "Tier 1 safe write."
* **The Fix:** Add an explicit guard: if `$HOME` is empty or equals `/`, `mcpd` must refuse to start (log a fatal error and `std::process::exit(1)`). This is the same pattern used for the Landlock `NotEnforced` case. Add a regression test that constructs `home()` with `HOME` unset and asserts the process exits rather than defaulting to `/`.

---

### Issue #3: Rust `audit.rs` Violates INV-8 in Two Distinct Ways: No `fsync`, No Hash Chain

* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/audit.rs:152–160` (the write path) and the entire file (hash chain is absent)
* **The Flaw:** INV-8 and BP-7 jointly require: (1) every write must be `fsync`'d so a kernel crash does not silently lose entries, and (2) entries must be hash-chained so deletions or edits are detectable. The Python `AuditLog` in `controller/audit.py:504–506` satisfies both: it calls `os.write()` then `os.fsync()` per line, and it stamps `seq` + `prev_hash` on every entry. The Rust `audit.rs` does neither. The write is:
  ```rust
  let _ = guard.write_all(&bytes);
  ```
  No `fsync` call exists anywhere in this file. No sequence number. No hash of the previous entry. A `SIGKILL` between `write_all` and the OS flushing page cache drops the entry permanently. Any attacker or buggy process that truncates `/var/log/mcpd/audit.log` leaves zero forensic evidence. The `let _` also silently discards the write error — if the disk is full, audit logging silently stops. CLAUDE.md says "The audit log MUST NOT be writable by the AI models." Making it undetectably truncatable is equivalent.
* **The Fix:** After `guard.write_all(&bytes)`, call `guard.sync_all()` (or the raw `libc::fsync`) and propagate the error to `tracing::error!` rather than discarding it. Add a sequence counter and chain hash (`sha256(prev_hash || canonical_bytes)`) stored in the `Writer` struct and stamped into each serialized line — matching what `controller/audit.py` already does. Add a differential test (BP-12) that verifies both audit implementations produce chainable, fsync-confirmed output.

---

### Issue #4: 4 KB Audit Truncation in `audit.rs` Produces Systematically Malformed JSON

* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/audit.rs:144–149`
* **The Flaw:** When a serialized audit line exceeds 4000 bytes, the code does:
  ```rust
  bytes.truncate(4000);
  bytes.extend_from_slice(b"...\"}");
  ```
  This truncates at an **arbitrary byte boundary** and then appends the four-byte suffix `...\"}` unconditionally. The resulting bytes are not valid JSON under any standard. If truncation lands inside a string value, the closing `"` of the suffix closes only that string — but the outer object and any parent strings remain unclosed. If truncation lands inside a number or boolean, the appended `"` starts an unquoted string. If the serialized JSON was `{"timestamp":"...","method":"...","params_redacted":{"path":"/very/lon..."}` at byte 4000, the result becomes syntactically broken and `jq`, the audit viewer, or any chain verifier will reject it with a parse error. Every truncated line silently destroys audit integrity for that entry. Given that params can legally be large (long file paths, package names, intent reasons), truncation is not a rare edge case.
* **The Fix:** Either enforce a hard 4 KB budget during *construction* (truncate the `params_redacted` field specifically before serializing, never the serialized output), or record a fixed-length truncation sentinel as a separate top-level field (`"truncated": true, "original_size": N`) and emit a structurally complete JSON object. Never truncate the byte stream of a serialized JSON object.

---

### Issue #5: HITL `EXPLAIN` Loop Restarts Full Timeout on Every `?` Press, Defeating BP-4

* **Category:** Code/Architecture
* **Location:** `dual-brain/controller/hitl.py:511–514` (the `while decision == Decision.EXPLAIN` loop)
* **The Flaw:** After showing the HITL prompt, the code runs:
  ```python
  while decision == Decision.EXPLAIN:
      decision = self._presenter.read_decision(self._timeout_seconds)
  ```
  `self._timeout_seconds` defaults to 300 seconds (5 minutes). Each time the user presses `?` (Help/EXPLAIN), `read_decision()` is called with the **original full timeout**, not the remaining time since the prompt was first shown. `_read_loop` in `TerminalPresenter` resets its own `deadline = time.monotonic() + timeout_seconds` (line 265) on each call. A user who presses `?` once per 299 seconds can keep the HITL gate open indefinitely — no session-level clock governs the total elapsed time. BP-4 is unambiguous: "enforce the approval lockout on a monotonic clock." The code violates this for the EXPLAIN case. A confused or malicious operator (or a UI automation bug) could keep a Tier 3 destructive operation in a pending-approval state forever, never receiving a `TIMEOUT → DENY` outcome.
* **The Fix:** Record `prompt_shown_at = time.monotonic()` (already done at line 457) and pass `remaining_time = self._timeout_seconds - (time.monotonic() - prompt_shown_at)` to every `read_decision()` call, including inside the EXPLAIN loop. If `remaining_time <= 0`, immediately return `Decision.TIMEOUT` without calling `read_decision()`.

---

### Issue #6: TOCTOU Double `realpath` in `risk_classifier.py` Can Hide the Critical Path Match

* **Category:** Code/Architecture
* **Location:** `dual-brain/controller/risk_classifier.py:142–149`
* **The Flaw:** The classification path is:
  ```python
  if _target_is_critical(target):          # first realpath() call here
      match = _CRITICAL_PATHS.search(os.path.realpath(target) if target else "")
      return ClassificationResult(
          ...
          blocked_pattern=match.group(0) if match else None,
      )
  ```
  `_target_is_critical()` calls `os.path.realpath(target)` internally (line 105). The outer scope then calls `os.path.realpath(target)` **again** on line 143. Between these two calls, a symlink at `target` can be swapped to point at a different path. The second `realpath` resolves to a benign path; `_CRITICAL_PATHS.search()` returns `None`; `blocked_pattern` is recorded as `None` in the audit log and in the `ClassificationResult`. The tier classification still returns `Tier.HIGH` (because the first call returned `True`), so execution is blocked — but the forensic record of *which* critical pattern was matched is silently erased. In a follow-up MODIFY cycle where the user revises the intent, the downstream code that might enforce per-pattern policies has no `blocked_pattern` to dispatch on.
* **The Fix:** Call `os.path.realpath(target)` exactly once, assign to a local variable, pass it to both `_target_is_critical()` (refactored to accept a pre-resolved path) and the subsequent `_CRITICAL_PATHS.search()`. Eliminate the duplicated I/O syscall and the race window with a single change.

---

### Issue #7: `intent_store.revise()` Leaks the Stale Original Intent for Up to 5 Minutes

* **Category:** Code/Architecture
* **Location:** `dual-brain/controller/intent_store.py:79–109` (the `revise()` method)
* **The Flaw:** When a HITL MODIFY cycle runs, `revise()` creates a **new** `IntentEntry` for the corrected intent but **never deletes the original entry**. Both the old `original_ref_id` and the new `new_ref_id` remain live in `_store` for up to `TTL_SECONDS` (300 seconds). Any component that retained the original ID can call `store.get(original_ref_id)` and retrieve the pre-modification intent — the one the user explicitly rejected or asked to change. INV-1 says the Intent Object must be tightly controlled; having a superseded version silently remain accessible for 5 minutes is a direct weakening of that guarantee. In a future multi-turn agentic loop (Phase 8), a subagent that received the original ID before the modification would be able to act on the obsolete and user-rejected version.
* **The Fix:** In `revise()`, after creating the new entry, immediately delete the original: `del self._store[original_ref_id]`. The `_get_unlocked → merge → validate → store` sequence already runs under a single lock acquisition (as the comment on line 93 notes), so adding the delete is atomic. Add a test that confirms `store.get(original_ref_id)` returns `None` immediately after a successful `revise()`.

---

### Issue #8: Landlock Silently Continues When Critical Sandbox Paths Are Skipped

* **Category:** Conflicts
* **Location:** `src/mcpd/src/sandbox/landlock.rs:53–73` (the `warn!` + `continue` pattern on path open failure)
* **The Flaw:** INV-5 states: "If Landlock is unavailable, mcpd MUST exit with a clear error — never run without it." CLAUDE.md enforces this for the `RulesetStatus::NotEnforced` case (line 107: `bail!(...)`). However, for the **per-path** case, the code silently omits any path that `PathFd::new(p)` fails to open:
  ```rust
  Err(e) => warn!("landlock: skipping read-only root '{}': {}", p, e),
  ```
  This means if `/proc` doesn't exist (container environment), or `/etc` is a bind-mount that isn't opened before sandboxing, the corresponding Landlock rules are silently dropped. The kernel enforces only the rules that were successfully added. A Landlock ruleset missing `/proc` permits mcpd to read arbitrary process metadata or `/proc/sysrq-trigger`. A ruleset missing `/etc` silently permits reads of `/etc/shadow`. The daemon returns `RulesetStatus::FullyEnforced` because the rule set *was* fully applied — but it was applied against a reduced and insecure rule set. INV-5's spirit is "fail safe," but the per-path degradation is fail-open.
* **The Fix:** For the static roots that must be present on any production Linux system (`/proc`, `/sys`, `/etc`, `/usr`, `/lib`, `/var/log`), treat `PathFd::new()` failure as fatal: `bail!("landlock: required root '{}' unavailable: {}", p, e)`. Reserve the soft `warn!+continue` behavior only for optional or user-configured extra roots (`MCPD_FS_READ_ROOTS` and `$HOME`). Document this distinction in the function comment.

---

### Issue #9: `SENSITIVE_HOME_SUBDIRS` Is Missing Credentials Files That `fs.write` Will Overwrite Without HITL

* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/tools/fs.rs:41–43`
* **The Flaw:** The current denylist for Tier 1 auto-write is:
  ```rust
  const SENSITIVE_HOME_SUBDIRS: &[&str] = &[
      ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", ".config/secrets",
  ];
  ```
  Absent from this list: `.bash_history`, `.zsh_history`, `.fish_history`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `.config/gcloud`, `.config/gh`, `.env` (at home level). Shell history files accumulate API keys, passwords, and tokens typed directly into the terminal over the life of a machine. A model-generated `fs.write` to `/home/user/.bash_history` with a crafted payload would execute at Tier 1 (auto-execute + audit log, no HITL prompt) because `.bash_history` is not in `SENSITIVE_HOME_SUBDIRS`. An attacker who can influence QB output could silently inject history entries that run on the next shell session. `.netrc` holds plaintext FTP/HTTP credentials. `.git-credentials` holds GitHub tokens. `.npmrc` holds npm auth tokens. None are protected.
* **The Fix:** Extend `SENSITIVE_HOME_SUBDIRS` to include at minimum: `.bash_history`, `.zsh_history`, `.fish_history`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `.config/gcloud`, `.config/gh`. Add a unit test that iterates a corpus of known credential filenames and asserts `is_tier_1_safe()` returns `false` for each. Pin this corpus as a CI gate so future tool additions can't regress it.

---

### Issue #10: `CLAUDE.md` and `GROUND_TRUTH.md` Describe v6.9 as Two Different Features with Contradictory Completion States

* **Category:** Conflicts
* **Location:** `CLAUDE.md` Phase Status table (the `v6.9 Scope O` row) vs. `incremental/GROUND_TRUTH.md` § 1 Status Board (the `V6.9` row)
* **The Flaw:** `CLAUDE.md` states:
  > "v6.9 Scope O (feat/v6.9-scope-o) | **Complete offline; UTM-unverified** | Architectural pivot: per-tool cost dropped ... Layer 1 — controller/tool_catalogue.yaml becomes the single source of truth..."

  `GROUND_TRUTH.md` § 1 states:
  > "| V6.9 | Phase 7 M7.3: Playwright MCP for browser automation | **PENDING**"

  These describe **entirely different features** assigned the **same version number**, with **opposite completion states**. `GROUND_TRUTH.md` begins with: *"If code, chat history, or memory disagrees with this file — this file wins."* `CLAUDE.md` begins with the instruction to read it as the architecture source of truth. Two documents that each claim to be the single source of truth, that flatly contradict one another on a concrete factual question (what is v6.9 and is it done?), means no automated process or human engineer can determine the actual project state without a manual audit. BP-13 states that the same error recurring in a subsequent build is a process failure — this conflict has existed across at least the Phase 7 planning horizon and has not been resolved. R2 of GROUND_TRUTH.md says "Never write code for Vn+1 while Vn is RED." If no one knows what V6.9 actually is, R2 cannot be enforced.
* **The Fix:** Designate exactly one document as the authoritative version ledger and eliminate or redirect the other. Concretely: `GROUND_TRUTH.md` § 1 must be updated to match the actual codebase state (v6.9 = Scope O, Complete offline). Add a CI gate (`scripts/check_version_ledger.py`) that parses both documents, extracts version rows, and fails if they contradict one another — the next version table drift is caught at PR time, not at the next UTM boot.
