"""SQLite persistence — job queue rows and last-scan state.

Stdlib sqlite3 only, no ORM: one shared connection guarded by a lock
(`check_same_thread=False`), WAL mode so reads don't block writes, and a tiny
migration runner (`PRAGMA user_version` + an ordered list of migration
functions applied once each at open).
"""

from __future__ import annotations

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


# Ordered migrations; MIGRATIONS[i] brings a db from version i to i+1.
MIGRATIONS = [_v1_initial]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
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
            (last_scan, json.dumps(counts), json.dumps(items)))

    def load_scan(self) -> dict | None:
        row = self.query_one("SELECT last_scan, counts, items FROM scan_state WHERE id = 1")
        if row is None:
            return None
        return {"last_scan": row["last_scan"],
                "counts": json.loads(row["counts"]),
                "items": json.loads(row["items"])}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
