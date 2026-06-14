"""
mcpd_client.py — Long-lived subprocess wrapper for the mcpd Rust daemon.

mcpd (Phase 1) speaks JSON-RPC 2.0 over stdin/stdout, one newline-terminated
request line in / one line out, strictly request-response. It exits 0 on
stdin EOF (see src/mcpd/src/server.rs).

This module is the Controller's only interface to mcpd. M2.x callers go
through `McpdClient.call(method, params)`; the orchestration layer in M2.12
will instantiate one client per Controller run and reuse it across intents.

Security / invariant alignment:
  - INV-3 (mcpd is stdio-only): we never open a TCP/Unix socket to mcpd. The
    transport is the inherited pipes.
  - INV-4 (params validated before exec): mcpd does its own JSON-Schema
    validation server-side and returns -32602 on failure. The Controller's
    intent_schema validator (M2.2) is an additional client-side gate.
  - Tool-output reflection (INV-2-extended): this module surfaces raw mcpd
    output. The orchestration loop is responsible for routing that output
    to QB summarisation, never back into the PB context.

Concurrency:
  Calls are serialised by an internal lock. mcpd's stdio transport is a
  single linear stream — concurrent writes would corrupt it. The lock is
  cheap insurance against accidental threading mistakes upstream.

Timeout:
  Per-call hard timeout (default 10 s). On timeout the underlying mcpd
  process is killed (SIGKILL) and McpdTimeoutError is raised. Subsequent
  calls fail with McpdProcessError until the caller respawns the client.
  We do NOT auto-restart — the orchestration layer decides whether a
  killed-mcpd-mid-intent is recoverable, and it has the audit context.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union


# Per-call default timeout (P2-F8 mitigation).
DEFAULT_TIMEOUT_SECONDS = 10.0

# ── Environment scrubbing (SF-7 / BP-8) ───────────────────────────────────
# Only these variables are inherited by the mcpd subprocess.  API keys,
# database URLs, and other secrets are excluded.

_SAFE_ENV_VARS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LC_ALL", "LC_CTYPE", "LANG", "TZ",
    "TERM", "COLORTERM",
})


def _scrubbed_env(
    *,
    audit_log: Optional[Union[str, Path]] = None,
    rust_log: Optional[str] = None,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a scrubbed environment for the mcpd subprocess.

    Only whitelisted variables are inherited from the parent process.
    API keys and other secrets are excluded (SF-7).
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_VARS}
    if audit_log is not None:
        env["MCPD_AUDIT_LOG"] = str(Path(audit_log).expanduser())
    if rust_log is not None:
        env["RUST_LOG"] = rust_log
    if extra:
        env.update(extra)
    return env

# Bytes read per os.read() call when assembling a response line.
_READ_CHUNK = 4096

# Time we wait for mcpd to gracefully shutdown after closing stdin before
# escalating to SIGKILL.
_GRACEFUL_SHUTDOWN_SECONDS = 5.0


# ── Exceptions ──────────────────────────────────────────────────────────────

class McpdError(Exception):
    """Base for all McpdClient errors."""


class McpdProcessError(McpdError):
    """mcpd died, couldn't start, or has already been closed."""


class McpdTimeoutError(McpdError):
    """mcpd took longer than the per-call timeout to respond.

    When this is raised the mcpd process has already been SIGKILLed.
    The client is dead and subsequent calls will raise McpdProcessError.
    """


class McpdProtocolError(McpdError):
    """mcpd's response was not valid line-delimited JSON-RPC 2.0.

    Possible causes: corrupt stream, mismatched ids, partial line on EOF.
    Raises imply a contract violation and should be treated as fatal.
    """


