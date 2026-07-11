# Control Center — Live Guest Verification Checklist

**Phase 6 Scope D**. This is the operator-facing edge-case matrix. Automated smoke tests
in `gui/control/pages/tests/test_pages_smoke.py` cover the state-transition mechanics;
this document covers the things a human must observe on a running guest before the
`v1.0-rc1` ISO ships.

**Rule for the sweep**: every red cell becomes an F-xx entry in
`incremental/GROUND_TRUTH.md`, gets fixed, and the sweep restarts. Loop until every
row is green.

---

## Preconditions before starting the sweep

Run these on the guest before opening Control Center for the first time. Each must
return the shown result — if any preconditions fail, the sweep is invalid until the
underlying setup bug is fixed.

| Check | Command | Expected |
|---|---|---|
| Daemon service present | `systemctl status icebreaker-controller.service` | listed unit, "loaded" |
| Daemon socket exists | `ls -l /run/icebreaker/controller.sock` | 0660 icebreaker:icebreaker-users |
| Current user is in `icebreaker-users` | `id -Gn "$USER" \| tr ' ' '\n' \| grep icebreaker-users` | matches |
| System config parses | `python3 -c 'import tomllib; tomllib.load(open("/etc/icebreaker/controller.toml","rb"))'` | no exception |
| Catalogue parses | `python3 -c 'import tomllib; tomllib.load(open("/usr/share/icebreaker/catalogue.toml","rb"))'` | no exception |
| `pkexec` present | `command -v pkexec` | `/usr/bin/pkexec` |
| polkit rules for icebreaker | `pkaction --action-id org.freedesktop.systemd1.manage-units` | policy loaded |
| Schema JSON present | `ls /usr/share/icebreaker/schemas/*.json \| wc -l` | ≥ 1 |

If any of the above fails, do NOT proceed — file the failure as a packaging bug and
fix it in `cx-distro/` before running the sweep again.

---

## Per-page checklist

Each row below is one guest-side action + expected result. The three columns after the
result are: user has no API keys / user has some API keys / user has all API keys — a
different guest state can hide bugs the others surface.

### Behavior page

| # | Action | Expected result | Fresh | Some keys | All keys |
|---|---|---|---|---|---|
| B1 | Open Control Center → Behavior | Page renders in < 500 ms; no banner | ☐ | ☐ | ☐ |
| B2 | Read the four verifier voting rows | Votes=1, Require=0, Parallel=on, QB retries=3 | ☐ | ☐ | ☐ |
| B3 | Set Votes=1 Require=2 | Validator banner appears; Apply button disabled | ☐ | ☐ | ☐ |
| B4 | Toggle Parallel off with Votes=3 Require=3 | Banner appears (unanimous sequential = always reject) | ☐ | ☐ | ☐ |
| B5 | Restore Votes=3 Require=2 (Parallel=on) | Banner disappears; Apply enabled | ☐ | ☐ | ☐ |
| B6 | Click Apply & Restart | polkit prompt appears; on success, "Restarted" flash | ☐ | ☐ | ☐ |
| B7 | Cancel polkit prompt | Clean error message; no crash; config unchanged | ☐ | ☐ | ☐ |
| B8 | Pick each retry mode radio | Selection saves immediately; `retry_mode` in TOML matches | ☐ | ☐ | ☐ |
| B9 | Toggle a fallback backend switch | Change reflected in `qb.fallback_chain` after Apply | ☐ | ☐ | ☐ |
| B10 | Toggle auto-cancel on cost breach | Value persists across restart | ☐ | ☐ | ☐ |

### Limits page

| # | Action | Expected result | Fresh | Some keys | All keys |
|---|---|---|---|---|---|
| L1 | Open Limits page | Renders in < 500 ms; no banner | ☐ | ☐ | ☐ |
| L2 | Read HITL lockout row | Value is 3 (INV-6 floor); spinner min = 3 | ☐ | ☐ | ☐ |
| L3 | Try to type 2 into HITL lockout | Not accepted (Adw.SpinRow clamps at 3) | ☐ | ☐ | ☐ |
| L4 | Set `max_shell_context_chars` = 2048 | Value saves; badge shows "hot-reload" | ☐ | ☐ | ☐ |
| L5 | Set `max_recent_commands` = 20 | Value saves; badge shows "hot-reload" | ☐ | ☐ | ☐ |
| L6 | Set session ceiling USD = 5.00 | Value saves as float, not int | ☐ | ☐ | ☐ |
| L7 | Try to set warn_fraction = 1.5 | Not accepted (schema max = 1.0) | ☐ | ☐ | ☐ |
| L8 | Restart controller, re-open Limits | All values persist | ☐ | ☐ | ☐ |

### Models page

