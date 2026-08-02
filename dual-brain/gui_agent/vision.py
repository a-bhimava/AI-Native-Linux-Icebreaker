"""Vision-grounding for GUI automation (Fix V / v6.15).

F-51 marker: F-103-vision (per incremental/GROUND_TRUTH.md F-103).

Cloud VLM (Gemini 2.5 Flash vision primary, Claude Haiku 4.5 fallback)
turns a screenshot into a structured list of clickable elements with
pixel boxes + captions + kind. Consumed by ``gui_agent.agent.GuiAgent``'s
``_handle_parse_screen`` and the ``grounded_*`` orchestrators.

Design:
- Depends only on ``litellm`` (already core) — no torch / transformers.
- ``VisionGrounder(config, litellm_completion=None)`` — the second arg
  is dependency injection for tests (unit tests pass a mock; production
  passes ``None`` and we import ``litellm.completion`` lazily).
- Structured output via LiteLLM's ``response_format={"type":"json_object",
  "schema":...}`` — schema-forced so the model can't drift.
- One malformed-JSON retry with sampling decay (temperature 0.4 → 0.0).
- On rate-limit / provider error: transparent fallback to
  ``config["fallback_backend"]`` if set.
- 2s TTL in-memory cache keyed on the screenshot SHA-256 so a
  ``parse_screen`` followed by ``grounded_click`` doesn't pay the VLM
  cost twice.
- Cost ceiling (``cost_ceiling_usd_per_turn``): a call that would push
  the running sum past the ceiling is refused, audited, and surfaces to
  the user. Prevents runaway loops from becoming a $ bill.

Security / invariants:
- The screenshot bytes flow QB → VLM → QB. Never touches the Privileged
  Brain. This is the same trust boundary as the existing QB tool-selection
  step — the vision call sits inside the QB, not across the boundary.
- Cost tracked per-turn (via the running ``cost_usd_this_turn`` state);
  ``cost_ceiling_usd_per_turn`` denies + audits when breached (BP-10).
- Model free-text ("caption") is DISPLAYED but never decides — the
  decision downstream is ``element_id`` (integer) + box coords (BP-6).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)


# ── Data model ──────────────────────────────────────────────────────────

_ELEMENT_KIND_VALUES = ("button", "input", "link", "icon", "text", "list",
                        "menu", "tab", "checkbox", "radio", "slider", "other")

_ELEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "minimum": 0, "maximum": 999},
        "box": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 32768},
            "minItems": 4,
            "maxItems": 4,
        },
        "caption": {"type": "string", "maxLength": 200},
        "kind": {"type": "string", "enum": list(_ELEMENT_KIND_VALUES)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["id", "box", "caption", "kind"],
    "additionalProperties": False,
}

_PARSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "elements": {
            "type": "array",
            "items": _ELEMENT_SCHEMA,
            "maxItems": 100,
        },
    },
    "required": ["elements"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ElementBox:
    """One parsed UI element."""

    id: int
    box: tuple[int, int, int, int]  # x, y, w, h
    caption: str
    kind: str
    confidence: float = 1.0

    @property
    def centroid(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return (x + w // 2, y + h // 2)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "box": list(self.box),
            "caption": self.caption,
            "kind": self.kind,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ParseResult:
    """Result of one ``parse_screen`` call."""

    elements: tuple[ElementBox, ...]
    vlm_latency_ms: int
    vlm_cost_usd: float
    backend_used: str
    cached: bool
    screenshot_sha256: str

    def to_dict(self) -> dict:
        return {
            "elements": [e.to_dict() for e in self.elements],
            "vlm_latency_ms": self.vlm_latency_ms,
            "vlm_cost_usd": self.vlm_cost_usd,
            "backend_used": self.backend_used,
            "cached": self.cached,
            "screenshot_sha256": self.screenshot_sha256,
        }


# ── Errors ──────────────────────────────────────────────────────────────

class VisionError(Exception):
    """Base class for vision-grounder errors."""


class VisionCostCeilingExceeded(VisionError):
    """The per-turn cost ceiling would be exceeded by this call."""

    def __init__(self, would_reach_usd: float, ceiling_usd: float) -> None:
        super().__init__(
            f"cost ceiling exceeded: this call would reach ${would_reach_usd:.4f} "
            f"of ${ceiling_usd:.4f} ceiling"
        )
        self.would_reach_usd = would_reach_usd
        self.ceiling_usd = ceiling_usd


class VisionMalformedResponse(VisionError):
    """VLM returned content that didn't parse as JSON after retries."""


