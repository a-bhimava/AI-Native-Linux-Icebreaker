#!/usr/bin/env python3
"""
pb_cache.py — SQLite cache for Privileged Brain NL→command queries.

Usage:
  python3 pb_cache.py get "show all docker containers"   → prints cached cmd or nothing
  python3 pb_cache.py set "show all docker containers" "docker ps -a"
  python3 pb_cache.py list                               → show top 20 most-used
  python3 pb_cache.py stats                              → hit/miss stats
  python3 pb_cache.py clear                              → wipe cache
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path.home() / ".pb_cache.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            query_hash  TEXT PRIMARY KEY,
            query_nl    TEXT NOT NULL,
            command     TEXT NOT NULL,
            hits        INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            last_used   REAL NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("INSERT OR IGNORE INTO stats VALUES ('hits', 0)")
    con.execute("INSERT OR IGNORE INTO stats VALUES ('misses', 0)")
    con.commit()
    return con


def _hash(query: str) -> str:
    return hashlib.sha256(query.lower().strip().encode()).hexdigest()


def cmd_get(query: str) -> int:
    con = _connect()
    h = _hash(query)
    row = con.execute("SELECT command FROM cache WHERE query_hash=?", (h,)).fetchone()
    if row:
        now = time.time()
        con.execute(
            "UPDATE cache SET hits=hits+1, last_used=? WHERE query_hash=?",
            (now, h),
        )
        con.execute("UPDATE stats SET value=value+1 WHERE key='hits'")
        con.commit()
        print(row[0])
        return 0
    con.execute("UPDATE stats SET value=value+1 WHERE key='misses'")
    con.commit()
    return 1


def cmd_set(query: str, command: str) -> None:
    con = _connect()
    h = _hash(query)
    now = time.time()
    con.execute("""
        INSERT INTO cache (query_hash, query_nl, command, hits, created_at, last_used)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET
            command=excluded.command,
            hits=hits+1,
            last_used=excluded.last_used
    """, (h, query.strip(), command.strip(), now, now))
    con.commit()


def cmd_list() -> None:
    con = _connect()
    rows = con.execute(
        "SELECT query_nl, command, hits, last_used FROM cache ORDER BY hits DESC LIMIT 20"
    ).fetchall()
    if not rows:
        print("Cache is empty.")
        return
    print(f"{'Hits':>5}  {'Last used':>20}  {'Query → Command'}")
    print("-" * 80)
    for nl, cmd, hits, last_used in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_used))
        preview = f"{nl[:28]!r} → {cmd[:30]!r}"
        print(f"{hits:>5}  {ts:>20}  {preview}")


def cmd_stats() -> None:
    con = _connect()
    hits = con.execute("SELECT value FROM stats WHERE key='hits'").fetchone()[0]
    misses = con.execute("SELECT value FROM stats WHERE key='misses'").fetchone()[0]
    total = hits + misses
    count = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    rate = f"{hits/total*100:.1f}%" if total else "N/A"
    print(f"Cache entries : {count}")
    print(f"Cache hits    : {hits}")
    print(f"Cache misses  : {misses}")
    print(f"Hit rate      : {rate}")
    size_kb = DB_PATH.stat().st_size / 1024 if DB_PATH.exists() else 0
    print(f"DB size       : {size_kb:.1f} KB  ({DB_PATH})")


def cmd_clear() -> None:
    con = _connect()
    con.execute("DELETE FROM cache")
    con.execute("UPDATE stats SET value=0")
    con.commit()
    print("Cache cleared.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "get":
        if len(sys.argv) < 3:
            sys.exit(1)
        sys.exit(cmd_get(sys.argv[2]))

    elif subcmd == "set":
        if len(sys.argv) < 4:
            sys.exit(1)
        cmd_set(sys.argv[2], sys.argv[3])

    elif subcmd == "list":
        cmd_list()

    elif subcmd == "stats":
        cmd_stats()

    elif subcmd == "clear":
        cmd_clear()

    else:
        print(f"Unknown subcommand: {subcmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