| # | Action | Expected result | Fresh | Some keys | All keys |
|---|---|---|---|---|---|
| M1 | Open Models page | Renders in < 500 ms; preset badges start as "checking…" | ☐ | ☐ | ☐ |
| M2 | Wait 10 s | Every preset row shows a definite badge (not indefinitely "checking") | ☐ | ☐ | ☐ |
| M3 | Preset badges for backends with a key | Green ✓ or red ✗ (never yellow/grey) | – | ☐ | ☐ |
| M4 | Preset badges for backends without a key | Grey "no key" | ☐ | – | – |
| M5 | Hover a red or grey badge | Tooltip shows provider's error message | – | ☐ | ☐ |
| M6 | Click "Verify now" | Badges reset to "checking"; new results within 15 s | – | ☐ | ☐ |
| M7 | Swap backend Gemini → Anthropic | Model dropdown refreshes; custom text if any is saved under Gemini | ☐ | ☐ | ☐ |
| M8 | Type custom Gemini model, swap to Anthropic, swap back | Custom text restored | ☐ | ☐ | ☐ |
| M9 | Read "On apply" summary | Shows exact `[qb].backend = "..."` and `[qb.<x>].model = "..."` | ☐ | ☐ | ☐ |
| M10 | Expand "Advanced PB endpoint" | Fields pre-filled from current config | ☐ | ☐ | ☐ |
| M11 | Set `pb_transport` = unix, `pb_endpoint` = `unix:///run/icebreaker/pbd.sock` | Saves and restarts cleanly | ☐ | ☐ | ☐ |
| M12 | Set QB max tokens to 100000 | Value saves; validates against schema max | ☐ | ☐ | ☐ |
| M13 | Set QB timeout = 8000 | Reject (schema max = 7200) | ☐ | ☐ | ☐ |
| M14 | Airplane mode: Verify now | Badges show "network" (grey), not "not found" (red) | – | ☐ | ☐ |
| M15 | Wrong key: Verify now | Badges show "auth failed" (yellow), not "not found" (red) | – | ☐ | – |
| M16 | Add a bogus preset ID via TOML overlay | Row appears in Models with "not found" (red) | ☐ | ☐ | ☐ |

### Keys page

| # | Action | Expected result | Fresh | Some keys | All keys |
|---|---|---|---|---|---|
| K1 | Open Keys page | Renders; three rows (Gemini / Anthropic / OpenAI) | ☐ | ☐ | ☐ |
| K2 | Row for a not-configured key | Shows "Not set"; Set button visible | ☐ | – | – |
| K3 | Row for a configured key | Shows masked preview like `sk-…5v6`; Update button visible | – | ☐ | ☐ |
| K4 | Click Set / Update | polkit prompt; on success, row updates | ☐ | ☐ | ☐ |
| K5 | Cancel polkit | Clean error; no partial write | ☐ | ☐ | ☐ |
| K6 | Enter empty key | Rejected client-side; no submission | ☐ | ☐ | ☐ |
| K7 | After successful save | Restart daemon; verify env var reaches new daemon process | ☐ | ☐ | ☐ |

### Status page

| # | Action | Expected result |
|---|---|---|
| S1 | Open Status page | 3 service rows, 2 socket rows, 3 key rows |
| S2 | Daemon running | Controller = green ● |
| S3 | Stop daemon: `systemctl stop icebreaker-controller`; refresh page | Controller = red ✗ within 5 s |
| S4 | Start daemon | Controller = green within 5 s |
| S5 | pbd not up | PB llama-server row = red "missing" |

### Errors page

| # | Action | Expected result |
|---|---|---|
| E1 | Open Errors page | Renders even if `/var/log/icebreaker/system.jsonl` doesn't exist |
| E2 | Existing errors in log | Rows displayed in reverse chronological order |
| E3 | Rows > 50 | Only most-recent 50 shown; header indicates truncation |
| E4 | Toggle verbose errors | Setting persists; next turn's error reason shows traceback |
| E5 | Click Copy All | Clipboard gets JSON-shaped export |
| E6 | Corrupt log line | Skipped without crashing; header notes skipped count |

### Tools page

| # | Action | Expected result |
|---|---|---|
| T1 | Open Tools page | Renders 22 tool cards (or fewer if schema dir empty) |
| T2 | Each card | Shows name / description / tier badge / reversible / requires-HITL |
| T3 | No schemas installed | Empty state message, not crash |

### Theme page

| # | Action | Expected result |
|---|---|---|
| Th1 | Open Theme page | Available themes listed |
| Th2 | Pick a theme | Palette flips immediately; every page in Control Center recolors |
| Th3 | Restart Control Center | Selection persists |

---

## System-level edge cases (post-per-page)

| # | Action | Expected result |
|---|---|---|
| X1 | Corrupt user config: `echo "not toml [ " > ~/.config/icebreaker/controller.toml` | Every page renders with a red banner naming the file |
| X2 | Fix the corrupt file → reload | Banners disappear |
| X3 | Set config file 0000 perms | Pages render with a "permission denied" banner |
| X4 | Kill daemon mid-turn | Terminal shows reconnect banner; new turn works |
| X5 | Two Control Center windows open simultaneously | Both refresh; last-writer wins on Apply |
| X6 | Concurrent config write (Control Center + `nano` at same time) | Atomic write means no truncation; last writer wins |
| X7 | `pkexec` binary missing (`rm /usr/bin/pkexec`) | Apply shows "pkexec not available" (not crash) |
| X8 | User not in `icebreaker-users` group | Daemon connect shows "permission denied" banner |
| X9 | Legacy v6.65 controller.toml (no verifier / no new keys) | Every page renders; new fields fall back to defaults |
| X10 | Downgrade rollback: shipped v1.0-rc1 config loaded by hypothetical v6.65 daemon | Loads without rejecting unknown fields (top-level `additionalProperties: true`) |

---

## Recording results

For each row, mark:
- ☐ → not yet tested
- ✓ → passed on the guest
- ✗ → failed; `Fxx-<short>` entry filed in `GROUND_TRUTH.md`

The sweep is done when every row is ✓ across every column that applies. Any ✗ blocks
the `v1.0-rc1` ISO build.

---

## Automated coverage of this checklist

Rows marked B3–B5, M7–M9, X1, X6, X7 have direct automated coverage in
`test_pages_smoke.py` and `test_config_io_hardening.py`. The rest depend on a running
guest, systemd, polkit, and real API keys — those cannot be tested in CI and require
the manual sweep to sign off.
