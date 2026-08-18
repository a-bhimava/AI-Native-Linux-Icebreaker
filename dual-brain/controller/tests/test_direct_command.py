"""Regression locks for the strict PB-first offline command grammar."""

from __future__ import annotations

from controller.direct_command import parse_direct_command
from controller.turn_events import ResultEvent
from controller.tests.test_run_turn_from_intent import _build_ctrl


HOME = "/home/alice"


def _parse(command: str, cwd: str = "/home/alice/work"):
    return parse_direct_command(command, cwd=cwd, home=HOME)


def test_known_read_only_commands_have_fixed_actions():
    expected = {
        "# uptime": "system.uptime",
        "# df -h": "system.disk",
        "# free -h": "system.memory",
        "# ps": "process.list",
        "# ip addr": "network.status",
    }
    for command, action in expected.items():
        result = _parse(command)
        assert result is not None
        assert result.intent["action"] == action
        assert result.intent["target"] == ""


def test_ls_and_alias_resolve_to_authenticated_cwd_only():
    result = _parse("# ls")
    assert result is not None
    assert result.intent["action"] == "fs.list"
    assert result.intent["target"] == "/home/alice/work"

    alias = _parse("# what's in this folder")
    assert alias is not None
    assert alias.intent["target"] == "/home/alice/work"


def test_relative_path_stays_beneath_home():
    result = _parse("# cat notes.txt")
    assert result is not None
    assert result.intent["action"] == "fs.read"
    assert result.intent["target"] == "/home/alice/work/notes.txt"

    assert _parse("# ls ../../etc") is None
    assert _parse("# cat /etc/passwd") is None


def test_unsafe_or_generic_shell_is_not_an_offline_command():
    for command in (
        "# ls; whoami",
        "# ls | cat",
        "# cat $(pwd)/secret",
        "# sudo ls",
        "# rm -rf .",
        "# ls -la",
        "# echo hello",
        "# cat ~/secret",
    ):
        assert _parse(command) is None


def test_non_prefixed_input_never_enters_offline_lane():
    assert _parse("ls") is None


def test_offline_match_forces_pb_and_never_needs_qb_summary():
    """A local match remains useful when the cloud provider is unavailable."""
    match = _parse("# cat notes.txt")
    assert match is not None
    target = str(match.intent["target"])
    ctrl, qb, pb, mcpd, _audit, session = _build_ctrl(
        pb_content={"tool": "fs.read", "params": {"path": target}},
    )
    ctrl._cfg.run.tier0_fast_path = True

    events = list(ctrl.run_turn_from_intent(
        match.intent, session, source="offline_direct", force_pb=True,
        offline_direct=True,
    ))

    assert pb.complete.call_count == 1
    assert qb.complete.call_count == 0
    pb_user = pb.complete.call_args.kwargs["user"]
    assert "# cat notes.txt" not in pb_user
    assert "cat notes.txt" not in pb_user
    assert mcpd.call.call_count == 1
    result = [event.result for event in events if isinstance(event, ResultEvent)][-1]
    assert result.success is True
    assert result.output == '{"status": "ok"}'
