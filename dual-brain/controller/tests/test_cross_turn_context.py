"""v6.9 Task #149 shipping-scope — cross-turn context.

Verifies the QB context preamble gains a `recent_turns:` block once
a session has ≥ 1 completed turn, so Turn N can reference Turn N-1's
tool output (user ask 2026-07-17: "run the second part of the command
after seeing the output of the first").

Coverage:
  - SessionState.render_recent_turns pairs each user query with the
    following tool_result_summary; skips synthetic assistant messages.
  - Empty history → empty string (backward-compat, first turn).
  - max_turns=0 → disabled (BP-2 opt-out).
  - _build_qb_input inserts the block INSIDE the <context> ... </context>
    tags so QB prompts (which key off <context>) see it.
  - Line lengths respected: individual query/result cleaned to 200
    chars; total block trimmed to max_chars.
  - Pronoun-resolution reproducer: Turn 1 ('what is my IP' → tool
    summary 'Your IP is 192.168.64.31'), Turn 2 QB context contains
    'IP is 192.168.64.31' — QB can now resolve 'save it to a file'.
"""
from __future__ import annotations

from types import SimpleNamespace

from controller.session import SessionState, ShellContext


def _mk_session(**cfg_kwargs):
    """Build a SessionState with the minimum cfg surface Task #149
    exercises. Real SessionConfig via config._build_session_config
    is heavier than we need for this test."""
    cfg = SimpleNamespace(
        session_ttl_seconds=0,
        max_turns=50,
        undo=SimpleNamespace(max_history=0),
        max_shell_context_chars=512,
        max_recent_commands=5,
        recent_turns_count=cfg_kwargs.get("recent_turns_count", 3),
        recent_turns_max_chars=cfg_kwargs.get("recent_turns_max_chars", 800),
    )
    s = SessionState(session_id="s-1", backend="gemini", cfg=cfg)
    return s


def test_render_recent_turns_empty_on_fresh_session():
    s = _mk_session()
    assert s.render_recent_turns() == ""


def test_render_recent_turns_skips_incomplete_first_query():
    """A user query with no tool result yet is not a useful record."""
    s = _mk_session()
    s.add_user_message("what is my IP")
    # No add_tool_result_summary yet → no complete pair.
    assert s.render_recent_turns() == ""


def test_render_recent_turns_single_complete_pair():
    s = _mk_session()
    s.add_user_message("what is my IP address")
    s.add_assistant_message('{"action":"network.status"}')  # noise, should skip
    s.add_tool_result_summary("Your IP is 192.168.64.31")
    block = s.render_recent_turns()
    assert "recent_turns:" in block
    assert "T-1 query: what is my IP address" in block
    assert "result: Your IP is 192.168.64.31" in block


def test_render_recent_turns_orders_newest_first():
    s = _mk_session()
    s.add_user_message("show my cpu")
    s.add_tool_result_summary("CPU is 0.0%")
    s.add_user_message("show my memory")
    s.add_tool_result_summary("Memory 4 GB free")
    block = s.render_recent_turns()
    # Most recent turn labeled T-1
    lines = block.splitlines()
    header_idx = lines.index("recent_turns:")
    assert "T-1 query: show my memory" in lines[header_idx + 1]
    assert "T-2 query: show my cpu" in lines[header_idx + 3]


def test_render_recent_turns_respects_max_turns():
    s = _mk_session()
    for i in range(5):
        s.add_user_message(f"query {i}")
        s.add_tool_result_summary(f"result {i}")
    block = s.render_recent_turns(max_turns=2)
    assert "T-1 query: query 4" in block
    assert "T-2 query: query 3" in block
    assert "T-3" not in block
    assert "query 0" not in block


def test_render_recent_turns_disabled_when_zero():
    s = _mk_session()
    s.add_user_message("x")
    s.add_tool_result_summary("y")
    assert s.render_recent_turns(max_turns=0) == ""


def test_render_recent_turns_caps_total_length():
    s = _mk_session()
    for i in range(10):
        s.add_user_message(f"long query {i}: " + "x" * 100)
        s.add_tool_result_summary(f"long result {i}: " + "y" * 100)
    block = s.render_recent_turns(max_turns=10, max_chars=200)
    assert len(block) <= 200
    # Trimming marker present when block was longer than cap.
    assert block.endswith("…")


def test_shell_context_render_inserts_recent_turns_block():
    """The <context> XML preamble gains the recent_turns block when the
    caller (main.py::_build_qb_input) passes it in."""
    ctx = ShellContext(cwd="/home/icebreaker", user="icebreaker")
    block = "recent_turns:\n  T-1 query: hi\n       result: hello"
    rendered = ctx.render(recent_turns_block=block)
    assert rendered.startswith("<context>")
    assert rendered.endswith("</context>")
    assert "recent_turns:" in rendered
    assert "T-1 query: hi" in rendered
    # cwd / user still there
    assert "cwd: /home/icebreaker" in rendered
    assert "user: icebreaker" in rendered


def test_shell_context_render_omits_block_when_empty():
    ctx = ShellContext(cwd="/x")
    rendered = ctx.render(recent_turns_block="")
    assert "recent_turns:" not in rendered
    assert "cwd: /x" in rendered


def test_reproducer_pronoun_resolution_context_contains_prior_ip():
    """v6.9 Task #149 reproducer: after Turn 1 answers 'what is my IP',
    Turn 2's context contains the IP so QB can resolve 'save it'."""
    s = _mk_session()
    s.add_user_message("what is my IP")
    s.add_assistant_message('{"action":"network.status"}')  # noise
    s.add_tool_result_summary("Your IP is 192.168.64.31")
    turn2_context = s.render_recent_turns()
    # The literal IP is in the context so QB can emit
    # fs.write content="192.168.64.31" for the follow-up "save it".
    assert "192.168.64.31" in turn2_context
    assert "what is my IP" in turn2_context
