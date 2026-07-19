"""RpaBridge — sandboxed Robot Framework automation subprocess.

Spawned by the Controller daemon (ADR-13). Communicates over AF_UNIX
JSON-RPC, same framing as ``controller.protocol``.

Lifecycle:
  1. Controller calls ``RpaBridge.spawn()``
  2. Child applies Landlock + Seccomp (INV-5)
  3. Child enters JSON-RPC request loop on stdin/stdout
  4. On stdin EOF, child exits cleanly

Hard timeout (default 30s) enforced via ``signal.setitimer`` + SIGALRM.
If a workflow exceeds the deadline, SIGALRM raises ``_WorkflowTimeout``,
partial results are collected and returned. The Controller's SIGKILL
watchdog (PR #29) provides a defense-in-depth second layer.

Environment scrubbing (BP-8): only safe locale/term vars plus display
vars are inherited.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from controller.protocol import (
    JsonRpcResponse,
    JSONRPC_VERSION,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    make_error,
)

from .protocol import (
    ALL_RPA_METHODS,
    RPA_PING,
    RPA_EXECUTE_WORKFLOW,
    RPA_FIND_BY_IMAGE,
    RPA_LIST_WORKFLOWS,
    validate_rpa_params,
)
from .workflow_gen import WorkflowGenerator, WorkflowError
from .image_match import ImageMatcher, ImageMatchError


_SAFE_ENV_VARS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LC_ALL", "LC_CTYPE", "LANG", "TZ",
    "TERM", "COLORTERM",
})

_RPA_ENV_VARS: frozenset[str] = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
})

_DEFAULT_SCRATCH_DIR = "/tmp/icebreaker-rpa"
_DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class KeywordResult:
    index: int
    name: str
    status: str  # "pass" | "fail" | "timeout" | "skipped"
    elapsed_ms: float
    screenshot_hash: str
    error: str = ""


@dataclass(frozen=True)
class WorkflowResult:
    success: bool
    timed_out: bool
    keywords_executed: int
    keywords_total: int
    elapsed_ms: float
    keyword_results: tuple[KeywordResult, ...]
    error: str = ""


class _WorkflowTimeout(Exception):
    """Internal: raised by SIGALRM handler when workflow exceeds timeout."""


def _scrubbed_rpa_env(
    *,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a scrubbed environment for the RPA Bridge subprocess (BP-8)."""
    env = {
        k: v for k, v in os.environ.items()
        if k in _SAFE_ENV_VARS or k in _RPA_ENV_VARS
    }
    if extra:
        env.update(extra)
    return env


