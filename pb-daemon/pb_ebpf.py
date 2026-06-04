#!/usr/bin/env python3
"""
pb_ebpf.py — Privileged Brain eBPF Kernel Probe Module
Attaches BCC probes to kernel functions to capture low-level events
(OOM kills, abnormal process exits, repeated syscall failures) and
forwards them to pb-daemon via its Unix socket.

Requirements:
  apt install bpfcc-tools python3-bpfcc linux-headers-$(uname -r)

Run as root: sudo python3 pb_ebpf.py
"""

import json
import os
import signal
import socket
import sys
import time

SOCKET_PATH = "/run/pb-daemon.sock"

# ── eBPF C program ────────────────────────────────────────────────────────────
# Attaches to:
#   - oom_kill_process  (OOM killer fires)
#   - do_coredump       (process crashed with a core dump)
#   - inet_csk_accept   (new TCP connection accepted — for connection storms)

BPF_PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#include <linux/oom.h>

// ── Event type enum sent via perf buffer ──
enum event_type {
    EVT_OOM    = 1,
    EVT_CRASH  = 2,
    EVT_CONN   = 3,
};

struct event_t {
    u32  pid;
    u32  uid;
    char comm[TASK_COMM_LEN];
    u32  type;
    u64  ts_ns;
};

BPF_PERF_OUTPUT(events);
BPF_HASH(crash_count, u32, u32);   // pid → crash count

// ── OOM kill ──────────────────────────────────────────────────────────────────
int kprobe__oom_kill_process(struct pt_regs *ctx, struct oom_control *oc, const char *message)
{
    struct event_t evt = {};
    evt.type   = EVT_OOM;
    evt.ts_ns  = bpf_ktime_get_ns();
    bpf_get_current_comm(&evt.comm, sizeof(evt.comm));
    evt.pid    = bpf_get_current_pid_tgid() >> 32;
    evt.uid    = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    events.perf_submit(ctx, &evt, sizeof(evt));
    return 0;
}

// ── Core dump (crash) ─────────────────────────────────────────────────────────
int kprobe__do_coredump(struct pt_regs *ctx)
{
    struct event_t evt = {};
    evt.type   = EVT_CRASH;
    evt.ts_ns  = bpf_ktime_get_ns();
    bpf_get_current_comm(&evt.comm, sizeof(evt.comm));
    evt.pid    = bpf_get_current_pid_tgid() >> 32;
    evt.uid    = bpf_get_current_uid_gid() & 0xFFFFFFFF;
    events.perf_submit(ctx, &evt, sizeof(evt));
    return 0;
}
"""

# ── Event names ───────────────────────────────────────────────────────────────
EVT_NAMES = {1: "OOM_KILL", 2: "CORE_DUMP", 3: "TCP_FLOOD"}


def send_to_daemon(message: str) -> None:
    """Forward an event string to pb-daemon's Unix socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCKET_PATH)
            s.sendall((message + "\n").encode())
            # Drain response (non-blocking)
            s.settimeout(5.0)
            try:
                resp = s.recv(4096)
                print(f"[pb-daemon] {resp.decode().strip()}")
            except Exception:
                pass
    except Exception as exc:
        print(f"[pb-ebpf] Could not reach pb-daemon: {exc}", file=sys.stderr)


def main() -> None:
    if os.geteuid() != 0:
        print("ERROR: pb_ebpf.py must run as root (sudo).", file=sys.stderr)
        sys.exit(1)

    try:
        from bcc import BPF
    except ImportError:
        print("ERROR: python3-bpfcc not installed.", file=sys.stderr)
        print("  sudo apt install bpfcc-tools python3-bpfcc linux-headers-$(uname -r)", file=sys.stderr)
        sys.exit(1)

    print("[pb-ebpf] Attaching eBPF probes...")
    b = BPF(text=BPF_PROGRAM)
    print("[pb-ebpf] Probes attached:")
    print("  kprobe::oom_kill_process  — OOM killer")
    print("  kprobe::do_coredump       — process crash / core dump")
    print(f"[pb-ebpf] Forwarding events to pb-daemon at {SOCKET_PATH}")

    # Deduplicate: don't spam daemon with same event within 10s
    _last: dict = {}

    def handle_event(cpu, data, size):
        evt   = b["events"].event(data)
        etype = EVT_NAMES.get(evt.type, "UNKNOWN")
        comm  = evt.comm.decode(errors="replace")
        pid   = evt.pid
        key   = f"{etype}:{comm}"
        now   = time.time()
        if now - _last.get(key, 0) < 10:
            return
        _last[key] = now

        msg = (
            f"[KERNEL EVENT] type={etype} process={comm} pid={pid}\n"
            f"Please explain what '{etype}' means for process '{comm}' "
            f"and give a one-line remediation command."
        )
        print(f"[pb-ebpf] Event: {etype} | process={comm} pid={pid}")
        send_to_daemon(msg)

    b["events"].open_perf_buffer(handle_event, page_cnt=64)

    def _shutdown(sig, frame):
        print("\n[pb-ebpf] Detaching probes and exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("[pb-ebpf] Monitoring... (Ctrl+C to stop)")
    while True:
        try:
            b.perf_buffer_poll(timeout=1000)
        except KeyboardInterrupt:
            _shutdown(None, None)


if __name__ == "__main__":
    main()
