"""SQLite persistence — job queue rows and last-scan state.

Stdlib sqlite3 only, no ORM: one shared connection guarded by a lock
(`check_same_thread=False`), WAL mode so reads don't block writes, and a tiny
migration runner (`PRAGMA user_version` + an ordered list of migration
functions applied once each at open).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("doblarr.store")


def _v1_initial(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE jobs (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT '',
            source_lang TEXT NOT NULL DEFAULT '',
            target_lang TEXT NOT NULL DEFAULT '',
            input_file  TEXT,
            status      TEXT NOT NULL DEFAULT 'queued',
            stage       TEXT NOT NULL DEFAULT '',
            progress    INTEGER NOT NULL DEFAULT 0,
            message     TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}'  -- JSON: fields without a column
        );
        CREATE TABLE scan_state (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            last_scan TEXT,
            counts    TEXT NOT NULL DEFAULT '{}',   -- JSON object
            items     TEXT NOT NULL DEFAULT '[]'    -- JSON list of item dicts
        );
    """)


def _v2_teasers_and_casts(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'full';
        CREATE TABLE voice_casts (
            title_key  TEXT PRIMARY KEY,   -- see doblarr.voices.cast_key
            title      TEXT NOT NULL DEFAULT '',
            cast_data  TEXT NOT NULL DEFAULT '[]',   -- JSON list of cast entries
            updated_at TEXT NOT NULL
        );
    """)


def _v3_title_plans(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE title_plans (
            title_key  TEXT PRIMARY KEY,   -- see doblarr.voices.cast_key
            title      TEXT NOT NULL DEFAULT '',
            plan       TEXT NOT NULL DEFAULT '{}',   -- JSON: per-title config overrides
            updated_at TEXT NOT NULL
        );
    """)


def _v4_target_locale(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        ALTER TABLE jobs ADD COLUMN target_locale TEXT NOT NULL DEFAULT '';
    """)


def _v5_knowledge(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE knowledge_entries (
            id           TEXT NOT NULL,
            revision     INTEGER NOT NULL DEFAULT 1,
            kind         TEXT NOT NULL,
            locale       TEXT NOT NULL DEFAULT '',
            coverage     TEXT NOT NULL DEFAULT '[]',  -- JSON list of declared locales
            source_lang  TEXT,
            source_form  TEXT NOT NULL DEFAULT '',
            phrase       TEXT NOT NULL,
            sense        TEXT NOT NULL DEFAULT '',
            usage        TEXT NOT NULL DEFAULT '',
            examples     TEXT NOT NULL DEFAULT '[]',  -- JSON list
            pronunciation TEXT NOT NULL DEFAULT '',
            ipa          TEXT,
            scope        TEXT NOT NULL DEFAULT 'personal',
            scope_ref    TEXT NOT NULL DEFAULT '',
            suppresses   TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'proposed',
            origin       TEXT NOT NULL DEFAULT 'local',  -- local | installed
            license      TEXT NOT NULL DEFAULT '',
            contributor  TEXT NOT NULL DEFAULT '',
            review_history TEXT NOT NULL DEFAULT '[]',   -- JSON list
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (id, revision)
        );
        CREATE TABLE knowledge_realizations (
            id          TEXT NOT NULL,
            entry_id    TEXT NOT NULL,
            revision    INTEGER NOT NULL DEFAULT 1,
            engine      TEXT NOT NULL,
            model       TEXT,        -- NULL = deliberately broad, never "unknown"
            voice       TEXT,        -- NULL = any voice
            replacement TEXT NOT NULL,
            evidence    TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'proposed',
            origin      TEXT NOT NULL DEFAULT 'local',
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (id, revision)
        );
        CREATE INDEX idx_knowledge_entries_lookup
            ON knowledge_entries(kind, locale, scope, status);
        CREATE INDEX idx_knowledge_realizations_lookup
            ON knowledge_realizations(entry_id, engine, status);
    """)


