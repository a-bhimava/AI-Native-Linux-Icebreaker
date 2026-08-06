"""Format an mcpd COW dry-run diff for the Tier-3 HITL modal.

M7.0.1d (v6.16, 2026-08-03) — closes audit rows B-2 (Tier-3 gate carries
no diff) and C-1 (COW stub) at the presentation layer.

Input shape (from mcpd's ticket `preview.diff`, per M7.0.1a's DryRunDiff):
    {
      "operation":              "fs.delete" | "fs.write" | "package.*",
      "bytes_delta":            int (signed),
      "file_count_delta":       int (signed),
      "affected_paths_sample":  [str, ...] (capped at 20),
      "human_summary":          str (mcpd's own phrasing, verbatim),
      "risk":                   "LOW" | "MED" | "HIGH",
      "reversible":             bool
    }

Output shape (whitepaper §8.2 + terraform-plan compact summary):

    3.2 GB will be freed. 847 files will be deleted.
    Change: 847 to delete, 3.2 GB freed.
    Risk: LOW — irreversible.

    Affected (sample):
      - /var/cache/apt/archives/foo.deb
      - /var/cache/apt/archives/bar.deb
      - ... (827 more)

All strings coming out of mcpd are sanitized (ANSI/C0/C1 stripped, CRLF
neutralized, per BP-3) — mcpd is trusted, but defense-in-depth guards
against an intent object that made it past QB validation carrying
adversarial payload text.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Match ANSI CSI escapes + C0/C1 control chars (except \n and \t).
# Mirror of hitl._sanitize_display / audit._C0_C1_RE — kept local to
# avoid a load-order dependency.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_C0_C1 = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _sanitize(s: Any) -> str:
    """Strip ANSI escapes + C0/C1 controls + neutralize CRLF. Never raises."""
    if not isinstance(s, str):
        return ""
    s = _ANSI_CSI.sub("", s)
    # Normalize CRLF BEFORE the C0/C1 strip — the range 0x0b-0x1f includes
    # \r (0x0d), so stripping first would erase the CR before we can
    # promote it to LF.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _C0_C1.sub("", s)
    return s


def _human_bytes(n: int) -> str:
    """Format an unsigned byte count using KB/MB/GB/TB units.

    Mirrors mcpd's src/mcpd/src/tools/cow.rs::human_bytes so the compact
    summary line matches the human_summary line's units.
    """
    if n < 0:
        n = -n
    kb = 1024
    mb = 1024 * kb
    gb = 1024 * mb
    tb = 1024 * gb
    if n >= tb:
        return f"{n / tb:.1f} TB"
    if n >= gb:
        return f"{n / gb:.1f} GB"
    if n >= mb:
        return f"{n / mb:.1f} MB"
    if n >= kb:
        return f"{n / kb:.1f} KB"
    if n == 1:
        return "1 byte"
    return f"{n} bytes"


def _reversibility_note(diff: dict) -> str:
    """One-word reversibility hint for the Risk line."""
    return "reversible" if diff.get("reversible") else "irreversible"


def _compact_summary(diff: dict) -> str:
    """Terraform-plan-style compact summary: 'Change: N to delete, X freed.'"""
    op = diff.get("operation", "")
    bytes_delta = int(diff.get("bytes_delta", 0) or 0)
    file_count_delta = int(diff.get("file_count_delta", 0) or 0)
    parts: list[str] = []

    if op == "fs.delete":
        n = abs(file_count_delta)
        parts.append(f"{n} to delete")
        if bytes_delta < 0:
            parts.append(f"{_human_bytes(bytes_delta)} freed")
    elif op == "fs.write":
        if file_count_delta > 0:
            parts.append(f"{file_count_delta} to add")
        else:
            parts.append("1 to modify")
        if bytes_delta > 0:
            parts.append(f"{_human_bytes(bytes_delta)} added")
        elif bytes_delta < 0:
            parts.append(f"{_human_bytes(bytes_delta)} freed")
    elif op.startswith("package."):
        # For package ops, file_count_delta = affected package count.
        # bytes_delta is often 0 (mcpd's apt-get -s -qq parser doesn't
        # extract sizes reliably) — skip the size half if so.
        n = abs(file_count_delta)
        if op == "package.install":
            verb = "to install"
        elif op == "package.remove":
            verb = "to remove"
        else:  # package.upgrade
            verb = "to upgrade"
        parts.append(f"{n} {verb}")
        if bytes_delta != 0:
            parts.append(f"{_human_bytes(bytes_delta)} added")

    if not parts:
        return ""
    return "Change: " + ", ".join(parts) + "."


def _affected_sample_block(diff: dict) -> Optional[str]:
    """Bullet list of the first 20 affected paths + '(N more)' truncation."""
    sample = diff.get("affected_paths_sample") or []
    if not isinstance(sample, list) or not sample:
        return None
    file_count_delta = int(diff.get("file_count_delta", 0) or 0)
    total = abs(file_count_delta)
    shown = [_sanitize(p) for p in sample if isinstance(p, str)]
    if not shown:
        return None
    lines = ["Affected (sample):"]
    for path in shown:
        lines.append(f"  - {path}")
    if total > len(shown):
        lines.append(f"  - ... ({total - len(shown)} more)")
    return "\n".join(lines)


def format_diff(diff: Optional[dict]) -> Optional[str]:
    """Turn an mcpd `preview.diff` dict into HITL-modal text.

    Returns None if the diff is missing/empty so callers can distinguish
    "no diff to show" from "diff shown but empty". The whitepaper §8.2
    shape is preserved: line 1 = mcpd's human_summary verbatim, line 2 =
    terraform-plan compact summary, line 3 = Risk + reversibility, then
    an optional Affected block.
    """
    if not isinstance(diff, dict):
        return None
    human = _sanitize(diff.get("human_summary", ""))
    if not human:
        # Even without human_summary, the compact summary can carry the
        # user's decision-relevant info — don't collapse to None yet.
        pass
    compact = _compact_summary(diff)
    risk = _sanitize(diff.get("risk", "?")).upper() or "?"
    risk_line = f"Risk: {risk} — {_reversibility_note(diff)}."
    affected = _affected_sample_block(diff)

    blocks: list[str] = []
    if human:
        blocks.append(human)
    if compact:
        blocks.append(compact)
    blocks.append(risk_line)
    if affected:
        blocks.append(affected)

    if not blocks:
        return None
    return "\n".join(blocks)
