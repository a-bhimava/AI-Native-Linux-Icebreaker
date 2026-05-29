#!/usr/bin/env python3
"""
pb_cache.py — SQLite cache for Privileged Brain NL→command queries.

Exact-match lookup is O(1). Fuzzy semantic lookup normalizes the query
(lowercase, filler-word removal, synonym folding) and computes TF-IDF
cosine similarity against all cached keys — falling back gracefully when
the cache is empty or the sklearn dependency is unavailable.

Usage:
  python3 pb_cache.py get "show all docker containers"   → prints cached cmd or nothing
  python3 pb_cache.py set "show all docker containers" "docker ps -a"
  python3 pb_cache.py set-verified "show docker containers" "docker ps -a"
  python3 pb_cache.py list                               → show top 20 most-used
  python3 pb_cache.py stats                              → hit/miss stats
  python3 pb_cache.py clear                              → wipe cache
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".pb_cache.db"

# Similarity threshold for fuzzy lookup (0.0–1.0). Tuned for sysadmin paraphrase pairs.
_SIMILARITY_THRESHOLD = 0.72

# Filler words stripped before TF-IDF comparison
_FILLER = re.compile(
    r"\b(please|can you|could you|i want to|i need to|i'd like to|"
    r"i would like to|show me|tell me|give me|help me|let me|just|"
    r"quickly|simply|how (do i|can i|to))\b",
    re.IGNORECASE,
)

# Synonym normalization — maps variants to a canonical token
_SYNONYMS: dict[str, str] = {
    "restart": "restart", "reload": "restart", "bounce": "restart",
    "stop": "stop", "halt": "stop", "shutdown": "stop",
    "start": "start", "launch": "start", "run": "start",
    "remove": "delete", "delete": "delete", "rm": "delete",
    "show": "list", "display": "list", "print": "list", "view": "list",
    "list": "list", "ls": "list",
    "install": "install", "add": "install",
    "uninstall": "remove_pkg", "purge": "remove_pkg",
    "check": "status", "status": "status", "inspect": "status",
    "kill": "kill", "terminate": "kill",
}


def normalize_intent(query: str) -> str:
    """Normalize a natural-language intent string for cache comparison."""
    text = query.lower().strip()
    text = _FILLER.sub(" ", text)
    # Fold synonyms token by token
    tokens = re.findall(r"\w+", text)
    tokens = [_SYNONYMS.get(t, t) for t in tokens]
    return " ".join(tokens)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            query_hash  TEXT PRIMARY KEY,
            query_nl    TEXT NOT NULL,
            query_norm  TEXT NOT NULL,
            command     TEXT NOT NULL,
            verified    INTEGER NOT NULL DEFAULT 0,
            hits        INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL,
            last_used   REAL NOT NULL
        )
    """)
    # Migrate: add query_norm + verified columns to existing DBs that predate this version
    _cols = {row[1] for row in con.execute("PRAGMA table_info(cache)")}
    if "query_norm" not in _cols:
        con.execute("ALTER TABLE cache ADD COLUMN query_norm TEXT NOT NULL DEFAULT ''")
    if "verified" not in _cols:
        con.execute("ALTER TABLE cache ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    con.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("INSERT OR IGNORE INTO stats VALUES ('hits', 0)")
    con.execute("INSERT OR IGNORE INTO stats VALUES ('misses', 0)")
    con.execute("INSERT OR IGNORE INTO stats VALUES ('fuzzy_hits', 0)")
    con.commit()
    return con


def _hash(query: str) -> str:
    return hashlib.sha256(query.lower().strip().encode()).hexdigest()


def _fuzzy_lookup(con: sqlite3.Connection, norm_query: str) -> Optional[str]:
    """Return the cached command with highest cosine similarity, or None."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        import numpy as np
    except ImportError:
        return None

    rows = con.execute("SELECT query_norm, command FROM cache").fetchall()
    if not rows:
        return None

    corpus = [r[0] for r in rows]
    commands = [r[1] for r in rows]

    try:
        vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
        tfidf = vec.fit_transform(corpus + [norm_query])
        sims = cosine_similarity(tfidf[-1], tfidf[:-1])[0]
        best_idx = int(np.argmax(sims))
        if sims[best_idx] >= _SIMILARITY_THRESHOLD:
            return commands[best_idx]
    except Exception:
        pass
    return None


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

    # Exact miss — try fuzzy
    norm = normalize_intent(query)
    fuzzy_cmd = _fuzzy_lookup(con, norm)
    if fuzzy_cmd:
        con.execute("UPDATE stats SET value=value+1 WHERE key='fuzzy_hits'")
        con.commit()
        print(fuzzy_cmd)
        return 0

    con.execute("UPDATE stats SET value=value+1 WHERE key='misses'")
    con.commit()
    return 1


def cmd_set(query: str, command: str, verified: bool = False) -> None:
    con = _connect()
    h = _hash(query)
    norm = normalize_intent(query)
    now = time.time()
    con.execute("""
        INSERT INTO cache (query_hash, query_nl, query_norm, command, verified, hits, created_at, last_used)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(query_hash) DO UPDATE SET
            command=excluded.command,
            query_norm=excluded.query_norm,
            verified=MAX(cache.verified, excluded.verified),
            hits=hits+1,
            last_used=excluded.last_used
    """, (h, query.strip(), norm, command.strip(), int(verified), now, now))
    con.commit()


def cmd_list() -> None:
    con = _connect()
    rows = con.execute(
        "SELECT query_nl, command, verified, hits, last_used FROM cache ORDER BY hits DESC LIMIT 20"
    ).fetchall()
    if not rows:
        print("Cache is empty.")
        return
    print(f"{'Hits':>5}  {'V':>1}  {'Last used':>20}  {'Query → Command'}")
    print("-" * 90)
    for nl, cmd, verified, hits, last_used in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_used))
        v_mark = "✓" if verified else " "
        preview = f"{nl[:28]!r} → {cmd[:30]!r}"
        print(f"{hits:>5}  {v_mark}  {ts:>20}  {preview}")


def cmd_stats() -> None:
    con = _connect()
    hits = con.execute("SELECT value FROM stats WHERE key='hits'").fetchone()[0]
    misses = con.execute("SELECT value FROM stats WHERE key='misses'").fetchone()[0]
    fuzzy = con.execute("SELECT value FROM stats WHERE key='fuzzy_hits'").fetchone()[0]
    total = hits + misses
    count = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    verified = con.execute("SELECT COUNT(*) FROM cache WHERE verified=1").fetchone()[0]
    rate = f"{(hits + fuzzy)/total*100:.1f}%" if total else "N/A"
    print(f"Cache entries : {count}  ({verified} verified)")
    print(f"Exact hits    : {hits}")
    print(f"Fuzzy hits    : {fuzzy}")
    print(f"Cache misses  : {misses}")
    print(f"Combined rate : {rate}")
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
        cmd_set(sys.argv[2], sys.argv[3], verified=False)

    elif subcmd == "set-verified":
        if len(sys.argv) < 4:
            sys.exit(1)
        cmd_set(sys.argv[2], sys.argv[3], verified=True)

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