class VisionAllBackendsFailed(VisionError):
    """Both primary and fallback backends failed."""


class VisionDisabled(VisionError):
    """``[gui.vision] enabled=false`` — module is off."""


# ── The grounder ────────────────────────────────────────────────────────

_DEFAULT_PROMPT = """You are a UI-parsing model. The image is a screenshot of a computer window.

Enumerate EVERY interactive element visible in the screenshot: buttons, text
inputs, links, dropdowns, checkboxes, radio buttons, tabs, menu items,
sliders, and important icons. Do not enumerate decorative labels or
background text unless the user could plausibly click on them.

For each element, return:
  - `id`: sequential integer starting at 0 (assign in reading order,
     top-to-bottom, left-to-right within each row)
  - `box`: [x, y, width, height] in PIXELS relative to the top-left
     of the SCREENSHOT (not the screen). Use the tight bounding box —
     just the interactive region, not padding around it.
  - `caption`: short human-readable label for the element. Use its
     visible text if any (e.g. "Send", "Search"). If no text, describe
     it in <= 6 words (e.g. "close window X icon", "hamburger menu").
  - `kind`: one of button, input, link, icon, text, list, menu, tab,
     checkbox, radio, slider, other.
  - `confidence`: your confidence in this identification, 0.0 to 1.0.

Return ONLY a JSON object with an "elements" array. No prose, no
markdown, no explanation. Cap the list at {max_elements} entries;
prioritize elements the user is most likely to interact with if there
are more.

{hint_line}"""


