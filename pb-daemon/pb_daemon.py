#!/usr/bin/env python3
"""
pb-daemon — Privileged Brain AI System Daemon
Monitors journald, /proc, and a Unix socket.
Calls the local Ollama API (privileged-brain) when anomalies are detected
or when the shell sends a direct query.

Run as: pb-daemon  (started by systemd)
Query:  pb-ask "question"
"""

import json
import logging
import os
import re
import select
import signal
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_URL      = "http://localhost:11434/api/generate"
MODEL_NAME      = "privileged-brain"
SOCKET_PATH     = "/run/pb-daemon.sock"
LOG_FILE        = "/var/log/pb-daemon.log"
POLL_INTERVAL   = 30          # seconds between /proc polls
OOM_THRESHOLD   = 95          # % memory used before alerting
CPU_THRESHOLD   = 90          # % CPU for >60s before alerting
DISK_THRESHOLD  = 90          # % disk used before alerting
MAX_LOG_BYTES   = 10_000_000  # rotate log at 10 MB

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger("pb-daemon")


# ── Ollama API ────────────────────────────────────────────────────────────────

def ask_model(prompt: str, max_tokens: int = 300) -> str:
    """Send a prompt to the local Ollama instance and return the response."""
    payload = json.dumps({
        "model":  MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.1},
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["response"].strip()
    except Exception as exc:
        return f"[pb-daemon: Ollama unavailable — {exc}]"


def ask_model_stream(prompt: str, sock_conn) -> None:
    """Stream the model response token-by-token to a Unix socket connection."""
    payload = json.dumps({
        "model":  MODEL_NAME,
        "prompt": prompt,
        "stream": True,
        "options": {"num_predict": 400, "temperature": 0.1},
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                token = chunk.get("response", "")
                sock_conn.sendall(token.encode())
                if chunk.get("done"):
                    break
        sock_conn.sendall(b"\n")
    except Exception as exc:
        sock_conn.sendall(f"\n[pb-daemon error: {exc}]\n".encode())


# ── /proc monitors ────────────────────────────────────────────────────────────

def read_meminfo() -> dict:
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
    except Exception:
        pass
    return info


def mem_used_pct() -> float:
    m = read_meminfo()
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", 0)
    if total == 0:
        return 0.0
    return 100.0 * (total - avail) / total


def disk_used_pct(path: str = "/") -> float:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        if total == 0:
            return 0.0
        return 100.0 * (total - avail) / total
    except Exception:
        return 0.0


def cpu_load() -> float:
    """1-minute load average as % of available CPUs."""
    try:
        load1 = os.getloadavg()[0]
        cpus  = os.cpu_count() or 1
        return 100.0 * load1 / cpus
    except Exception:
        return 0.0


# ── Alert helpers ─────────────────────────────────────────────────────────────

_alerted: dict = {}   # throttle: topic → last alert timestamp

def should_alert(topic: str, cooldown: int = 300) -> bool:
    now = time.time()
    if now - _alerted.get(topic, 0) > cooldown:
        _alerted[topic] = now
        return True
    return False


def alert(topic: str, context: str, cooldown: int = 300) -> None:
    if not should_alert(topic, cooldown):
        return
    log.warning("ANOMALY [%s]: %s", topic, context[:200])
    answer = ask_model(
        f"System anomaly detected. Context:\n{context}\n\n"
        "Give a 2-sentence explanation and one specific fix command."
    )
    log.warning("AI SUGGESTION [%s]: %s", topic, answer)
    # Desktop notification if running in a graphical session
    try:
        os.system(f'notify-send "Privileged Brain" "{topic}: {answer[:120]}" 2>/dev/null')
    except Exception:
        pass


# ── journald monitor ──────────────────────────────────────────────────────────

def journald_monitor() -> None:
    """Tail journald for ERROR/CRITICAL entries and alert on them."""
    try:
        from systemd import journal
    except ImportError:
        log.warning("systemd-python not installed — journald monitor disabled.")
        log.warning("Install: pip3 install systemd-python  (or apt install python3-systemd)")
        return

    j = journal.Reader()
    j.log_level(journal.LOG_ERR)   # ERROR and above
    j.seek_tail()
    j.get_previous()               # skip existing entries

    log.info("journald monitor started (watching ERROR+ events)")
    p = select.poll()
    p.register(j, j.get_events())

    while True:
        events = p.poll(5000)   # 5s timeout
        if events:
            j.process()
            for entry in j:
                unit    = entry.get("_SYSTEMD_UNIT", "")
                msg     = entry.get("MESSAGE", "")
                prio    = entry.get("PRIORITY", 6)
                if prio <= 3:   # ERR=3, CRIT=2, ALERT=1, EMERG=0
                    context = f"Unit: {unit}\nMessage: {msg}"
                    alert(f"journald:{unit or 'kernel'}", context, cooldown=120)


# ── /proc poller ──────────────────────────────────────────────────────────────

def proc_poller() -> None:
    log.info("Proc poller started (interval: %ds)", POLL_INTERVAL)
    while True:
        mem = mem_used_pct()
        cpu = cpu_load()
        dsk = disk_used_pct("/")

        log.debug("Stats — mem=%.1f%% cpu=%.1f%% disk=%.1f%%", mem, cpu, dsk)

        if mem > OOM_THRESHOLD:
            alert(
                "high-memory",
                f"Memory usage is {mem:.1f}% (threshold {OOM_THRESHOLD}%). "
                f"MemInfo: {read_meminfo()}",
            )
        if cpu > CPU_THRESHOLD:
            alert(
                "high-cpu",
                f"CPU load is {cpu:.1f}% of capacity (threshold {CPU_THRESHOLD}%). "
                f"Load average: {os.getloadavg()}",
            )
        if dsk > DISK_THRESHOLD:
            alert(
                "disk-full",
                f"Disk usage on / is {dsk:.1f}% (threshold {DISK_THRESHOLD}%).",
            )

        time.sleep(POLL_INTERVAL)


# ── Unix socket server (for pb-ask) ──────────────────────────────────────────

def handle_client(conn, addr) -> None:
    try:
        data = b""
        conn.settimeout(5.0)
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        question = data.decode().strip()
        if question:
            log.info("Shell query: %s", question[:120])
            ask_model_stream(question, conn)
    except Exception as exc:
        log.warning("Client error: %s", exc)
    finally:
        conn.close()


def socket_server() -> None:
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)   # allow any user to query
    srv.listen(8)
    log.info("Unix socket listening: %s", SOCKET_PATH)

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


# ── Systemd OnFailure handler ─────────────────────────────────────────────────
# Called by: systemd via pb-notify@.service with UNIT_NAME env var

def handle_unit_failure() -> None:
    unit = os.environ.get("UNIT_NAME", "unknown")
    log.warning("systemd unit failed: %s", unit)

    # Gather last 20 journal lines for context
    try:
        import subprocess
        lines = subprocess.check_output(
            ["journalctl", "-u", unit, "-n", "20", "--no-pager", "-o", "short"],
            text=True, timeout=5,
        )
    except Exception:
        lines = "(could not fetch journal)"

    context = f"systemd unit '{unit}' failed.\nLast log lines:\n{lines}"
    answer = ask_model(
        f"{context}\n\nExplain why this service failed and give a fix command."
    )
    log.warning("AI diagnosis for %s:\n%s", unit, answer)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Check if we are being invoked as the OnFailure handler
    if os.environ.get("PB_MODE") == "unit-failure":
        handle_unit_failure()
        return

    log.info("=" * 60)
    log.info("  Privileged Brain Daemon starting")
    log.info("  Model: %s  |  Ollama: %s", MODEL_NAME, OLLAMA_URL)
    log.info("  Socket: %s  |  Log: %s", SOCKET_PATH, LOG_FILE)
    log.info("=" * 60)

    # Verify Ollama is reachable
    try:
        test = ask_model("reply with only the word OK", max_tokens=5)
        log.info("Ollama check: %s", test)
    except Exception as exc:
        log.error("Cannot reach Ollama: %s", exc)
        log.error("Make sure Ollama is running: ollama serve")
        sys.exit(1)

    # Signal handler for clean shutdown
    def _shutdown(sig, frame):
        log.info("Shutting down (signal %d)", sig)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # Start background threads
    threads = [
        threading.Thread(target=journald_monitor, daemon=True, name="journald"),
        threading.Thread(target=proc_poller,      daemon=True, name="proc-poll"),
        threading.Thread(target=socket_server,    daemon=True, name="socket-srv"),
    ]
    for t in threads:
        t.start()
        log.info("Started thread: %s", t.name)

    # Keep main thread alive
    while True:
        time.sleep(60)
        # Log rotate check
        try:
            if Path(LOG_FILE).stat().st_size > MAX_LOG_BYTES:
                bak = LOG_FILE + ".1"
                if os.path.exists(bak):
                    os.unlink(bak)
                os.rename(LOG_FILE, bak)
        except Exception:
            pass


if __name__ == "__main__":
    main()
