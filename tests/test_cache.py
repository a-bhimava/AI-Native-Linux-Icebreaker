"""Tests for shell/pb_cache.py — semantic cache (Architecture 4)."""

import sys
import os
import tempfile
from pathlib import Path
import pytest

# Point the cache at a temp DB so we don't pollute the user's real cache
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()

# Patch DB_PATH before importing the module
import importlib
sys.path.insert(0, str(Path(__file__).parent.parent / "shell"))

import pb_cache
pb_cache.DB_PATH = Path(_TMP_DB.name)


def teardown_module(_):
    Path(_TMP_DB.name).unlink(missing_ok=True)


# ── normalize_intent ──────────────────────────────────────────────────────────

class TestNormalizeIntent:
    def test_lowercases(self):
        assert pb_cache.normalize_intent("Show Disk Usage") == pb_cache.normalize_intent("show disk usage")

    def test_strips_filler_words(self):
        norm = pb_cache.normalize_intent("please show me the disk usage")
        assert "please" not in norm
        assert "show" not in norm or "list" in norm  # show → list synonym

    def test_synonym_folding_restart(self):
        a = pb_cache.normalize_intent("restart nginx")
        b = pb_cache.normalize_intent("reload nginx")
        c = pb_cache.normalize_intent("bounce nginx")
        assert a == b == c

    def test_synonym_folding_delete(self):
        a = pb_cache.normalize_intent("remove the file foo.txt")
        b = pb_cache.normalize_intent("delete the file foo.txt")
        assert a == b

    def test_synonym_folding_list(self):
        a = pb_cache.normalize_intent("show all processes")
        b = pb_cache.normalize_intent("list all processes")
        c = pb_cache.normalize_intent("display all processes")
        assert a == b == c

    def test_empty_string(self):
        assert pb_cache.normalize_intent("") == ""


# ── cmd_set / cmd_get (exact match) ──────────────────────────────────────────

class TestExactMatch:
    def setup_method(self):
        pb_cache.cmd_clear()

    def test_set_and_get(self):
        pb_cache.cmd_set("list all docker containers", "docker ps -a")
        out = []
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as buf:
            rc = pb_cache.cmd_get("list all docker containers")
        assert rc == 0
        assert "docker ps -a" in buf.getvalue()

    def test_miss_returns_1(self):
        rc = pb_cache.cmd_get("this query is definitely not cached xyz123")
        assert rc == 1

    def test_verified_flag_stored(self):
        pb_cache.cmd_set("restart nginx service", "systemctl restart nginx", verified=True)
        con = pb_cache._connect()
        row = con.execute(
            "SELECT verified FROM cache WHERE query_nl=?", ("restart nginx service",)
        ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_unverified_by_default(self):
        pb_cache.cmd_set("show memory usage", "free -h")
        con = pb_cache._connect()
        row = con.execute(
            "SELECT verified FROM cache WHERE query_nl=?", ("show memory usage",)
        ).fetchone()
        assert row is not None
        assert row[0] == 0

    def test_set_overwrites_command(self):
        pb_cache.cmd_set("show disk usage", "df -h")
        pb_cache.cmd_set("show disk usage", "df -Th")
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as buf:
            pb_cache.cmd_get("show disk usage")
        assert "df -Th" in buf.getvalue()

    def test_verified_flag_ratchets_up(self):
        pb_cache.cmd_set("show open ports", "ss -tlnp", verified=False)
        pb_cache.cmd_set("show open ports", "ss -tlnp", verified=True)
        con = pb_cache._connect()
        row = con.execute(
            "SELECT verified FROM cache WHERE query_nl=?", ("show open ports",)
        ).fetchone()
        assert row[0] == 1


# ── cmd_stats ─────────────────────────────────────────────────────────────────

class TestStats:
    def setup_method(self):
        pb_cache.cmd_clear()

    def test_stats_output(self, capsys):
        pb_cache.cmd_set("list users", "cut -d: -f1 /etc/passwd")
        pb_cache.cmd_get("list users")
        pb_cache.cmd_get("nonexistent query abc")
        pb_cache.cmd_stats()
        out = capsys.readouterr().out
        assert "Cache entries" in out
        assert "Exact hits" in out
        assert "Cache misses" in out
