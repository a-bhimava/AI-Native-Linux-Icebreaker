#!/usr/bin/env python3
"""ib_debug.py — Icebreaker System Diagnostic Monitor

Monitors and logs all Icebreaker subsystems: services, sockets, processes,
logs, models, config, and venv. Highlights start / run / fail / stop events.

Usage:
  python3 ib_debug.py                    # snapshot (default)
  python3 ib_debug.py snapshot           # one-shot health report
  python3 ib_debug.py watch [--interval N]  # live dashboard, refresh every N s
  python3 ib_debug.py logs [--lines N]   # tail logs with highlighting
  python3 ib_debug.py report [--out FILE]   # write full report to file
  python3 ib_debug.py journal            # recent journalctl for all IB units

Options:
  --interval N    Watch refresh interval in seconds (default: 5)
  --lines N       Lines per log file to show (default: 20)
  --out FILE      Report output path (default: /tmp/ib_report_<ts>.txt)
  --no-color      Disable ANSI color output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Optional rich ─────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.rule import Rule
    from rich.live import Live
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ── Constants ─────────────────────────────────────────────────────────────────

SERVICES = [
    ("icebreaker-first-boot.service", "First Boot"),
    ("icebreaker-controller.service", "Controller"),
    ("icebreaker-pbd.service",        "Privileged Brain"),
]

SOCKETS = [
    ("/run/icebreaker/controller.sock", "Controller RPC"),
    ("/run/icebreaker/pbd.sock",        "PB llama-server"),
]

LOG_PATHS = {
    "controller":  "/var/log/icebreaker/controller.log",
    "terminal":    "/var/log/icebreaker/terminal.log",
    "first-boot":  "/var/log/icebreaker/first-boot.log",
    "audit":       "/var/log/icebreaker/controller-audit.log",
}

CONFIG_FILE  = "/etc/icebreaker/controller.toml"
MODEL_DIRS   = ["/var/lib/icebreaker/models",
                os.path.expanduser("~/.local/share/icebreaker/models")]
CHECKSUM_REL = "checksums.sha256"
VENV_PYTHON  = "/opt/icebreaker/venv/bin/python3"

# Patterns for log line classification
_ERR_RE  = re.compile(r'\b(ERROR|FATAL|CRITICAL|Exception|Traceback|failed|crash|refused)\b', re.I)
_WARN_RE = re.compile(r'\b(WARN|WARNING|deprecated|timeout|retry|degraded)\b', re.I)
_OK_RE   = re.compile(r'\b(started|running|connected|OK|success|ready|loaded|listening)\b', re.I)
_STOP_RE = re.compile(r'\b(stopped|stopping|shutdown|exit|killed|terminated)\b', re.I)

# ── ANSI helpers ──────────────────────────────────────────────────────────────

NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()

def _c(text: str, code: str) -> str:
    if NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t):  return _c(t, "32")
def red(t):    return _c(t, "31")
def yellow(t): return _c(t, "33")
def cyan(t):   return _c(t, "36")
def bold(t):   return _c(t, "1")
def dim(t):    return _c(t, "2")

def tag_ok(msg=""):   return green("[OK]  ") + msg
def tag_fail(msg=""): return red("[FAIL] ") + msg
def tag_warn(msg=""): return yellow("[WARN] ") + msg
def tag_info(msg=""): return cyan("[INFO] ") + msg

def colorize_log_line(line: str) -> str:
    line = line.rstrip()
    if _ERR_RE.search(line):
        return red(line)
    if _WARN_RE.search(line):
        return yellow(line)
    if _STOP_RE.search(line):
        return yellow(line)
    if _OK_RE.search(line):
        return green(line)
    return dim(line)

def divider(title: str = "", width: int = 72) -> str:
    if title:
        pad = width - len(title) - 2
        return bold(f"── {title} " + "─" * max(0, pad))
    return bold("─" * width)

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ServiceStatus:
    unit: str
    label: str
    active: str = "unknown"     # active / inactive / failed / activating
    sub: str = "unknown"        # running / dead / exited / start / failed
    pid: Optional[str] = None
    memory: Optional[str] = None
    uptime: Optional[str] = None
    last_lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # first-boot is expected to be inactive/dead after a successful run
        if self.unit == "icebreaker-first-boot.service":
            return self.sub in ("dead", "exited", "running")
        return self.active == "active" and self.sub == "running"

    @property
    def status_tag(self) -> str:
        if self.ok:
            return tag_ok(f"{self.label}")
        if self.active == "failed" or self.sub == "failed":
            return tag_fail(f"{self.label}")
        if self.active == "inactive":
            state = "inactive (dead)" if self.unit == "icebreaker-first-boot.service" else "inactive"
            return (tag_ok if self.unit == "icebreaker-first-boot.service" else tag_warn)(
                f"{self.label} — {state}"
            )
        return tag_warn(f"{self.label} — {self.active}/{self.sub}")

@dataclass
class SocketStatus:
    path: str
    label: str
    exists: bool = False
    connectable: bool = False
    ping_ok: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exists and self.connectable

    @property
    def status_tag(self) -> str:
        if self.ping_ok:
            return tag_ok(f"{self.label} — connected, daemon responded")
        if self.connectable:
            return tag_ok(f"{self.label} — socket reachable")
        if self.exists:
            return tag_warn(f"{self.label} — socket exists but connection refused")
        return tag_fail(f"{self.label} — not found ({self.path})")

@dataclass
class LogSummary:
    name: str
    path: str
    exists: bool
    lines: list[str] = field(default_factory=list)  # last N lines
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size_bytes: int = 0

@dataclass
class ModelInfo:
    path: str
    name: str
    size_mb: float
    checksum_ok: Optional[bool]  # None = not checked

@dataclass
class ProcessInfo:
    pid: str
    cmdline: str
    cpu: str
    mem: str

@dataclass
class DiagSnapshot:
    timestamp: datetime
    hostname: str
    services: list[ServiceStatus]
    sockets: list[SocketStatus]
    processes: list[ProcessInfo]
    logs: list[LogSummary]
    models: list[ModelInfo]
    config_ok: bool
    config_error: str
    venv_ok: bool
    venv_error: str
    journal_lines: list[str]

# ── Collectors ────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"{cmd[0]}: not found"
    except Exception as exc:
        return -1, "", str(exc)

def collect_service(unit: str, label: str, log_lines: int = 5) -> ServiceStatus:
    s = ServiceStatus(unit=unit, label=label)
    rc, out, _ = _run(["systemctl", "show", unit,
                        "--property=ActiveState,SubState,MainPID,MemoryCurrent"])
    if rc != 0:
        s.active = "unavailable"
        return s
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    s.active  = props.get("ActiveState", "unknown")
    s.sub     = props.get("SubState",    "unknown")
    pid       = props.get("MainPID", "0")
    s.pid     = pid if pid and pid != "0" else None
    mem_bytes = props.get("MemoryCurrent", "")
    try:
        mb = int(mem_bytes) / (1024 * 1024)
        s.memory = f"{mb:.0f}MB"
    except (ValueError, TypeError):
        s.memory = None
    # last N journal lines
    rc2, out2, _ = _run(["journalctl", "-u", unit, "-n", str(log_lines), "--no-pager",
                          "--output=short-iso"], timeout=6)
    if rc2 == 0:
        s.last_lines = [l for l in out2.splitlines() if l.strip() and not l.startswith("--")]
    return s

def collect_socket(path: str, label: str) -> SocketStatus:
    s = SocketStatus(path=path, label=label)
    s.exists = Path(path).exists()
    if not s.exists:
        return s
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(path)
            s.connectable = True
            # Send a JSON-RPC ping to the controller
            req = json.dumps({"jsonrpc": "2.0", "method": "status", "id": 1}) + "\n"
            sock.sendall(req.encode())
            data = sock.recv(4096)
            if data:
                resp = json.loads(data.decode())
                if "result" in resp or resp.get("id") == 1:
                    s.ping_ok = True
    except (ConnectionRefusedError, OSError):
        s.connectable = False
    except json.JSONDecodeError:
        s.connectable = True   # connected but not a JSON-RPC socket (e.g. pbd)
        s.ping_ok = True
    except Exception as exc:
        s.error = str(exc)
    return s

def collect_processes() -> list[ProcessInfo]:
    rc, out, _ = _run(["ps", "aux"])
    procs = []
    # Specific patterns — broad words like "controller" are scoped to
    # Icebreaker paths to avoid matching macOS/system processes.
    exact = ("llama-server", "mcpd", "start-pbd", "start-qbd", "ib_debug.py")
    scoped = ("icebreaker",               # path or service name
              "-m controller",            # python3 -m controller
              "-m terminal",              # python3 -m terminal
              "/opt/icebreaker/",         # venv executables
              "icebreaker-controller",    # systemd unit name in argv
              "icebreaker-pbd",)
    if rc != 0:
        return procs
    for line in out.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        cmd = parts[10]
        if "ps aux" in cmd:
            continue
        if any(k in cmd for k in exact) or any(k in cmd for k in scoped):
            procs.append(ProcessInfo(
                pid=parts[1], cmdline=cmd, cpu=parts[2], mem=parts[3]
            ))
    return procs

def collect_log(name: str, path: str, n_lines: int = 20) -> LogSummary:
    p = Path(path)
    ls = LogSummary(name=name, path=path, exists=p.exists())
    if not ls.exists:
        return ls
    ls.size_bytes = p.stat().st_size
    try:
        rc, out, _ = _run(["tail", "-n", str(n_lines * 3), path])
        all_lines = out.splitlines()
    except Exception:
        return ls
    ls.lines = all_lines[-n_lines:]
    for line in all_lines:
        if _ERR_RE.search(line):
            ls.errors.append(line.strip())
        elif _WARN_RE.search(line):
            ls.warnings.append(line.strip())
    # keep only the 5 most recent of each
    ls.errors   = ls.errors[-5:]
    ls.warnings = ls.warnings[-5:]
    return ls

def collect_models() -> list[ModelInfo]:
    results = []
    seen = set()
    checksums: dict[str, str] = {}
    # Load checksums from any model dir
    for mdir in MODEL_DIRS:
        cs_path = Path(mdir) / CHECKSUM_REL
        if cs_path.exists():
            for line in cs_path.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2:
                    checksums[parts[1]] = parts[0]
            break
    for mdir in MODEL_DIRS:
        d = Path(mdir)
        if not d.exists():
            continue
        for f in d.glob("*.gguf"):
            if f.name in seen:
                continue
            seen.add(f.name)
            size_mb = f.stat().st_size / (1024 * 1024)
            # Check checksum if we have one
            ck_ok: Optional[bool] = None
            for cs_name, cs_hash in checksums.items():
                if f.name in cs_name or cs_name in f.name:
                    rc, out, _ = _run(["sha256sum", str(f)], timeout=60)
                    if rc == 0:
                        actual = out.split()[0]
                        ck_ok = actual == cs_hash
                    break
            results.append(ModelInfo(path=str(f), name=f.name,
                                     size_mb=size_mb, checksum_ok=ck_ok))
    return results

def collect_config() -> tuple[bool, str]:
    p = Path(CONFIG_FILE)
    if not p.exists():
        return False, f"not found: {CONFIG_FILE}"
    try:
        import tomllib  # Python 3.11+
        with p.open("rb") as f:
            tomllib.load(f)
        return True, ""
    except ImportError:
        pass
    try:
        import tomli
        with p.open("rb") as f:
            tomli.load(f)
        return True, ""
    except ImportError:
        pass
    # Fallback: basic structural check
    content = p.read_text()
    for required in ("[qb]", "[run]", "pb_endpoint", "pb_model_id"):
        if required not in content:
            return False, f"missing required key/section: {required}"
    return True, ""

def collect_venv() -> tuple[bool, str]:
    vp = Path(VENV_PYTHON)
    if not vp.exists():
        return False, f"venv python not found: {VENV_PYTHON}"
    rc, out, err = _run([str(vp), "-c",
        "import controller; import terminal; print('OK')"], timeout=10)
    if rc != 0:
        return False, (err or out).strip().splitlines()[-1] if (err or out) else "import failed"
    return True, ""

def collect_journal(n_lines: int = 30) -> list[str]:
    units = [u for u, _ in SERVICES]
    rc, out, _ = _run(
        ["journalctl", "--no-pager", "--output=short-iso", "-n", str(n_lines)] +
        [arg for u in units for arg in ["-u", u]],
        timeout=8
    )
    if rc != 0:
        return []
    return [l for l in out.splitlines() if l.strip() and not l.startswith("--")]

def take_snapshot(log_lines: int = 20) -> DiagSnapshot:
    hostname = _run(["hostname"])[1].strip() or "unknown"
    services = [collect_service(u, l) for u, l in SERVICES]
    sockets  = [collect_socket(p, l) for p, l in SOCKETS]
    processes = collect_processes()
    logs     = [collect_log(n, p, log_lines) for n, p in LOG_PATHS.items()]
    models   = collect_models()
    cfg_ok, cfg_err   = collect_config()
    venv_ok, venv_err = collect_venv()
    journal  = collect_journal(20)
    return DiagSnapshot(
        timestamp=datetime.now(),
        hostname=hostname,
        services=services,
        sockets=sockets,
        processes=processes,
        logs=logs,
        models=models,
        config_ok=cfg_ok,
        config_error=cfg_err,
        venv_ok=venv_ok,
        venv_error=venv_err,
        journal_lines=journal,
    )

# ── Renderers ─────────────────────────────────────────────────────────────────

def render_snapshot(snap: DiagSnapshot, show_logs: bool = True,
                    show_journal: bool = True) -> str:
    lines: list[str] = []
    w = 72

    # Header
    ts = snap.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(bold("═" * w))
    lines.append(bold(f"  ICEBREAKER DIAGNOSTIC  │  {ts}  │  {snap.hostname}"))
    lines.append(bold("═" * w))
    lines.append("")

    # ── Services ──
    lines.append(divider("SERVICES"))
    all_svc_ok = True
    for svc in snap.services:
        detail = ""
        if svc.pid:    detail += f"  PID={svc.pid}"
        if svc.memory: detail += f"  mem={svc.memory}"
        if svc.active == "failed":
            all_svc_ok = False
            lines.append("  " + tag_fail(
                f"{svc.label} ({svc.unit})  — FAILED"
            ))
        elif svc.ok:
            lines.append("  " + tag_ok(
                f"{svc.label}{dim(detail)}"
            ))
        else:
            all_svc_ok = False
            lines.append("  " + tag_warn(
                f"{svc.label} — {svc.active}/{svc.sub}{dim(detail)}"
            ))
    if not shutil.which("systemctl"):
        lines.append("  " + dim("  (systemctl not available — not running on systemd)"))
    lines.append("")

    # ── Sockets ──
    lines.append(divider("SOCKETS"))
    for sock in snap.sockets:
        lines.append("  " + sock.status_tag)
        if sock.error:
            lines.append("    " + dim(f"  error: {sock.error}"))
    lines.append("")

    # ── Processes ──
    lines.append(divider("PROCESSES"))
    if snap.processes:
        for p in snap.processes:
            cmd_short = p.cmdline[:80] + ("…" if len(p.cmdline) > 80 else "")
            lines.append(
                f"  " + green("●") + f"  PID {p.pid:>6}  "
                + dim(f"cpu={p.cpu}%  mem={p.mem}%  ")
                + cmd_short
            )
    else:
        lines.append("  " + tag_warn("No Icebreaker processes found"))
    lines.append("")

    # ── Models ──
    lines.append(divider("MODELS"))
    if snap.models:
        for m in snap.models:
            size_str = f"{m.size_mb:.0f}MB"
            ck_str = ""
            if m.checksum_ok is True:
                ck_str = green("  checksum OK")
            elif m.checksum_ok is False:
                ck_str = red("  CHECKSUM MISMATCH")
            lines.append(f"  " + tag_ok(f"{m.name}  ({size_str}){ck_str}"))
    else:
        lines.append("  " + tag_warn("No .gguf model files found in model dirs"))
        for d in MODEL_DIRS:
            lines.append(f"    {dim('checked:')} {d}")
    lines.append("")

    # ── Config & Venv ──
    lines.append(divider("CONFIG & VENV"))
    if snap.config_ok:
        lines.append("  " + tag_ok(f"controller.toml  ({CONFIG_FILE})"))
    else:
        lines.append("  " + tag_fail(f"controller.toml  — {snap.config_error}"))
    if snap.venv_ok:
        lines.append("  " + tag_ok(f"Python venv  ({VENV_PYTHON})"))
    else:
        lines.append("  " + tag_fail(f"Python venv  — {snap.venv_error}"))
    lines.append("")

    # ── Log summary ──
    if show_logs:
        lines.append(divider("LOG SUMMARY  (recent errors & warnings)"))
        any_issue = False
        for ls in snap.logs:
            size_str = f"{ls.size_bytes // 1024}KB" if ls.size_bytes > 0 else "empty"
            if not ls.exists:
                lines.append("  " + dim(f"{ls.name}: not found ({ls.path})"))
                continue
            header = dim(f"{ls.name}  [{size_str}]")
            if ls.errors:
                any_issue = True
                lines.append("  " + red(f"[ERR] {ls.name}") + dim(f"  [{size_str}]"))
                for e in ls.errors[-3:]:
                    lines.append("    " + red("▸ ") + e[:120])
            elif ls.warnings:
                lines.append("  " + yellow(f"[WRN] {ls.name}") + dim(f"  [{size_str}]"))
                for w in ls.warnings[-2:]:
                    lines.append("    " + yellow("▸ ") + w[:120])
            else:
                lines.append("  " + tag_ok(header))
        if not any_issue:
            lines.append("  " + green("No errors or warnings found in logs"))
        lines.append("")

    # ── Journal ──
    if show_journal and snap.journal_lines:
        lines.append(divider("RECENT JOURNAL  (all IB units)"))
        for jl in snap.journal_lines[-15:]:
            lines.append("  " + colorize_log_line(jl))
        lines.append("")

    # ── Health summary ──
    lines.append(bold("═" * w))
    failed_svcs   = [s.label for s in snap.services if not s.ok and s.active == "failed"]
    inactive_svcs = [s.label for s in snap.services if not s.ok and s.active != "failed"]
    failed_socks  = [s.label for s in snap.sockets if not s.ok]
    total_errors  = sum(len(l.errors) for l in snap.logs)

    if not failed_svcs and not failed_socks and snap.config_ok and snap.venv_ok:
        lines.append(green("  SYSTEM HEALTHY") + dim(f"  ({len(snap.processes)} IB processes running)"))
    else:
        lines.append(red("  SYSTEM ISSUES DETECTED:"))
        if failed_svcs:
            lines.append(red(f"    FAILED services: {', '.join(failed_svcs)}"))
        if inactive_svcs:
            lines.append(yellow(f"    INACTIVE: {', '.join(inactive_svcs)}"))
        if failed_socks:
            lines.append(red(f"    MISSING sockets: {', '.join(failed_socks)}"))
        if total_errors:
            lines.append(yellow(f"    {total_errors} error(s) in log files"))
        if not snap.config_ok:
            lines.append(red(f"    CONFIG: {snap.config_error}"))
        if not snap.venv_ok:
            lines.append(red(f"    VENV: {snap.venv_error}"))
    lines.append(bold("═" * w))

    return "\n".join(lines)

def render_tail_line(source: str, line: str) -> str:
    tag = cyan(f"[{source:<12}]")
    return f"{tag} {colorize_log_line(line)}"

# ── Rich versions (if available) ──────────────────────────────────────────────

def rich_snapshot(snap: DiagSnapshot) -> None:
    if not HAS_RICH:
        print(render_snapshot(snap))
        return
    console = Console()
    ts = snap.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    console.print(Rule(f"[bold]ICEBREAKER DIAGNOSTIC[/]  {ts}  │  {snap.hostname}",
                       style="bold blue"))

    # Services table
    t = Table(title="Services", box=box.SIMPLE, show_header=True,
              header_style="bold cyan")
    t.add_column("Service", style="bold")
    t.add_column("State")
    t.add_column("PID")
    t.add_column("Memory")
    for svc in snap.services:
        if svc.ok:
            state = Text("● running", style="green")
        elif svc.active == "failed":
            state = Text("✗ FAILED", style="bold red")
        elif svc.active == "inactive" and svc.unit == "icebreaker-first-boot.service":
            state = Text("○ done", style="dim green")
        else:
            state = Text(f"? {svc.active}/{svc.sub}", style="yellow")
        t.add_row(svc.label, state, svc.pid or "—", svc.memory or "—")
    console.print(t)

    # Sockets table
    t2 = Table(title="Sockets", box=box.SIMPLE, show_header=True,
               header_style="bold cyan")
    t2.add_column("Socket")
    t2.add_column("Path", style="dim")
    t2.add_column("Status")
    for sock in snap.sockets:
        if sock.ping_ok:
            st = Text("✓ responded", style="green")
        elif sock.connectable:
            st = Text("✓ reachable", style="green")
        elif sock.exists:
            st = Text("! conn refused", style="yellow")
        else:
            st = Text("✗ not found", style="bold red")
        t2.add_row(sock.label, sock.path, st)
    console.print(t2)

    # Models table
    t3 = Table(title="Models", box=box.SIMPLE, show_header=True,
               header_style="bold cyan")
    t3.add_column("File")
    t3.add_column("Size")
    t3.add_column("Checksum")
    for m in snap.models:
        ck = ("✓ OK" if m.checksum_ok else
              ("✗ MISMATCH" if m.checksum_ok is False else "—"))
        ck_style = ("green" if m.checksum_ok else
                    ("bold red" if m.checksum_ok is False else "dim"))
        t3.add_row(m.name, f"{m.size_mb:.0f}MB", Text(ck, style=ck_style))
    if not snap.models:
        t3.add_row("[dim]no .gguf files found[/dim]", "", "")
    console.print(t3)

    # Processes
    if snap.processes:
        t4 = Table(title="Processes", box=box.SIMPLE, show_header=True,
                   header_style="bold cyan")
        t4.add_column("PID")
        t4.add_column("CPU%")
        t4.add_column("MEM%")
        t4.add_column("Command", no_wrap=False)
        for p in snap.processes:
            t4.add_row(p.pid, p.cpu, p.mem, p.cmdline[:100])
        console.print(t4)

    # Errors from logs
    all_errors = [(ls.name, e) for ls in snap.logs for e in ls.errors]
    if all_errors:
        console.print(Rule("[bold red]Log Errors[/bold red]", style="red"))
        for src, line in all_errors[-10:]:
            console.print(f"  [dim]{src}[/dim]  [red]{line[:120]}[/red]")

    # Health footer
    failed = [s.label for s in snap.services if s.active == "failed"]
    bad_socks = [s.label for s in snap.sockets if not s.ok]
    if not failed and not bad_socks and snap.config_ok and snap.venv_ok:
        console.print(Rule("[green bold]SYSTEM HEALTHY[/green bold]", style="green"))
    else:
        console.print(Rule("[bold red]ISSUES DETECTED[/bold red]", style="red"))
        if failed:
            console.print(f"  [bold red]Failed services:[/bold red] {', '.join(failed)}")
        if bad_socks:
            console.print(f"  [bold red]Missing sockets:[/bold red] {', '.join(bad_socks)}")
        if not snap.config_ok:
            console.print(f"  [bold red]Config:[/bold red] {snap.config_error}")
        if not snap.venv_ok:
            console.print(f"  [bold red]Venv:[/bold red] {snap.venv_error}")

# ── Modes ─────────────────────────────────────────────────────────────────────

def mode_snapshot(args: argparse.Namespace) -> None:
    print(dim(f"Collecting diagnostics…"), end="\r", flush=True)
    snap = take_snapshot(log_lines=args.lines)
    print(" " * 40, end="\r")
    if HAS_RICH and not NO_COLOR:
        rich_snapshot(snap)
    else:
        print(render_snapshot(snap))

def mode_watch(args: argparse.Namespace) -> None:
    interval = args.interval
    print(bold(f"Watching Icebreaker systems — refresh every {interval}s  (Ctrl-C to stop)"))
    try:
        while True:
            snap = take_snapshot(log_lines=10)
            # Clear screen
            print("\033[2J\033[H", end="")
            if HAS_RICH and not NO_COLOR:
                rich_snapshot(snap)
            else:
                print(render_snapshot(snap, show_journal=False))
            print(dim(f"\n  Next refresh in {interval}s…  (Ctrl-C to stop)"))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n" + dim("Stopped."))

def mode_logs(args: argparse.Namespace) -> None:
    """Tail all log files continuously with colorized output."""
    existing = {name: path for name, path in LOG_PATHS.items()
                if Path(path).exists()}
    if not existing:
        print(tag_warn("No log files found at expected paths:"))
        for name, path in LOG_PATHS.items():
            print(f"  {path}")
        return

    print(bold(f"Tailing {len(existing)} log file(s) — Ctrl-C to stop"))
    for name, path in existing.items():
        print(f"  {cyan(name)}: {dim(path)}")
    print(divider())

    # Print last N lines of each file first
    for name, path in existing.items():
        rc, out, _ = _run(["tail", "-n", str(args.lines), path])
        if rc == 0:
            for line in out.splitlines():
                print(render_tail_line(name, line))

    # Now follow all files
    try:
        cmd = ["tail", "-f", "-n", "0"] + list(existing.values())
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True)
        path_to_name = {v: k for k, v in existing.items()}
        current_source = "unknown"
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("==>") and line.endswith("<=="):
                # tail file-change header: "==> /path/to/file.log <=="
                path_part = line[4:-4].strip()
                current_source = path_to_name.get(path_part, Path(path_part).stem)
            elif line:
                print(render_tail_line(current_source, line))
    except KeyboardInterrupt:
        print("\n" + dim("Stopped."))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

def mode_journal(args: argparse.Namespace) -> None:
    """Show recent journal entries for all IB units."""
    units = [u for u, _ in SERVICES]
    cmd = (["journalctl", "--no-pager", "--output=short-iso",
             "-n", str(args.lines)] +
           [arg for u in units for arg in ["-u", u]])
    rc, out, err = _run(cmd, timeout=10)
    if rc != 0:
        print(tag_warn(f"journalctl failed: {err}"))
        return
    for line in out.splitlines():
        if not line.startswith("--"):
            print(colorize_log_line(line))

def mode_report(args: argparse.Namespace) -> None:
    """Write a full diagnostic report to file."""
    outpath = args.out or f"/tmp/ib_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
    print(dim(f"Collecting full diagnostics for report…"))
    snap = take_snapshot(log_lines=50)
    text = render_snapshot(snap, show_logs=True, show_journal=True)
    # Strip ANSI for file output
    ansi_re = re.compile(r'\033\[[0-9;]*m')
    clean_text = ansi_re.sub("", text)

    # Also dump raw JSON for machine parsing
    def _status(ok, detail=""):
        return {"ok": ok, "detail": detail}
    data = {
        "timestamp": snap.timestamp.isoformat(),
        "hostname":  snap.hostname,
        "services":  [{
            "unit":   s.unit, "label": s.label,
            "active": s.active, "sub": s.sub,
            "pid":    s.pid, "memory": s.memory, "ok": s.ok,
            "journal_tail": s.last_lines,
        } for s in snap.services],
        "sockets":   [{
            "path": s.path, "label": s.label,
            "exists": s.exists, "connectable": s.connectable, "ping_ok": s.ping_ok,
        } for s in snap.sockets],
        "models":    [{
            "name": m.name, "size_mb": round(m.size_mb, 1),
            "checksum_ok": m.checksum_ok,
        } for m in snap.models],
        "config_ok": snap.config_ok,
        "config_error": snap.config_error,
        "venv_ok":   snap.venv_ok,
        "venv_error": snap.venv_error,
        "log_errors": {ls.name: ls.errors for ls in snap.logs},
        "log_warnings": {ls.name: ls.warnings for ls in snap.logs},
        "processes": [{"pid": p.pid, "cmd": p.cmdline} for p in snap.processes],
    }

    with open(outpath, "w") as f:
        f.write(clean_text)
        f.write("\n\n" + "─" * 72 + "\n")
        f.write("RAW JSON\n")
        f.write("─" * 72 + "\n")
        json.dump(data, f, indent=2)

    print(tag_ok(f"Report written to {outpath}"))
    # Also print summary to terminal
    if HAS_RICH and not NO_COLOR:
        rich_snapshot(snap)
    else:
        print(render_snapshot(snap))

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Icebreaker system diagnostic monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("mode", nargs="?", default="snapshot",
                        choices=["snapshot", "watch", "logs", "journal", "report"],
                        help="Mode to run (default: snapshot)")
    parser.add_argument("--interval", type=int, default=5,
                        help="Watch mode refresh interval in seconds (default: 5)")
    parser.add_argument("--lines", type=int, default=20,
                        help="Log lines to show (default: 20)")
    parser.add_argument("--out", type=str, default=None,
                        help="Report output file path")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color output")
    args = parser.parse_args()

    global NO_COLOR
    if args.no_color:
        NO_COLOR = True

    dispatch = {
        "snapshot": mode_snapshot,
        "watch":    mode_watch,
        "logs":     mode_logs,
        "journal":  mode_journal,
        "report":   mode_report,
    }
    dispatch[args.mode](args)
    return 0

if __name__ == "__main__":
    sys.exit(main())
