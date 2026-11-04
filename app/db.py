import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR

_lock = threading.Lock()
_path = os.path.join(DATA_DIR, "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    trigger TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    total INTEGER,
    scanned INTEGER NOT NULL DEFAULT 0,
    in_seerr INTEGER NOT NULL DEFAULT 0,
    requested INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    not_found INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT,
    slug TEXT,
    tmdb_id INTEGER,
    detail TEXT,
    user_name TEXT,
    approval TEXT,
    pending INTEGER
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS film_state (
    source TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT,
    tmdb_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT,
    since TEXT NOT NULL,
    PRIMARY KEY (source, slug)
);
CREATE INDEX IF NOT EXISTS film_state_list ON film_state(source, kind, since);
CREATE TABLE IF NOT EXISTS film_cache (
    slug TEXT PRIMARY KEY,
    tmdb_id INTEGER,
    tmdb_type TEXT,
    fetched_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        c.execute(
            "UPDATE runs SET status='interrupted', finished_at=? WHERE status='running'", (now(),)
        )


def prune(days: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with conn() as c:
        c.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE started_at < ?)", (cutoff,))
        c.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))


def start_run(source: str, label: str, trigger: str, dry_run: bool) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs (source, label, trigger, dry_run, started_at) VALUES (?, ?, ?, ?, ?)",
            (source, label, trigger, int(dry_run), now()),
        )
        return cur.lastrowid


def update_run(run_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def add_event(run_id: int, kind: str, title=None, slug=None, tmdb_id=None, detail=None,
              user_name=None, approval=None, pending=None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO events (run_id, ts, kind, title, slug, tmdb_id, detail, user_name, approval, pending) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, now(), kind, title, slug, tmdb_id, detail, user_name, approval, pending),
        )


def film_states(source: str) -> dict[str, dict]:
    with conn() as c:
        return {r["slug"]: dict(r) for r in c.execute("SELECT * FROM film_state WHERE source=?", (source,))}


def set_film_state(source: str, slug: str, title: str, tmdb_id, kind: str, detail) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO film_state (source, slug, title, tmdb_id, kind, detail, since) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, slug, title, tmdb_id, kind, detail, now()),
        )


def delete_film_states(source: str, slugs: list[str] | None = None) -> None:
    with conn() as c:
        if slugs is None:
            c.execute("DELETE FROM film_state WHERE source=?", (source,))
        else:
            c.executemany("DELETE FROM film_state WHERE source=? AND slug=?", [(source, s) for s in slugs])


def film_list(source: str, kinds: list[str], cursor: tuple[str, str] | None, limit: int) -> list[dict]:
    sql = f"SELECT * FROM film_state WHERE source=? AND kind IN ({','.join('?' * len(kinds))})"
    args: list = [source, *kinds]
    if cursor:
        sql += " AND (since, slug) < (?, ?)"
        args.extend(cursor)
    sql += " ORDER BY since DESC, slug DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def film_counts(source: str) -> dict[str, int]:
    with conn() as c:
        return {r["kind"]: r["n"] for r in c.execute(
            "SELECT kind, COUNT(*) AS n FROM film_state WHERE source=? GROUP BY kind", (source,))}


def last_run_per_source() -> dict[str, dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY source)"
        ).fetchall()
    return {r["source"]: dict(r) for r in rows}


def events(kinds: list[str] | None = None, source: str | None = None, run_id: int | None = None,
           limit: int = 50, before: int | None = None, after: int | None = None) -> list[dict]:
    sql = "SELECT e.*, r.source, r.label AS source_label FROM events e JOIN runs r ON r.id = e.run_id WHERE 1=1"
    args: list = []
    if kinds:
        sql += f" AND e.kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    if source:
        sql += " AND r.source = ?"
        args.append(source)
    if run_id:
        sql += " AND e.run_id = ?"
        args.append(run_id)
    if before:
        sql += " AND e.id < ?"
        args.append(before)
    if after:
        sql += " AND e.id > ?"
        args.append(after)
    sql += " ORDER BY e.id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def cached_film(slug: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM film_cache WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def cache_film(slug: str, tmdb_id: int | None, tmdb_type: str | None) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO film_cache (slug, tmdb_id, tmdb_type, fetched_at) VALUES (?, ?, ?, ?)",
            (slug, tmdb_id, tmdb_type, now()),
        )


def get_values() -> dict[str, str]:
    with conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM kv")}


def set_values(values: dict[str, str | None]) -> None:
    with conn() as c:
        for key, value in values.items():
            if value is None:
                c.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