class JsonRpcError(McpdError):
    """mcpd returned a JSON-RPC error response (per the 2.0 spec § 5.1).

    Standard codes mcpd emits:
      -32700  Parse error          (mcpd couldn't parse our request line)
      -32600  Invalid request      (jsonrpc field missing or != "2.0")
      -32601  Method not found     (unknown tool/method)
      -32602  Invalid params       (schema validation failed)
      -32603  Internal error       (tool execution raised)
    """

    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# ── Result envelope ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolResult:
    """A successful mcpd response.

    For most tools `result` is the operation's output envelope. For
    fs.delete, fs.write outside $HOME, and package.{install,remove,upgrade}
    the envelope's `status` is "requires_cow_approval" and the orchestration
    layer must surface a HITL prompt before proceeding (Phase 3 wires the
    COW overlay; for V1 the Controller surfaces the preview verbatim).
    """

    result: dict
    request_id: int

    @property
    def status(self) -> Optional[str]:
        if isinstance(self.result, dict):
            v = self.result.get("status")
            return v if isinstance(v, str) else None
        return None

    @property
    def requires_cow_approval(self) -> bool:
        return self.status == "requires_cow_approval"

    @property
    def cow_intent_id(self) -> Optional[str]:
        if not self.requires_cow_approval:
            return None
        v = self.result.get("intent_id")
        return v if isinstance(v, str) else None

    @property
    def cow_preview(self) -> Optional[dict]:
        if not self.requires_cow_approval:
            return None
        v = self.result.get("preview")
        return v if isinstance(v, dict) else None


# ── Client ──────────────────────────────────────────────────────────────────

