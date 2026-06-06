# Red-flag cheatsheet

When a `dbtests/` check fails or a number looks wrong, find the symptom in the **left column** below, read the middle column for what it usually means, follow the right column for what to do next.

Rows are roughly grouped by the test that surfaces the symptom. **Higher rows are higher-severity** — anything in the "INV-2 / INV-8 / brain isolation" block must block a merge.

---

## Security-critical (do not merge if these fail)

| Symptom | What it usually means | What to do |
|---|---|---|
| **Step 2** scenario 4 shows `api_key: sk-…` (the raw value) in the audit log instead of `<REDACTED>` | The `_SECRET_KEY_SUBSTRINGS` or `_SECRET_VALUE_PATTERNS` heuristic regressed. P2-F19 escape — secrets in params reach the audit log unprotected. | Open `controller/audit.py`, find `_key_looks_secret` and `_value_looks_secret`. The redaction normalises keys with `_normalise_key` (alnum-only lowercase) before substring match. Run the targeted tests: `pytest controller/tests/test_audit.py::test_secret_key_substrings_redacted -v`. |
| **Step 2** scenario 3 shows `outcome: executed` for `/etc/hosts; rm -rf /` | INV-2 escape — schema validator accepted shell metacharacters in `target`. Catastrophic; metachars will reach mcpd. | Open `schemas/intent.json`. The `target` pattern MUST be `^[^;&|\`$<>\x00-\x1f]*$` — if it's been weakened, restore it. Run `pytest controller/tests/test_intent_schema.py::test_every_shell_metachar_rejected_in_target -v`. |
| **Step 3** rejects any of the 22 mcpd tool names | Schema regex regressed. A real Tier 0/1/2/3 mcpd tool is now permanently unreachable from the Controller. | Open `schemas/intent.json`, find the `action` pattern. Current value is `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`. Confirm the change in `git log -p schemas/intent.json`; revert or widen. |
| **Step 3** accepts any of the 10 bogus action strings (e.g. `Bad.Tool`, `fs.read;rm`, empty string) | Schema regex too loose. A maliciously-crafted action could carry metacharacters into mcpd's dispatcher. | Same file as above; tighten the regex back to a strict pattern. The current shape should accept only `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+`. |
| **Step 4** check 4 shows `FAIL — classifier catalogue is OUT OF SYNC with mcpd` | The classifier's tier sets in `_mcpd_tools.py` no longer match the tools mcpd ships. New tool added without classification, or tool renamed. | On the VM: `python ~/dual-brain/scripts/export_mcpd_catalogue.py emit --mcpd ~/icebreaker/src/mcpd/target/release/mcpd`. `scp` the regenerated `_mcpd_tools.py` back to your dev machine. Commit. |
| **Step 4** check 5 shows audit file mode is `0o644` or looser (group/world-readable) but it should be `0o640` | Acceptable on a single-user dev VM. **NOT acceptable on a shared host** — audit log is meant to be operator-readable only. | Check `umask` on the VM (`umask` command). If it's `0022` you'll get `0o644` after the kernel masks the open() request. Fix: `umask 027` in the shell, then re-run the test. AuditLog's explicit `chmod` to `0o640` runs only on file creation; existing files keep their mode (by design — for log shipping). |
| **Step 4** check 5 shows parent dir is group- or world-writable (`0o755`, `0o775`, `0o777`) | Anyone on the box can drop a file into `~/.local/state/icebreaker/` and pre-fill the audit log path. INV-8 violation. | Check `umask` again. Fix permissions: `chmod 0700 ~/.local/state/icebreaker`. |

---

## Test-runner / setup issues

| Symptom | What it usually means | What to do |
|---|---|---|
| **Step 1** fails with `ModuleNotFoundError: No module named 'jsonschema'` (or `hypothesis`, `pytest`) | Project deps not installed. | Run `pip3 install -r dual-brain/requirements.txt` (or on the VM, `~/dual-brain-venv/bin/pip install -r ~/dual-brain/requirements.txt`). |
| **Step 1** hypothesis tests time out at 180 s | A recent schema regex change made the fuzz space explode. Hypothesis is trying inputs that the validator chokes on. | Re-run with `--quick` to confirm it's the fuzz that's stuck: `bash 01_pytest_local.sh --quick`. If pytest passes without fuzz, the bug is in a regex you recently changed. |
| **Step 1** flake on `test_concurrent_revise_chain_no_lost_updates` (intermittent) | Threading test occasionally times out under load. | Re-run. If it consistently fails, look at `controller/intent_store.py` — the `revise()` body must be one `with self._lock:` block end-to-end. |
| **Step 4** fails at check 1 (`VM status = not-found`) | The VM has been deleted or renamed. | `gcloud compute instances list` to find the current name. Set `VM_NAME=` env var: `VM_NAME=new-instance bash 04_vm_check.sh`. |
| **Step 4** fails at check 1 (`VM status = TERMINATED`) | VM is off. | `gcloud compute instances start instance-20260528-030421 --zone=us-central1-a`. |
| **Step 4** fails at check 2 (`mcpd binary not found at …`) | Phase 1 was never built on this VM, or the VM was reimaged. | SSH in, `cd ~/icebreaker/src/mcpd && cargo build --release`. Re-run the check. |
| **Step 4** fails at check 3 (deps not importable) | The venv at `~/dual-brain-venv` doesn't exist or is stale. | SSH in, `python3 -m venv ~/dual-brain-venv && ~/dual-brain-venv/bin/pip install -r ~/dual-brain/requirements.txt`. |
| **Step 5** (`05_deploy_to_vm.sh`) fails at the scp step | gcloud auth expired, or VM rebooted with a new internal IP. | `gcloud auth login` (or `gcloud auth application-default login`). |