def _v6_packs(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE knowledge_packs (
            pack_id      TEXT NOT NULL,
            release      TEXT NOT NULL,
            name         TEXT NOT NULL DEFAULT '',
            manifest     TEXT NOT NULL DEFAULT '{}',   -- JSON manifest as imported
            content_hash TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT 'third-party',  -- official | third-party
            distribution TEXT NOT NULL DEFAULT '',     -- URL/path the pack came from
            active       INTEGER NOT NULL DEFAULT 0,
            installed_at TEXT NOT NULL,
            PRIMARY KEY (pack_id, release)
        );
        ALTER TABLE knowledge_entries ADD COLUMN pack_id TEXT;
        ALTER TABLE knowledge_realizations ADD COLUMN pack_id TEXT;
    """)


def _v7_memory(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE translation_memory (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            source_lang TEXT NOT NULL,
            target_locale TEXT NOT NULL,
            source_text TEXT NOT NULL,
            context_hash TEXT NOT NULL,
            document TEXT NOT NULL,
            UNIQUE(id, revision)
        );
        CREATE INDEX memory_exact ON translation_memory
            (source_lang, target_locale, source_text, context_hash, seq);
        CREATE INDEX memory_versions ON translation_memory(id, seq);
    """)


# Ordered migrations; MIGRATIONS[i] brings a db from version i to i+1.
MIGRATIONS = [
    _v1_initial,
    _v2_teasers_and_casts,
    _v3_title_plans,
    _v4_target_locale,
    _v5_knowledge,
    _v6_packs,
    _v7_memory,
]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("casefold", 1, lambda s: s.casefold(), deterministic=True)
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, migrate in enumerate(MIGRATIONS[version:], start=version + 1):
                migrate(self._conn)
                self._conn.execute(f"PRAGMA user_version = {i}")
                self._conn.commit()
                log.info("applied db migration %d -> %s", i, self.path.name)

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -- scan state (single row) -------------------------------------------
    def save_scan(self, last_scan: str, counts: dict, items: list[dict]) -> None:
        self.execute(
            "INSERT INTO scan_state (id, last_scan, counts, items) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_scan=excluded.last_scan, "
            "counts=excluded.counts, items=excluded.items",
            (last_scan, json.dumps(counts), json.dumps(items)),
        )

    def load_scan(self) -> dict | None:
        row = self.query_one("SELECT last_scan, counts, items FROM scan_state WHERE id = 1")
        if row is None:
            return None
        return {
            "last_scan": row["last_scan"],
            "counts": json.loads(row["counts"]),
            "items": json.loads(row["items"]),
        }

    # -- voice casts (one row per title) ------------------------------------
    def save_cast(self, title_key: str, title: str, cast: list[dict]) -> None:
        self.execute(
            "INSERT INTO voice_casts (title_key, title, cast_data, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(title_key) DO UPDATE SET "
            "title=excluded.title, cast_data=excluded.cast_data, "
            "updated_at=excluded.updated_at",
            (title_key, title, json.dumps(cast), _dt.datetime.now().isoformat(timespec="seconds")),
        )

    def load_cast(self, title_key: str) -> dict | None:
        row = self.query_one(
            "SELECT title, cast_data, updated_at FROM voice_casts WHERE title_key = ?", (title_key,)
        )
        if row is None:
            return None
        return {
            "title": row["title"],
            "cast": json.loads(row["cast_data"]),
            "updated_at": row["updated_at"],
        }

    # -- per-title dub plans (config overrides, one row per title) ----------
    def save_plan(self, title_key: str, title: str, plan: dict) -> None:
        self.execute(
            "INSERT INTO title_plans (title_key, title, plan, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(title_key) DO UPDATE SET "
            "title=excluded.title, plan=excluded.plan, "
            "updated_at=excluded.updated_at",
            (title_key, title, json.dumps(plan), _dt.datetime.now().isoformat(timespec="seconds")),
        )

    def load_plan(self, title_key: str) -> dict | None:
        row = self.query_one(
            "SELECT title, plan, updated_at FROM title_plans WHERE title_key = ?", (title_key,)
        )
        if row is None:
            return None
        return {
            "title": row["title"],
            "plan": json.loads(row["plan"]),
            "updated_at": row["updated_at"],
        }

    def save_recipe(self, title_key: str, title: str, plan: dict, cast: list[dict]) -> None:
        """Apply a recipe's plan and cast together, or leave both untouched."""
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO title_plans (title_key,title,plan,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(title_key) DO UPDATE SET title=excluded.title,plan=excluded.plan,"
                "updated_at=excluded.updated_at",
                (title_key, title, json.dumps(plan), now),
            )
            self._conn.execute(
                "INSERT INTO voice_casts (title_key,title,cast_data,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(title_key) DO UPDATE SET title=excluded.title,"
                "cast_data=excluded.cast_data,updated_at=excluded.updated_at",
                (title_key, title, json.dumps(cast), now),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