class McpdClient:
    """Long-lived mcpd subprocess wrapper.

    Use as a context manager whenever possible:
        with McpdClient.spawn(Path("/path/to/mcpd")) as client:
            tools = client.list_tools()
            result = client.call("system.status")

    Otherwise call .close() explicitly. The destructor will SIGKILL a leaked
    process as a last-resort safety net, but explicit cleanup is preferred.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        binary_path: Path,
        default_timeout: float,
    ) -> None:
        """Internal — use McpdClient.spawn(...) instead."""
        self._proc = proc
        self._binary_path = binary_path
        self._default_timeout = default_timeout
        self._lock = threading.Lock()
        self._next_id = 1
        # Spillover buffer: if a single os.read() returns more bytes than a
        # single response line (shouldn't happen with mcpd's strict 1-in-1-out,
        # but defensive against framing surprises), we stash the tail here.
        self._read_buffer = bytearray()
        # Once True, all future calls raise McpdProcessError immediately.
        # Set when the process is known dead (close, kill, timeout, EOF).
        self._closed = False

    # ── Construction / lifecycle ────────────────────────────────────────

    @classmethod
    def spawn(
        cls,
        binary: Union[str, Path],
        *,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        audit_log: Optional[Union[str, Path]] = None,
        rust_log: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        capture_stderr: bool = False,
    ) -> "McpdClient":
        """Spawn mcpd as a child process and return a connected client.

        Args:
            binary: path to the mcpd executable.
            default_timeout: per-call timeout (seconds). Overridable per call.
            audit_log: if given, sets MCPD_AUDIT_LOG so mcpd writes its
                audit log to this path instead of the default
                /var/log/mcpd/audit.log. Useful for hermetic tests.
            rust_log: optional value for RUST_LOG (default: mcpd's own default).
            extra_env: arbitrary additional env vars for the subprocess.
            capture_stderr: if True, stderr is piped (instead of DEVNULL).
                Reserved for tests that need mcpd's diagnostic logs.

        Raises:
            McpdProcessError: if the binary doesn't exist, isn't executable,
                or the process exits before we can issue a request.
        """
        binary = Path(binary).expanduser()
        if not binary.exists():
            raise McpdProcessError(f"mcpd binary not found at {binary}")
        if not os.access(binary, os.X_OK):
            raise McpdProcessError(f"mcpd binary at {binary} is not executable")

        env = _scrubbed_env(audit_log=audit_log, rust_log=rust_log, extra=extra_env)

        stderr_target = subprocess.PIPE if capture_stderr else subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                [str(binary)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_target,
                bufsize=0,  # unbuffered — proven pattern from Phase 1 ci.sh G9
                env=env,
            )
        except OSError as e:
            raise McpdProcessError(f"failed to spawn mcpd: {e}") from e

        # No eager liveness check: a 200-400 ms poll wait would penalise
        # every healthy spawn for no win. If mcpd dies at startup the
        # first .call() will detect EOF on stdout and raise
        # McpdProcessError with the captured exit code.
        return cls(proc, binary_path=binary, default_timeout=default_timeout)

    def __enter__(self) -> "McpdClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        # Safety net for leaked clients. Explicit .close() is strongly
        # preferred — this path will SIGKILL a still-alive mcpd without
        # waiting for graceful shutdown.
        try:
            if not self._closed and self._proc.poll() is None:
                self._proc.kill()
        except Exception:  # noqa: BLE001 — destructor must not raise
            pass

    @property
    def is_alive(self) -> bool:
        if self._closed:
            return False
        return self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if not self._closed else None

    @property
    def binary_path(self) -> Path:
        return self._binary_path

    # ── Public RPC API ──────────────────────────────────────────────────

    def list_tools(self, *, timeout: Optional[float] = None) -> dict:
        """Calls `tools/list`. Returns the discovery envelope:
            {"schema_version": "<x.y.z>", "tools": [...]}
        """
        result = self.call("tools/list", {}, timeout=timeout)
        return result.result

    def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        """Send a JSON-RPC request, return the result envelope.

        Args:
            method: the JSON-RPC method name (e.g. "fs.read", "tools/list").
            params: parameter object (default {}). Per JSON-RPC 2.0 § 4.1
                this MAY be omitted; mcpd coerces null/missing to {}.
            timeout: per-call hard timeout in seconds. Default uses
                self._default_timeout.

        Returns:
            ToolResult — wraps the result envelope and exposes
            requires_cow_approval / cow_intent_id / cow_preview helpers.

        Raises:
            McpdProcessError: client is closed, or mcpd died.
            McpdTimeoutError: mcpd didn't respond in time (process killed).
            McpdProtocolError: response malformed or id mismatch.
            JsonRpcError: mcpd returned a JSON-RPC error response.
        """
        if not isinstance(method, str) or not method:
            raise TypeError("method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise TypeError("params must be a dict or None")

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")

        with self._lock:
            if self._closed:
                raise McpdProcessError("McpdClient is closed")
            if self._proc.poll() is not None:
                # Process died between calls. Mark closed and surface.
                self._closed = True
                raise McpdProcessError(
                    f"mcpd is not running (exit code {self._proc.returncode})"
                )

            request_id = self._next_id
            self._next_id += 1
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params if params is not None else {},
                "id": request_id,
            }
            line = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._closed = True
                raise McpdProcessError(f"mcpd stdin write failed: {e}") from e

            response_line = self._readline_with_timeout(effective_timeout)
            response = self._parse_response(response_line, request_id)
            # Errors raise; success returns ToolResult.
            return response

    def close(self, *, timeout: float = _GRACEFUL_SHUTDOWN_SECONDS) -> int:
        """Close stdin so mcpd shuts down gracefully (server.rs:70-73),
        wait up to `timeout` seconds for exit, escalate to SIGKILL otherwise.

        Returns the exit code (-SIGKILL if we had to force-kill).
        Idempotent: subsequent calls return the cached exit code.
        """
        with self._lock:
            if self._closed:
                return self._proc.returncode if self._proc.returncode is not None else -9

            self._closed = True

            # Close stdin to trigger mcpd's read_line EOF path.
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except OSError:
                pass

            # Wait for graceful exit.
            try:
                return self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass

            # Escalate.
            self._proc.kill()
            try:
                return self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                # Truly stuck — last-resort signal.
                try:
                    os.kill(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return -9

    # ── Internals ──────────────────────────────────────────────────────

    def _readline_with_timeout(self, timeout: float) -> bytes:
        """Read one newline-terminated line from mcpd's stdout.

        Uses select() on the underlying fd so we can enforce a hard timeout
        even though stdout was opened with bufsize=0 (unbuffered).
        On timeout the process is SIGKILLed and McpdTimeoutError is raised.
        On EOF before newline, the process is reaped and McpdProcessError /
        McpdProtocolError is raised depending on whether bytes were received.
        """
        deadline = time.monotonic() + timeout

        # Drain any spillover from a prior read first.
        line, remainder = self._extract_first_line(self._read_buffer)
        if line is not None:
            self._read_buffer = remainder
            return line

        fd = self._proc.stdout.fileno()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_for_timeout()
                raise McpdTimeoutError(
                    f"mcpd did not respond within {timeout:.3f}s; process killed"
                )

            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError) as e:
                # ValueError can come from a closed fd; OSError on EBADF / EINTR.
                self._closed = True
                raise McpdProcessError(f"select on mcpd stdout failed: {e}") from e

            if not ready:
                # Spurious wake — fall back to deadline check at loop top.
                continue

            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError as e:
                self._closed = True
                raise McpdProcessError(f"read from mcpd stdout failed: {e}") from e

            if not chunk:
                # EOF. mcpd closed stdout. It is dead or about to be.
                self._closed = True
                # Reap so .returncode is populated.
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)

                if self._read_buffer:
                    # Partial line then EOF — protocol violation.
                    partial = bytes(self._read_buffer)
                    self._read_buffer = bytearray()
                    raise McpdProtocolError(
                        f"mcpd closed stdout mid-response. Partial bytes: {partial!r}"
                    )
                raise McpdProcessError(
                    f"mcpd closed stdout (exit code {self._proc.returncode}) "
                    "before sending a response"
                )

            self._read_buffer.extend(chunk)
            line, remainder = self._extract_first_line(self._read_buffer)
            if line is not None:
                self._read_buffer = remainder
                return line
            # No newline yet — loop and read more.

    @staticmethod
    def _extract_first_line(buf: bytearray) -> tuple[Optional[bytes], bytearray]:
        """Return (line, remainder) if buf contains a newline; else (None, buf)."""
        idx = buf.find(b"\n")
        if idx == -1:
            return None, buf
        line = bytes(buf[:idx])
        remainder = bytearray(buf[idx + 1:])
        return line, remainder

    def _parse_response(self, raw: bytes, expected_id: int) -> ToolResult:
        """Parse one response line, validate id, return ToolResult or raise."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise McpdProtocolError(f"mcpd response was not valid utf-8: {e}") from e

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise McpdProtocolError(
                f"mcpd response was not valid JSON: {e}; raw={text!r}"
            ) from e

        if not isinstance(payload, dict):
            raise McpdProtocolError(
                f"mcpd response was not a JSON object: {text!r}"
            )

        if payload.get("jsonrpc") != "2.0":
            raise McpdProtocolError(
                f"mcpd response missing jsonrpc=='2.0': {text!r}"
            )

        # Parse errors (-32700) carry id=null per JSON-RPC 2.0 § 4.2. Don't
        # require id correlation on that single error path; raise immediately.
        err = payload.get("error")
        if err is not None:
            if not isinstance(err, dict):
                raise McpdProtocolError(f"mcpd error envelope malformed: {err!r}")
            code = err.get("code")
            message = err.get("message", "")
            data = err.get("data")
            if not isinstance(code, int):
                raise McpdProtocolError(
                    f"mcpd error envelope missing integer code: {err!r}"
                )
            raise JsonRpcError(code=code, message=str(message), data=data)

        # Successful response — id MUST match.
        if payload.get("id") != expected_id:
            raise McpdProtocolError(
                f"mcpd response id {payload.get('id')!r} != expected {expected_id!r}; "
                "stream out of sync — treating client as dead"
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpdProtocolError(
                f"mcpd response missing result object: {text!r}"
            )

        return ToolResult(result=result, request_id=expected_id)

    def _kill_for_timeout(self) -> None:
        """Send SIGKILL and mark client dead. Used by the timeout path."""
        self._closed = True
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