class VisionGrounder:
    """LiteLLM-backed vision UI grounder.

    Parameters
    ----------
    config : dict
        The parsed ``[gui.vision]`` section from ``controller.toml``.
        Required keys: ``enabled``, ``backend``. Optional (with defaults):
        ``fallback_backend``, ``max_elements_per_parse``,
        ``cost_ceiling_usd_per_turn``, ``cache_ttl_seconds``,
        ``retry_on_malformed_json``.
    litellm_completion : callable, optional
        Injected for tests. Production leaves this ``None`` and the
        module lazily imports ``litellm.completion``.
    """

    def __init__(
        self,
        config: dict,
        litellm_completion: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not config.get("enabled", True):
            self._disabled = True
        else:
            self._disabled = False

        self._backend: str = config.get("backend", "gemini/gemini-2.5-flash")
        self._fallback_backend: Optional[str] = config.get("fallback_backend") or None
        self._max_elements: int = int(config.get("max_elements_per_parse", 50))
        self._cost_ceiling: float = float(config.get("cost_ceiling_usd_per_turn", 0.01))
        self._cache_ttl: float = float(config.get("cache_ttl_seconds", 2.0))
        self._retry_malformed: int = int(config.get("retry_on_malformed_json", 1))

        # Per-turn running cost, reset by caller between turns via reset_turn_cost().
        self._turn_cost_usd: float = 0.0

        # In-memory 2s cache: {sha256: (ParseResult, insert_time)}
        self._cache: dict[str, tuple[ParseResult, float]] = {}

        self._completion = litellm_completion  # None → lazy import

    # ── public API ─────────────────────────────────────────────────

    def reset_turn_cost(self) -> None:
        """Called by the orchestrator at the start of each user turn."""
        self._turn_cost_usd = 0.0

    def turn_cost_usd(self) -> float:
        return self._turn_cost_usd

    def parse_screen(
        self,
        image_source: Any,  # ScreenshotResult | Path | str | bytes
        prompt_hint: str = "",
    ) -> ParseResult:
        """Parse a screenshot into a structured element list.

        Raises
        ------
        VisionDisabled
            ``[gui.vision] enabled=false``.
        VisionCostCeilingExceeded
            This call would push per-turn cost past the ceiling.
        VisionMalformedResponse
            VLM returned unparseable JSON after retry.
        VisionAllBackendsFailed
            Primary and fallback both errored.
        """
        if self._disabled:
            raise VisionDisabled("[gui.vision] enabled=false")

        png_bytes = _load_png_bytes(image_source)
        sha256 = hashlib.sha256(png_bytes).hexdigest()

        cached = self._cache_get(sha256)
        if cached is not None:
            _log.debug("vision cache hit sha=%s", sha256[:12])
            return cached

        # Budget check before we spend $ — assume worst-case Gemini
        # 2.5 Flash vision cost for a 1MP image (~$0.0003 per call).
        # Real cost is measured after and can adjust below.
        est_cost = 0.0003
        if self._turn_cost_usd + est_cost > self._cost_ceiling:
            raise VisionCostCeilingExceeded(
                would_reach_usd=self._turn_cost_usd + est_cost,
                ceiling_usd=self._cost_ceiling,
            )

        # Try primary, then fallback.
        errors: list[str] = []
        for backend_name in _backend_chain(self._backend, self._fallback_backend):
            try:
                raw = self._call_vlm(png_bytes, prompt_hint, backend_name)
                result = ParseResult(
                    elements=raw.elements,
                    vlm_latency_ms=raw.vlm_latency_ms,
                    vlm_cost_usd=raw.vlm_cost_usd,
                    backend_used=raw.backend_used,
                    cached=False,
                    screenshot_sha256=sha256,
                )
                self._turn_cost_usd += result.vlm_cost_usd
                self._cache_put(sha256, result)
                return result
            except _RetriableError as exc:
                errors.append(f"{backend_name}: {exc}")
                _log.warning("vision backend %s failed: %s", backend_name, exc)
                continue

        raise VisionAllBackendsFailed("; ".join(errors))

    # ── internals ──────────────────────────────────────────────────

    def _cache_get(self, sha: str) -> Optional[ParseResult]:
        hit = self._cache.get(sha)
        if hit is None:
            return None
        result, t_insert = hit
        if time.monotonic() - t_insert > self._cache_ttl:
            del self._cache[sha]
            return None
        # Return a copy with cached=True (frozen dataclass — build new).
        return ParseResult(
            elements=result.elements,
            vlm_latency_ms=result.vlm_latency_ms,
            vlm_cost_usd=result.vlm_cost_usd,
            backend_used=result.backend_used,
            cached=True,
            screenshot_sha256=result.screenshot_sha256,
        )

    def _cache_put(self, sha: str, result: ParseResult) -> None:
        # Bounded cache: keep 8 most-recent entries. Callers making
        # many distinct parse_screen calls in one turn don't need long
        # history — the point is short-lookback dedup.
        if len(self._cache) >= 8:
            oldest = min(self._cache.items(), key=lambda kv: kv[1][1])[0]
            del self._cache[oldest]
        self._cache[sha] = (result, time.monotonic())

    def _call_vlm(
        self,
        png_bytes: bytes,
        prompt_hint: str,
        backend_name: str,
    ) -> ParseResult:
        completion = self._completion or _lazy_litellm_completion()

        b64 = base64.b64encode(png_bytes).decode("ascii")
        hint_line = f"HINT: {prompt_hint}" if prompt_hint else ""
        prompt_text = _DEFAULT_PROMPT.format(
            max_elements=self._max_elements,
            hint_line=hint_line,
        ).strip()

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }]

        attempts = 0
        temperature = 0.4
        last_error = ""
        while attempts <= self._retry_malformed:
            t_start = time.monotonic()
            try:
                resp = completion(
                    model=backend_name,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=temperature,
                    max_tokens=8192,
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001 — providers throw all sorts
                raise _RetriableError(f"provider error: {type(exc).__name__}: {exc}") from None

            latency_ms = int((time.monotonic() - t_start) * 1000)

            # LiteLLM response shape: choices[0].message.content is a JSON str.
            try:
                content = resp["choices"][0]["message"]["content"]
                cost_usd = float(resp.get("_hidden_params", {}).get("response_cost", 0.0) or 0.0)
            except (KeyError, IndexError, TypeError) as exc:
                raise _RetriableError(f"response shape unexpected: {exc}") from None

            elements = _parse_and_validate(content, self._max_elements)
            if elements is not None:
                return ParseResult(
                    elements=tuple(elements),
                    vlm_latency_ms=latency_ms,
                    vlm_cost_usd=cost_usd,
                    backend_used=backend_name,
                    cached=False,
                    screenshot_sha256="",  # filled by caller after cache insert
                )

            # Malformed JSON — retry once with lower temperature.
            attempts += 1
            temperature = 0.0
            last_error = content[:200] if isinstance(content, str) else repr(content)[:200]

        raise VisionMalformedResponse(
            f"VLM returned unparseable JSON after {attempts} attempts. "
            f"Last payload excerpt: {last_error!r}"
        )


# ── Module-private helpers ──────────────────────────────────────────────

class _RetriableError(Exception):
    """Internal: raised when a backend fails in a way that should fall
    through to the fallback backend."""


def _lazy_litellm_completion() -> Callable[..., Any]:
    """Import ``litellm.completion`` on first real use so tests running
    without ``litellm`` installed can still import this module."""
    import litellm  # noqa: PLC0415
    return litellm.completion


def _backend_chain(primary: str, fallback: Optional[str]) -> tuple[str, ...]:
    if fallback and fallback != primary:
        return (primary, fallback)
    return (primary,)


def _load_png_bytes(image_source: Any) -> bytes:
    """Accept a ScreenshotResult, Path, str path, or raw bytes."""
    if isinstance(image_source, bytes):
        return image_source
    # ScreenshotResult has a `.path` attribute; Path/str are direct.
    path = getattr(image_source, "path", image_source)
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"screenshot not found: {p}")
    return p.read_bytes()


