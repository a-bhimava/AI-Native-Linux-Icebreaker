"""`ib-trust` — CLI to inspect + manage the GUI trust store.

Discoverable single-command entry point. `ib-trust` with no args
prints help + current active grants at a glance (no-op-safe browse
before the user has to type anything specific).

Subcommands:
  ib-trust list                          # show active grants (colored table on TTY)
  ib-trust list --all                    # include expired
  ib-trust list --json                   # machine-readable
  ib-trust add <app> <tool> [--ttl SEC] [--tier once|session|persistent|deny]
  ib-trust revoke <app> <tool>
  ib-trust undo                          # revert last add/revoke this session
  ib-trust why <app> <tool>              # explain the current decision + matching grant
  ib-trust defaults                      # print the loaded defaults
  ib-trust export [PATH]                 # dump user grants to stdout or PATH
  ib-trust import PATH                   # merge grants from PATH (dry-run friendly)

UX inspired by `git`, `kubectl`, `gh` — verbs that read like sentences,
colored tables that degrade to plain on pipes, `--dry-run` on every
mutation, `undo` for the "oh no I revoked the wrong thing" case.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from gui_agent.trust_store import (
    TIER_DENY,
    TIER_ONCE,
    TIER_PERSISTENT,
    TIER_SESSION,
    TrustError,
    TrustGrant,
    TrustStore,
)


# ── Small color helpers (no dep on rich/colorama — stdlib only) ────────

def _color_supported() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _color_supported():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:  return _c(t, "32")
def _red(t: str) -> str:    return _c(t, "31")
def _yellow(t: str) -> str: return _c(t, "33")
def _cyan(t: str) -> str:   return _c(t, "36")
def _gray(t: str) -> str:   return _c(t, "90")
def _bold(t: str) -> str:   return _c(t, "1")


# ── Fuzzy match — help users type "click" and get "gui.grounded_click" ─

_TOOL_ALIASES: dict[str, str] = {
    # short → canonical
    "click":       "gui.grounded_click",
    "type":        "gui.grounded_type",
    "drag":        "gui.grounded_drag",
    "scroll":      "gui.grounded_scroll",
    "hover":       "gui.hover",
    "screenshot":  "gui.screenshot",
    "parse":       "gui.parse_screen",
    "key":         "gui.press_key",
    "keys":        "gui.key_sequence",
}


def _fuzzy_tool(user_input: str) -> str:
    """Best-effort canonicalization for the CLI. If the input is
    already qualified (contains `.` or `*`), return as-is."""
    if "." in user_input or "*" in user_input:
        return user_input
    return _TOOL_ALIASES.get(user_input.lower(), user_input)


# ── Table rendering ────────────────────────────────────────────────────

def _grant_row(g: TrustGrant, now: float, session_id: str) -> list[str]:
    if g.tier == TIER_DENY:
        state_str = _red("DENY")
    elif not g.is_active(now, session_id):
        state_str = _gray("expired")
    else:
        state_str = _green("active")
    ttl_str = "—"
    if g.tier == TIER_PERSISTENT and g.expires_at > 0:
        remaining = int(g.expires_at - now)
        if remaining > 0:
            ttl_str = _human_seconds(remaining)
        else:
            ttl_str = _gray("expired")
    elif g.tier == TIER_SESSION:
        ttl_str = _cyan(f"session {g.session_id[:12]}")
    elif g.tier == TIER_ONCE:
        ttl_str = _yellow("once")
    return [state_str, g.app, g.tool, g.tier, ttl_str, g.granted_by,
            (g.reason or "")[:40]]


def _human_seconds(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d"


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    if not rows:
        print(_gray("(no grants)"))
        return
    # Strip ANSI codes for width calc so colored strings don't inflate.
    import re
    ansi_re = re.compile(r"\033\[\d+m")
    def _plain(s: str) -> str:
        return ansi_re.sub("", s)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(_plain(str(cell))))
    fmt_head = "  ".join(_bold(h.ljust(widths[i])) for i, h in enumerate(headers))
    print(fmt_head)
    print(_gray("  ".join("─" * widths[i] for i in range(len(headers)))))
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            pad = widths[i] - len(_plain(str(cell)))
            cells.append(str(cell) + " " * pad)
        print("  ".join(cells))


# ── Undo ring — process-local ─────────────────────────────────────────

_UNDO_STATE_PATH = Path.home() / ".cache" / "icebreaker" / "ib_trust_undo.json"
_UNDO_MAX = 16


def _push_undo(entry: dict) -> None:
    try:
        _UNDO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stack: list[dict] = []
        if _UNDO_STATE_PATH.exists():
            with contextlib_suppress(json.JSONDecodeError):
                stack = json.loads(_UNDO_STATE_PATH.read_text() or "[]")
        stack.append(entry)
        stack = stack[-_UNDO_MAX:]
        _UNDO_STATE_PATH.write_text(json.dumps(stack))
    except OSError:
        pass   # Undo is a nice-to-have; failure isn't fatal.


def _pop_undo() -> Optional[dict]:
    if not _UNDO_STATE_PATH.exists():
        return None
    try:
        stack = json.loads(_UNDO_STATE_PATH.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return None
    if not stack:
        return None
    entry = stack.pop()
    _UNDO_STATE_PATH.write_text(json.dumps(stack))
    return entry


def contextlib_suppress(*exc_types):
    import contextlib
    return contextlib.suppress(*exc_types)


# ── Subcommand implementations ────────────────────────────────────────

def cmd_list(args: argparse.Namespace, store: TrustStore) -> int:
    grants = store.list_all() if args.all else store.list_active()
    if args.json:
        out = [{"app": g.app, "tool": g.tool, "tier": g.tier,
                "expires_at": g.expires_at, "session_id": g.session_id,
                "granted_by": g.granted_by, "reason": g.reason,
                "granted_at": g.granted_at,
                "active": g.is_active(time.time(), store.session_id)}
               for g in grants]
        print(json.dumps(out, indent=2))
        return 0

    now = time.time()
    rows = [_grant_row(g, now, store.session_id) for g in grants]
    print(_bold(f"Trust store: {store._path}"))   # noqa: SLF001
    print(_gray(f"session_id: {store.session_id}"))
    print()
    _print_table(rows, headers=["state", "app", "tool", "tier",
                                "ttl", "granted_by", "reason"])
    return 0


def cmd_add(args: argparse.Namespace, store: TrustStore) -> int:
    tool = _fuzzy_tool(args.tool)
    if args.dry_run:
        print(_yellow(
            f"[dry-run] would grant {args.app} + {tool} "
            f"tier={args.tier} ttl={args.ttl}s reason={args.reason!r}"
        ))
        return 0
    try:
        grant = store.grant(args.app, tool, tier=args.tier,
                            ttl_seconds=args.ttl,
                            granted_by="user", reason=args.reason)
    except TrustError as exc:
        print(_red(f"error: {exc}"), file=sys.stderr)
        return 2
    _push_undo({"action": "grant", "app": grant.app, "tool": grant.tool,
                "tier": grant.tier})
    print(_green(f"✓ granted {grant.app} + {grant.tool} tier={grant.tier}"
                 + (f" ttl={_human_seconds(int(grant.expires_at - time.time()))}"
                    if grant.expires_at > 0 else "")))
    return 0


def cmd_revoke(args: argparse.Namespace, store: TrustStore) -> int:
    tool = _fuzzy_tool(args.tool)
    if args.dry_run:
        matching = [g for g in store.list_active()
                    if g.app == args.app.lower() and g.tool == tool.lower()
                    and g.tier != TIER_DENY]
        print(_yellow(f"[dry-run] would revoke {len(matching)} grant(s)"))
        for g in matching:
            print(f"  - {g.tier} {g.app} + {g.tool} ({g.reason or 'no reason'})")
        return 0
    try:
        removed = store.revoke(args.app, tool)
    except TrustError as exc:
        print(_red(f"error: {exc}"), file=sys.stderr)
        return 2
    if removed:
        _push_undo({"action": "revoke", "app": args.app.lower(),
                    "tool": tool.lower(), "count": removed})
        print(_green(f"✓ revoked {removed} grant(s) for {args.app} + {tool}"))
    else:
        print(_yellow(f"nothing to revoke: no active grants for {args.app} + {tool}"))
    return 0


def cmd_undo(args: argparse.Namespace, store: TrustStore) -> int:
    entry = _pop_undo()
    if entry is None:
        print(_yellow("nothing to undo"))
        return 0
    if entry["action"] == "grant":
        store.revoke(entry["app"], entry["tool"])
        print(_green(f"↩ undid grant of {entry['app']} + {entry['tool']}"))
    elif entry["action"] == "revoke":
        # Can't restore the exact original tier/reason without a full snapshot;
        # explain and prompt the user to re-grant explicitly.
        print(_yellow(
            f"cannot auto-restore {entry['count']} revoked grant(s) for "
            f"{entry['app']} + {entry['tool']} — original tier and TTL "
            f"are lost. Re-grant explicitly with `ib-trust add`."
        ))
        return 1
    return 0


def cmd_why(args: argparse.Namespace, store: TrustStore) -> int:
    tool = _fuzzy_tool(args.tool)
    d = store.check(args.app, tool)
    verdict = _green("ALLOWED") if d.allowed else _red("DENIED")
    print(f"{verdict}  {args.app} + {tool}")
    print(f"  reason: {d.reason}")
    if d.matched_grant is not None:
        g = d.matched_grant
        print(f"  matched grant:")
        print(f"    app={g.app!r} tool={g.tool!r} tier={g.tier}")
        print(f"    granted_by={g.granted_by} at "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(g.granted_at))}")
        if g.expires_at:
            print(f"    expires_at="
                  f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(g.expires_at))}")
        if g.reason:
            print(f"    reason: {g.reason!r}")
    return 0 if d.allowed else 1


def cmd_defaults(args: argparse.Namespace, store: TrustStore) -> int:
    defaults = [g for g in store.list_all() if g.granted_by == "defaults"]
    now = time.time()
    rows = [_grant_row(g, now, store.session_id) for g in defaults]
    print(_bold(f"Defaults from: {store._defaults_dir}"))   # noqa: SLF001
    print()
    _print_table(rows, headers=["state", "app", "tool", "tier",
                                "ttl", "granted_by", "reason"])
    return 0


def cmd_export(args: argparse.Namespace, store: TrustStore) -> int:
    """Dump user-granted (not defaults) entries as JSONL."""
    user_grants = [g for g in store.list_all() if g.granted_by == "user"]
    lines = []
    for g in user_grants:
        d: dict = {"app": g.app, "tool": g.tool, "tier": g.tier}
        if g.expires_at:
            d["expires_at"] = g.expires_at
        if g.reason:
            d["reason"] = g.reason
        lines.append(json.dumps(d))
    output = "\n".join(lines) + ("\n" if lines else "")
    if args.path == "-":
        sys.stdout.write(output)
    else:
        Path(args.path).write_text(output)
        print(_green(f"✓ exported {len(lines)} grant(s) to {args.path}"))
    return 0


def cmd_import(args: argparse.Namespace, store: TrustStore) -> int:
    text = Path(args.path).read_text()
    added = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(_red(f"line {lineno}: skipping bad JSON ({exc})"),
                  file=sys.stderr)
            continue
        try:
            if args.dry_run:
                print(_yellow(f"[dry-run] would import: {obj}"))
                continue
            store.grant(app=obj.get("app", ""), tool=obj.get("tool", ""),
                        tier=obj.get("tier", TIER_PERSISTENT),
                        ttl_seconds=int(obj.get("ttl_seconds", 0) or 0),
                        granted_by="user",
                        reason=obj.get("reason", "imported"))
            added += 1
        except TrustError as exc:
            print(_red(f"line {lineno}: {exc}"), file=sys.stderr)
    if not args.dry_run:
        print(_green(f"✓ imported {added} grant(s) from {args.path}"))
    return 0


# ── main() ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ib-trust",
        description="Manage the Icebreaker GUI trust store — the per-app "
                    "per-tool allowlist that lets grounded actions run "
                    "without re-prompting.",
    )
    p.add_argument("--store", default=None,
                   help="override trust store path (default: /var/lib/icebreaker/gui_trust.jsonl)")
    p.add_argument("--defaults-dir", default=None,
                   help="override defaults dir (default: /etc/icebreaker/gui_trust.d/)")
    p.add_argument("--session-id", default=os.environ.get("ICEBREAKER_SESSION_ID", ""),
                   help="session_id for session-scoped grants")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    p_list = sub.add_parser("list", help="show active grants")
    p_list.add_argument("--all", action="store_true", help="include expired")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="grant trust for an app + tool")
    p_add.add_argument("app")
    p_add.add_argument("tool")
    p_add.add_argument("--tier", choices=[TIER_ONCE, TIER_SESSION, TIER_PERSISTENT, TIER_DENY],
                       default=TIER_PERSISTENT)
    p_add.add_argument("--ttl", type=int, default=3600,
                       help="seconds until expiry (persistent tier only; default 3600)")
    p_add.add_argument("--reason", default="user-granted via ib-trust")
    p_add.add_argument("--dry-run", action="store_true",
                       help="show what would happen without persisting")
    p_add.set_defaults(func=cmd_add)

    p_rev = sub.add_parser("revoke", help="remove active grants for app + tool")
    p_rev.add_argument("app")
    p_rev.add_argument("tool")
    p_rev.add_argument("--dry-run", action="store_true")
    p_rev.set_defaults(func=cmd_revoke)

    p_undo = sub.add_parser("undo", help="revert the last add or revoke")
    p_undo.set_defaults(func=cmd_undo)

    p_why = sub.add_parser("why", help="explain the check() result for app + tool")
    p_why.add_argument("app")
    p_why.add_argument("tool")
    p_why.set_defaults(func=cmd_why)

    p_defaults = sub.add_parser("defaults", help="show ISO-shipped defaults")
    p_defaults.set_defaults(func=cmd_defaults)

    p_export = sub.add_parser("export", help="dump user grants as JSONL")
    p_export.add_argument("path", nargs="?", default="-",
                          help="- for stdout (default), or a file path")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="merge grants from a JSONL file")
    p_import.add_argument("path")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    store_kwargs = {}
    if args.store:
        store_kwargs["path"] = Path(args.store)
    if args.defaults_dir:
        store_kwargs["defaults_dir"] = Path(args.defaults_dir)
    if args.session_id:
        store_kwargs["session_id"] = args.session_id

    try:
        store = TrustStore(**store_kwargs)
    except (OSError, TrustError) as exc:
        print(_red(f"error: cannot open trust store: {exc}"), file=sys.stderr)
        return 3

    # No subcommand? Show help + current state — discoverable UX.
    if args.command is None:
        parser.print_help()
        print()
        print(_bold("Current active grants:"))
        cmd_list(argparse.Namespace(all=False, json=False), store)
        return 0

    try:
        return args.func(args, store)
    except (TrustError, OSError) as exc:
        print(_red(f"error: {exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