---

## Output looks suspicious but exit code is 0

| Symptom | What it usually means | What to do |
|---|---|---|
| **Step 2** scenario 1 shows `ref_id == intent.intent_id` (same UUID twice) | `intent_store.put()` is returning the candidate's `intent_id` instead of generating a fresh one. This BREAKS the opaque-UUID layer (INV-1) — the PB could predict the ref_id from the QB's output. | Check `controller/intent_store.py` `put()` — it must call `uuid.uuid4()` internally and store under that key, not under `intent['intent_id']`. |
| **Step 4** check 4 reports `OK` but the `mcpd advertises X tools` is not 22 | mcpd added or removed a tool since Phase 1 froze. The classifier may still be in sync (G2 passes) but the user-visible tool count changed. | Update `EXPECTED_TOOL_COUNT` in `scripts/export_mcpd_catalogue.py` and add the new tool to `TOOL_CATEGORIES`. Re-run check 4. |
| **Step 1** prints `375 passed, 23 skipped` on Mac but the user expected 398 | The 23 skipped tests are the mcpd integration tests — they need Linux + a built mcpd binary, which you don't have on Mac. This is correct behaviour. | If you want all 398 to run, use `05_deploy_to_vm.sh` instead. |

---

## What pytest fail summaries usually mean

`pytest` will surface the failing test name. A few common ones and what they're checking:

| Test name fragment | What it asserts | If it fails |
|---|---|---|
| `test_fsync_persists_through_sigkill` | Subprocess writes one audit entry, kills itself with SIGKILL, parent reopens log and sees the entry. Proves `os.fsync` happened before the kill. | Check `controller/audit.py` `write()` — `os.fsync(self._fd)` MUST be called unconditionally after each `os.write`. Don't add an `if self._fsync` branch unless you also gate the test on it. |
| `test_concurrent_revise_chain_no_lost_updates` | 8 threads each do 50 revisions on the same intent UUID. Final store has exactly 1 + 400 entries, no None returns, no exceptions. | `intent_store.revise()` must hold the lock for the full get-merge-put critical section. If it's split across multiple `with self._lock:` blocks, the race is back. |
| `test_every_c0_control_char_rejected_in_target` (32 cases) | Each of the 32 C0 control characters (`\x00`–`\x1f`) inserted into target → schema rejects. | Schema's target pattern is `^[^;&|\`$<>\x00-\x1f]*$`. If any of the 32 leaks through, the pattern's character class is wrong. |
| `test_every_shell_metachar_rejected_in_target` (7 cases) | Each of `; & | \` $ < >` inserted into target → schema rejects. | Same pattern as above; check the literal characters in the denylist. |
| `test_no_phantom_tools_leak_in` | `_mcpd_tools.ALL_TOOLS` does NOT contain `service.status`, `network.firewall_*`, `package.purge`, etc. (the historical drift names). | Open `scripts/export_mcpd_catalogue.py`, check `TOOL_CATEGORIES`. If a phantom name has crept in, remove it. |

---

## When you genuinely don't know

The audit log is your friend. Run `02_cross_module_smoke.py` with `-v` if you add it, OR just `tail -f ~/.local/state/icebreaker/controller-audit.log` while the Controller is running. The log will tell you which `outcome` your action ended up at, with full provenance (tier, reason, risk_level, rejection envelope).

If you've recently changed a regex, an enum, or a heuristic, `git log -p <file>` and walk backwards. The four modules are small enough that "what's different from last week" is usually one commit.

If a security-critical assertion fails (any row in the top block of this document) and you don't immediately know why, stop merging and ask. These are the gates the whitepaper depends on.