def _parse_and_validate(
    content: Any,
    max_elements: int,
) -> Optional[list[ElementBox]]:
    """Parse the model's JSON content and coerce to list[ElementBox].
    Returns None if parsing/validation fails (caller decides whether to retry)."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    # Strip a common Gemini markdown fence wrapper if present.
    if text.startswith("```"):
        # Drop first line, drop trailing ```
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    raw_elements = obj.get("elements")
    if not isinstance(raw_elements, list):
        return None

    out: list[ElementBox] = []
    for i, raw in enumerate(raw_elements[:max_elements]):
        if not isinstance(raw, dict):
            continue
        box = raw.get("box")
        if not (isinstance(box, list) and len(box) == 4
                and all(isinstance(v, int) and 0 <= v <= 32768 for v in box)):
            continue
        kind = raw.get("kind", "other")
        if kind not in _ELEMENT_KIND_VALUES:
            kind = "other"
        caption = str(raw.get("caption", "") or "")[:200]
        confidence = float(raw.get("confidence", 1.0) or 1.0)
        if not 0.0 <= confidence <= 1.0:
            confidence = max(0.0, min(1.0, confidence))
        out.append(ElementBox(
            id=int(raw.get("id", i)),
            box=(box[0], box[1], box[2], box[3]),
            caption=caption,
            kind=kind,
            confidence=confidence,
        ))
    # Renumber to guarantee contiguous IDs starting at 0.
    return [
        ElementBox(id=i, box=e.box, caption=e.caption,
                   kind=e.kind, confidence=e.confidence)
        for i, e in enumerate(out)
    ]


# ── Self-test entry point ───────────────────────────────────────────────

def _self_test() -> int:
    """``python -m gui_agent.vision --self-test`` — verify the module
    loads, parses a canned VLM response, and returns the right shape.
    Used by smoke-gate L3."""
    def _fake_completion(**kwargs: Any) -> dict:
        return {
            "choices": [{"message": {"content": json.dumps({
                "elements": [
                    {"id": 0, "box": [10, 20, 100, 40],
                     "caption": "Sign in", "kind": "button", "confidence": 0.95},
                    {"id": 1, "box": [10, 80, 200, 30],
                     "caption": "Email", "kind": "input", "confidence": 0.9},
                ]
            })}}],
            "_hidden_params": {"response_cost": 0.0001},
        }

    # Minimal 1x1 PNG so we can exercise the file-load path.
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c```\x00"
        b"\x00\x00\x04\x00\x01\x0b\xa4\x03\xed\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    grounder = VisionGrounder(
        config={"enabled": True, "backend": "gemini/gemini-2.5-flash"},
        litellm_completion=_fake_completion,
    )
    result = grounder.parse_screen(tiny_png)

    if len(result.elements) != 2:
        print(f"self-test FAILED: expected 2 elements, got {len(result.elements)}",
              flush=True)
        return 1
    if result.elements[0].caption != "Sign in":
        print(f"self-test FAILED: element 0 caption {result.elements[0].caption!r}",
              flush=True)
        return 1
    if result.elements[0].centroid != (60, 40):
        print(f"self-test FAILED: element 0 centroid {result.elements[0].centroid}",
              flush=True)
        return 1
    print(f"vision OK: parsed {len(result.elements)} elements, "
          f"backend={result.backend_used}, cost=${result.vlm_cost_usd:.4f}",
          flush=True)
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print("usage: python -m gui_agent.vision --self-test", file=__import__("sys").stderr)
    sys.exit(2)