class RpaBridge:
    """RPA Bridge process — Robot Framework UI automation.

    Use ``RpaBridge.main()`` as the subprocess entry point (via ``__main__.py``).
    Use ``RpaBridge.spawn_env()`` from the Controller to get the scrubbed env.
    """

    def __init__(self, scratch_dir: str | Path = _DEFAULT_SCRATCH_DIR) -> None:
        self._scratch = Path(scratch_dir)
        self._scratch.mkdir(parents=True, exist_ok=True)
        self._workflow_gen = WorkflowGenerator(scratch_dir=self._scratch)
        self._image_matcher = ImageMatcher()
        self._screenshot_manager: Any = None
        self._timed_out = False
        # F-53 Scope A.P1: expose the last screenshot capture failure so
        # audit/debug tooling can see WHY a step's screenshot_hash is
        # empty. Set on each _capture_step_screenshot() call — populated
        # on failure, cleared to None on success. Grep target for
        # test_no_silent_swallow.py's marker check.
        self._last_screenshot_error: str | None = None

    def _get_screenshot_manager(self) -> Any:
        if self._screenshot_manager is None:
            from gui_agent.screenshots import ScreenshotManager
            self._screenshot_manager = ScreenshotManager(self._scratch)
        return self._screenshot_manager

    def handle_request(self, method: str, params: dict) -> dict:
        """Dispatch an RPA method call. Returns a result dict."""
        try:
            validate_rpa_params(method, params)
        except Exception as exc:
            raise _InvalidParams(str(exc)) from exc

        if method == RPA_PING:
            return self._handle_ping()
        elif method == RPA_EXECUTE_WORKFLOW:
            return self._handle_execute_workflow(params)
        elif method == RPA_FIND_BY_IMAGE:
            return self._handle_find_by_image(params)
        elif method == RPA_LIST_WORKFLOWS:
            return self._handle_list_workflows()
        else:
            raise _MethodNotFound(method)

    def _handle_ping(self) -> dict:
        robot_available = False
        try:
            import robot  # noqa: F401
            robot_available = True
        except ImportError:
            pass

        return {
            "status": "ok",
            "robot_framework_available": robot_available,
            "uinput_available": os.path.exists("/dev/uinput"),
        }

    def _handle_execute_workflow(self, params: dict) -> dict:
        """Execute a keyword workflow with timeout and per-keyword screenshots."""
        keywords = params["keywords"]
        workflow_name = params.get("workflow_name", "workflow")
        timeout_seconds = params.get("timeout_seconds", _DEFAULT_TIMEOUT)

        try:
            validated = self._workflow_gen.validate_keywords(keywords)
        except WorkflowError as exc:
            return {
                "success": False,
                "error": str(exc),
                "reason": exc.reason,
                "keyword_name": exc.keyword_name,
            }

        self._workflow_gen.generate_robot_file(workflow_name, keywords)

        keyword_results: list[KeywordResult] = []
        start_time = time.monotonic()
        total = len(validated)
        self._timed_out = False

        def _sigalrm_handler(signum: int, frame: Any) -> None:
            self._timed_out = True
            raise _WorkflowTimeout("workflow exceeded timeout")

        old_handler = signal.signal(signal.SIGALRM, _sigalrm_handler)
        # setitimer (not alarm) so fractional timeouts like 2.5s are honored
        # instead of being truncated to whole seconds.
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

        try:
            for i, (name, args) in enumerate(validated):
                kw_start = time.monotonic()
                status = "pass"
                error = ""

                try:
                    self._execute_single_keyword(name, args)
                except _WorkflowTimeout:
                    elapsed_ms = (time.monotonic() - kw_start) * 1000
                    keyword_results.append(KeywordResult(
                        index=i, name=name, status="timeout",
                        elapsed_ms=elapsed_ms, screenshot_hash="",
                        error="workflow timeout exceeded",
                    ))
                    break
                except Exception as exc:  # noqa: BLE001
                    # F-53 Scope A.P3: per-keyword execution failure —
                    # `error = str(exc)` propagates the reason into the
                    # KeywordResult that both the audit log and the UI
                    # see. Nothing swallowed.
                    status = "fail"
                    error = str(exc)

                elapsed_ms = (time.monotonic() - kw_start) * 1000

                screenshot_hash = self._capture_step_screenshot()

                kr = KeywordResult(
                    index=i, name=name, status=status,
                    elapsed_ms=elapsed_ms,
                    screenshot_hash=screenshot_hash,
                    error=error,
                )
                keyword_results.append(kr)

                remaining = max(0, (timeout_seconds - (time.monotonic() - start_time)) * 1000)
                self._emit_progress(
                    keyword_index=i,
                    keyword_total=total,
                    keyword_name=name,
                    status=status,
                    elapsed_ms=elapsed_ms,
                    screenshot_hash=screenshot_hash,
                    timeout_remaining_ms=remaining,
                )

        except _WorkflowTimeout:
            self._timed_out = True
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, old_handler)

        total_elapsed = (time.monotonic() - start_time) * 1000
        all_passed = all(kr.status == "pass" for kr in keyword_results)

        return {
            "success": all_passed and not self._timed_out,
            "timed_out": self._timed_out,
            "keywords_executed": len(keyword_results),
            "keywords_total": total,
            "elapsed_ms": total_elapsed,
            "keyword_results": [
                {
                    "index": kr.index,
                    "name": kr.name,
                    "status": kr.status,
                    "elapsed_ms": kr.elapsed_ms,
                    "screenshot_hash": kr.screenshot_hash,
                    "error": kr.error,
                }
                for kr in keyword_results
            ],
        }

    def _execute_single_keyword(self, name: str, args: list[str]) -> None:
        """Execute a single Robot Framework keyword.

        Uses ``robot.api.TestSuite`` for execution. On macOS / systems
        without Robot Framework, this is a no-op (tests use the fake shim).
        """
        try:
            from robot.api import TestSuite
        except ImportError:
            return

        suite = TestSuite(name="single_keyword")
        test = suite.tests.create(name="step")
        test.body.create_keyword(name=name, args=args)
        suite.run(output=None, log=None, report=None)

    def _capture_step_screenshot(self) -> str:
        """Capture a screenshot after a keyword step, return SHA-256 hash.

        On failure, returns ``""`` and stashes the exception details on
        ``self._last_screenshot_error`` (F-53 Scope A.P1) so debug
        tooling can query why the hash is missing. Also emits an
        rpa.step_screenshot_error notification so the Controller +
        Errors page see it in real time. Callers already treat an
        empty return as "no screenshot" so no behavioral change for
        the happy path.
        """
        try:
            mgr = self._get_screenshot_manager()
            result = mgr.capture("")
            self._last_screenshot_error = None
            return result.sha256
        except Exception as exc:
            # F-53: previously swallowed silently — screenshot failures
            # under RPA workflows meant the audit trail showed missing
            # hashes with no explanation, and diagnosing "why did my
            # rpa.qb_monitor lose the frame" required attaching a
            # debugger. Now: stash + emit + return sentinel empty str.
            reason = f"{type(exc).__name__}: {exc}"[:400]
            self._last_screenshot_error = reason
            try:
                notification = {
                    "jsonrpc": "2.0",
                    "method": "rpa.step_screenshot_error",
                    "params": {"reason": reason},
                }
                sys.stdout.write(json.dumps(notification, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                # If stdout is torn during shutdown, we've already
                # captured the reason on self — nothing more to do.
                pass
            return ""

    def _emit_progress(self, **kwargs: Any) -> None:
        """Emit a JSON-RPC notification for per-keyword progress."""
        notification = {
            "jsonrpc": "2.0",
            "method": "rpa.keyword_progress",
            "params": kwargs,
        }
        sys.stdout.write(json.dumps(notification, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _handle_find_by_image(self, params: dict) -> dict:
        """Find a UI element by template image matching."""
        template_path = params["template_path"]
        confidence = params.get("confidence", ImageMatcher.DEFAULT_CONFIDENCE)
        region = params.get("region")

        screenshot_hash = self._capture_step_screenshot()

        try:
            mgr = self._get_screenshot_manager()
            screenshot_result = mgr.capture("")
            source_path = str(screenshot_result.path)
        except Exception as exc:
            return {"found": False, "error": f"Cannot capture screenshot: {exc}"}

        matcher = ImageMatcher(confidence=confidence)
        region_tuple = None
        if region:
            region_tuple = (region["x"], region["y"], region["width"], region["height"])

        try:
            result = matcher.match(source_path, template_path, region=region_tuple)
            return {
                "found": result.found,
                "confidence": result.confidence,
                "bbox": list(result.bbox),
                "center": list(result.center),
                "screenshot_hash": screenshot_result.sha256,
            }
        except ImageMatchError as exc:
            return {"found": False, "error": str(exc)}

    def _handle_list_workflows(self) -> dict:
        """List .robot files in scratch dir for inspection/replay."""
        workflows = []
        for f in sorted(self._scratch.glob("*.robot")):
            try:
                workflows.append({
                    "name": f.stem,
                    "path": str(f),
                    "size": f.stat().st_size,
                })
            except OSError:
                pass
        return {"workflows": workflows}

    def _run_loop(self) -> int:
        """Read JSON-RPC requests from stdin, write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = make_error("null", -32700, "Parse error")
                sys.stdout.write(resp.to_bytes().decode("utf-8"))
                sys.stdout.flush()
                continue

            req_id = msg.get("id", "null")
            method = msg.get("method", "")

            if method not in ALL_RPA_METHODS:
                resp = make_error(str(req_id), METHOD_NOT_FOUND, f"Unknown method: {method}")
                sys.stdout.write(resp.to_bytes().decode("utf-8"))
                sys.stdout.flush()
                continue

            try:
                result = self.handle_request(method, msg.get("params", {}))
                resp = JsonRpcResponse(id=str(req_id), result=result)
            except _InvalidParams as exc:
                resp = make_error(str(req_id), INVALID_PARAMS, str(exc))
            except _MethodNotFound as exc:
                resp = make_error(str(req_id), METHOD_NOT_FOUND, str(exc))
            except Exception as exc:  # noqa: BLE001
                # F-53 Scope A.P3: JSON-RPC top-level dispatch surfaces
                # every unhandled error as an INTERNAL_ERROR response
                # carrying str(exc). Controller-side sees + logs it.
                resp = make_error(str(req_id), INTERNAL_ERROR, str(exc))

            sys.stdout.write(resp.to_bytes().decode("utf-8"))
            sys.stdout.flush()

        return 0

    @classmethod
    def main(cls) -> int:
        """Subprocess entry point. Applies sandbox, then enters request loop."""
        scratch = os.environ.get("ICEBREAKER_RPA_SCRATCH", _DEFAULT_SCRATCH_DIR)

        if os.environ.get("ICEBREAKER_RPA_SKIP_SANDBOX") != "1":
            try:
                from .sandbox import apply_rpa_sandbox, SandboxError
                apply_rpa_sandbox(
                    home_dir=os.path.expanduser("~"),
                    scratch_dir=scratch,
                )
            except SandboxError as exc:
                print(f"RPA Bridge sandbox failed: {exc}", file=sys.stderr)
                return 1

        bridge = cls(scratch_dir=scratch)
        return bridge._run_loop()

    @classmethod
    def spawn_env(cls, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return the scrubbed environment for spawning the RPA Bridge subprocess."""
        return _scrubbed_rpa_env(extra=extra)


class _InvalidParams(Exception):
    pass


class _MethodNotFound(Exception):
    pass
