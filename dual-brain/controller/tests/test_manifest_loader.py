"""v6.9 Scope O Layer 2 Part A — manifest loader + session_op tests.

Locks in:
  1. Well-formed manifests load and register.
  2. Malformed manifests fail at load time with a clear reason (INV-4
     analogue for the manifest layer itself — a bad manifest never
     silently disables a tool or, worse, exposes a broken sandbox).
  3. session_op::set_cwd mutates SessionState.session_cwd and returns
     the resolved path as stdout ($STEP_N_STDOUT-friendly).
  4. Registry.dispatch validates params against the manifest's own
     param_schema (INV-4 gate for tool params).
  5. Unknown impl.kind raises rather than silently no-oping.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from controller.impl_kinds import DispatchContext, ImplResult
from controller.impl_kinds import session_op
from controller.manifest_loader import ManifestEntry, ManifestRegistry, load


# ─── Loader — happy path ──────────────────────────────────────────────────

def test_load_shipped_manifests_finds_navcd() -> None:
    reg = load()
    assert reg.has("nav.cd"), (
        f"nav.cd must be shipped in controller/manifests/; got {reg.names()}"
    )
    entry = reg.get("nav.cd")
    assert entry.kind == "session_op"
    assert entry.manifest["impl"]["op"] == "set_cwd"
    assert entry.manifest["tier"] == 0


# ─── Loader — validation failures ─────────────────────────────────────────

def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    _write(tmp_path, "bad", """
        name: bad.thing
        version: 1
        tier: 0
        param_schema: {type: object}
        impl:
          kind: session_op
          op: set_cwd
    """)
    with pytest.raises(ValueError, match="'description' is a required property"):
        load(manifests_dir=tmp_path)


def test_load_rejects_bad_name_pattern(tmp_path: Path) -> None:
    _write(tmp_path, "bad", """
        name: NotDotted
        version: 1
        description: "test"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: session_op
          op: set_cwd
    """)
    with pytest.raises(ValueError, match="does not match"):
        load(manifests_dir=tmp_path)


def test_load_rejects_unknown_impl_kind(tmp_path: Path) -> None:
    _write(tmp_path, "bad", """
        name: bad.thing
        version: 1
        description: "test"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: eval_python
          op: whatever
    """)
    with pytest.raises(ValueError, match="'eval_python' is not one of"):
        load(manifests_dir=tmp_path)


def test_load_rejects_unknown_session_op(tmp_path: Path) -> None:
    _write(tmp_path, "bad", """
        name: bad.thing
        version: 1
        description: "test"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: session_op
          op: nonexistent_op
    """)
    with pytest.raises(ValueError, match="no session_op handler"):
        load(manifests_dir=tmp_path)


def test_load_rejects_duplicate_name(tmp_path: Path) -> None:
    body = """
        name: dup.thing
        version: 1
        description: "test"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: session_op
          op: set_cwd
    """
    _write(tmp_path, "a", body)
    _write(tmp_path, "b", body)
    with pytest.raises(ValueError, match="duplicate name 'dup.thing'"):
        load(manifests_dir=tmp_path)


def test_load_rejects_malformed_yaml(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML parse error"):
        load(manifests_dir=tmp_path)


def test_load_missing_dir_returns_empty_registry(tmp_path: Path) -> None:
    reg = load(manifests_dir=tmp_path / "does_not_exist")
    assert reg.names() == []


# ─── session_op::set_cwd ──────────────────────────────────────────────────

def _mk_ctx(session_id: str = "sess-test") -> tuple[DispatchContext, SimpleNamespace]:
    """SessionState-shaped stub. session_cwd starts empty; the impl
    mutates it in place."""
    state = SimpleNamespace(session_id=session_id, session_cwd="")
    return DispatchContext(session=state), state


def test_set_cwd_updates_session_state(tmp_path: Path) -> None:
    reg = load()
    ctx, state = _mk_ctx()
    result = reg.dispatch("nav.cd", {"path": str(tmp_path)}, ctx)
    assert isinstance(result, ImplResult)
    assert state.session_cwd == str(tmp_path.resolve())
    assert result.stdout == str(tmp_path.resolve())
    assert result.metadata["old_cwd"] == ""
    assert result.metadata["new_cwd"] == str(tmp_path.resolve())


def test_set_cwd_records_old_cwd(tmp_path: Path) -> None:
    reg = load()
    ctx, state = _mk_ctx()
    state.session_cwd = "/prev"
    result = reg.dispatch("nav.cd", {"path": str(tmp_path)}, ctx)
    assert result.metadata["old_cwd"] == "/prev"
    assert result.metadata["new_cwd"] == str(tmp_path.resolve())


def test_set_cwd_rejects_nonexistent_path() -> None:
    reg = load()
    ctx, _ = _mk_ctx()
    with pytest.raises(ValueError, match="cannot resolve"):
        reg.dispatch("nav.cd", {"path": "/nonexistent/definitely/not/here"}, ctx)


def test_set_cwd_rejects_relative_path(tmp_path: Path) -> None:
    reg = load()
    ctx, _ = _mk_ctx()
    with pytest.raises(ValidationError):
        # param_schema pattern requires ^/ so it fires before session_op sees it.
        reg.dispatch("nav.cd", {"path": "relative/path"}, ctx)


def test_set_cwd_rejects_file_not_directory(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")
    reg = load()
    ctx, _ = _mk_ctx()
    with pytest.raises(ValueError, match="is not a directory"):
        reg.dispatch("nav.cd", {"path": str(f)}, ctx)


def test_set_cwd_rejects_missing_path_param() -> None:
    reg = load()
    ctx, _ = _mk_ctx()
    with pytest.raises(ValidationError):
        # Meta-schema on the manifest requires params.path.
        reg.dispatch("nav.cd", {}, ctx)


# ─── Registry dispatch surface ────────────────────────────────────────────

def test_registry_get_missing_raises() -> None:
    reg = load()
    with pytest.raises(KeyError):
        reg.get("does.not.exist")


def test_registry_dispatch_unknown_tool_raises() -> None:
    reg = load()
    ctx, _ = _mk_ctx()
    with pytest.raises(KeyError):
        reg.dispatch("does.not.exist", {}, ctx)


def test_registry_dispatch_unknown_kind_raises_not_implemented(tmp_path: Path) -> None:
    """A manifest that would use fs_read (Part B kind, no dispatcher yet)
    should refuse at dispatch — not silently no-op."""
    (tmp_path / "future.yaml").write_text(textwrap.dedent("""
        name: future.tool
        version: 1
        description: "Part B kind"
        tier: 0
        param_schema: {type: object}
        impl:
          kind: fs_read
    """), encoding="utf-8")
    reg = load(manifests_dir=tmp_path)
    ctx, _ = _mk_ctx()
    with pytest.raises(NotImplementedError, match="fs_read"):
        reg.dispatch("future.tool", {}, ctx)
