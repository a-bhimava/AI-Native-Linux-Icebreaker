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
from .workflow_gen import (
    WorkflowGenerator,
    WorkflowError,
    READ_ONLY_KEYWORDS,
    insert_auto_waits,
)
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
    # R2 advisory action-effect verification: "changed" = post-step screen
    # hash differs from previous step, "none" = a state-changing keyword
    # passed but the screen hash did not change, "unknown" = read-only
    # keyword / failed step / no hashes to compare. Never affects success.
    effect: str = "unknown"


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
    """Internal: raised by SIGALRM handler when workflow exceeds timeout.

    ``ROBOT_EXIT_ON_FAILURE`` makes Robot Framework abort the whole suite
    when this is raised inside a keyword (single-suite execution path) —
    ordinary keyword failures still continue to the next step, but a
    timeout hard-stops the run.
    """

    ROBOT_EXIT_ON_FAILURE = True


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
        # R2: previous step's screenshot hash, for effect verification.
        # Reset at the start of every workflow.
        self._prev_step_hash = ""
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
        auto_wait_seconds = float(params.get("auto_wait_seconds", 0.0))
        screenshot_policy = params.get("screenshot_policy", "all")

        try:
            validated = self._workflow_gen.validate_keywords(keywords)
        except WorkflowError as exc:
            return {
                "success": False,
                "error": str(exc),
                "reason": exc.reason,
                "keyword_name": exc.keyword_name,
            }

        note = ""
        if auto_wait_seconds > 0:
            expanded = insert_auto_waits(validated, auto_wait_seconds)
            inserted = len(expanded) - len(validated)
            if inserted:
                note = (
                    f"auto-wait: inserted {inserted} 'Wait Until Element Is "
                    f"Visible' step(s) at {auto_wait_seconds:g}s before "
                    "locator interactions"
                )
            validated = expanded

        # Audit artifact reflects the exact keyword list that executes
        # (including auto-inserted waits), with the transformation noted.
        self._workflow_gen.generate_robot_file(
            workflow_name, keywords, validated=validated, note=note,
        )

        keyword_results: list[KeywordResult] = []
        start_time = time.monotonic()
        total = len(validated)
        self._timed_out = False
        self._prev_step_hash = ""

        def _sigalrm_handler(signum: int, frame: Any) -> None:
            self._timed_out = True
            raise _WorkflowTimeout("workflow exceeded timeout")

        old_handler = signal.signal(signal.SIGALRM, _sigalrm_handler)
        # setitimer (not alarm) so fractional timeouts like 2.5s are honored
        # instead of being truncated to whole seconds.
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)

        try:
            if self._robot_available():
                self._run_workflow_suite(
                    validated, keyword_results,
                    start_time=start_time,
                    timeout_seconds=timeout_seconds,
                    screenshot_policy=screenshot_policy,
                )
            else:
                self._run_workflow_noop(
                    validated, keyword_results,
                    start_time=start_time,
                    timeout_seconds=timeout_seconds,
                    screenshot_policy=screenshot_policy,
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
                    "effect": kr.effect,
                }
                for kr in keyword_results
            ],
        }

    @staticmethod
    def _robot_available() -> bool:
        try:
            import robot  # noqa: F401
        except ImportError:
            return False
        return True

    def _run_workflow_suite(
        self,
        validated: list[tuple[str, list[str]]],
        keyword_results: list[KeywordResult],
        *,
        start_time: float,
        timeout_seconds: float,
        screenshot_policy: str,
    ) -> None:
        """Single-suite execution: one TestSuite, one test per keyword.

        One ``suite.run()`` pays Robot Framework startup once (the old code
        built and ran a fresh suite PER keyword) and lets GLOBAL/SUITE-scope
        library instances persist across keywords. One test per keyword
        preserves the old continue-on-failure semantics: a failing keyword
        marks its own test failed and execution moves to the next test.
        A listener (v3 API) records per-keyword telemetry; a workflow
        timeout aborts the whole suite via ``ROBOT_EXIT_ON_FAILURE``.
        """
        from robot.api import TestSuite

        suite = TestSuite(name="icebreaker_workflow")
        suite.resource.imports.library("SeleniumLibrary")
        for i, (name, args) in enumerate(validated):
            test = suite.tests.create(name=f"step_{i:03d}")
            test.body.create_keyword(name=name, args=args)

        bridge = self

        class _StepListener:
            ROBOT_LISTENER_API_VERSION = 3

            def __init__(self) -> None:
                self._kw_start = 0.0
                self._index = 0

            def start_test(self, data: Any, result: Any) -> None:
                self._kw_start = time.monotonic()

            def end_test(self, data: Any, result: Any) -> None:
                i = self._index
                self._index += 1
                if i >= len(validated):
                    return
                name, _args = validated[i]

                if bridge._timed_out:
                    # SIGALRM fired inside this keyword. Record one timeout
                    # row for the in-flight step; further end_test calls
                    # (exit-on-failure auto-fails remaining tests) no-op.
                    if not any(kr.status == "timeout" for kr in keyword_results):
                        keyword_results.append(KeywordResult(
                            index=i, name=name, status="timeout",
                            elapsed_ms=(time.monotonic() - self._kw_start) * 1000,
                            screenshot_hash="",
                            error="workflow timeout exceeded",
                        ))
                    return

                status = "pass" if result.passed else "fail"
                error = "" if result.passed else (result.message or "keyword failed")
                bridge._record_step(
                    keyword_results,
                    index=i, name=name, status=status, error=error,
                    kw_start=self._kw_start, start_time=start_time,
                    timeout_seconds=timeout_seconds,
                    total=len(validated),
                    screenshot_policy=screenshot_policy,
                )

        suite.run(
            output=None, log=None, report=None, listener=_StepListener(),
        )

    def _run_workflow_noop(
        self,
        validated: list[tuple[str, list[str]]],
        keyword_results: list[KeywordResult],
        *,
        start_time: float,
        timeout_seconds: float,
        screenshot_policy: str,
    ) -> None:
        """Per-keyword no-op loop for hosts without Robot Framework.

        Keeps dev/macOS behavior identical to before: keywords "pass"
        without doing anything, telemetry (screenshots, progress, effect)
        still flows so unit tests exercise the full recording path.
        """
        for i, (name, _args) in enumerate(validated):
            kw_start = time.monotonic()
            self._record_step(
                keyword_results,
                index=i, name=name, status="pass", error="",
                kw_start=kw_start, start_time=start_time,
                timeout_seconds=timeout_seconds,
                total=len(validated),
                screenshot_policy=screenshot_policy,
            )

    def _record_step(
        self,
        keyword_results: list[KeywordResult],
        *,
        index: int,
        name: str,
        status: str,
        error: str,
        kw_start: float,
        start_time: float,
        timeout_seconds: float,
        total: int,
        screenshot_policy: str,
    ) -> None:
        """Record one keyword's telemetry: screenshot, effect, progress."""
        elapsed_ms = (time.monotonic() - kw_start) * 1000

        capture = screenshot_policy == "all" or (
            screenshot_policy == "state_changing"
            and name not in READ_ONLY_KEYWORDS
        )
        screenshot_hash = self._capture_step_screenshot() if capture else ""
        effect = self._classify_effect(
            name=name, status=status,
            screenshot_hash=screenshot_hash, keyword_index=index,
        )

        keyword_results.append(KeywordResult(
            index=index, name=name, status=status,
            elapsed_ms=elapsed_ms,
            screenshot_hash=screenshot_hash,
            error=error,
            effect=effect,
        ))

        remaining = max(0, (timeout_seconds - (time.monotonic() - start_time)) * 1000)
        self._emit_progress(
            keyword_index=index,
            keyword_total=total,
            keyword_name=name,
            status=status,
            elapsed_ms=elapsed_ms,
            screenshot_hash=screenshot_hash,
            timeout_remaining_ms=remaining,
            effect=effect,
        )

    def _classify_effect(
        self,
        *,
        name: str,
        status: str,
        screenshot_hash: str,
        keyword_index: int,
    ) -> str:
        """R2 advisory action-effect verification.

        Compares the post-step screen hash against the previous step's.
        A state-changing keyword that "passed" while the screen stayed
        byte-identical is flagged ``"none"`` and surfaced via an
        ``rpa.no_effect`` notification — the top GUI-automation failure
        mode (blind actions) per the Phase 8 M8.2 design. Advisory only:
        never alters ``success``.
        """
        prev_hash = self._prev_step_hash
        if screenshot_hash:
            self._prev_step_hash = screenshot_hash

        if status != "pass" or name in READ_ONLY_KEYWORDS:
            return "unknown"
        if not screenshot_hash or not prev_hash:
            return "unknown"
        if screenshot_hash != prev_hash:
            return "changed"

        self._emit_notification("rpa.no_effect", {
            "keyword_index": keyword_index,
            "keyword_name": name,
            "screenshot_hash": screenshot_hash,
        })
        return "none"

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
        self._emit_notification("rpa.keyword_progress", kwargs)

    def _emit_notification(self, method: str, params: dict[str, Any]) -> None:
        """Emit a JSON-RPC notification on stdout."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
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
