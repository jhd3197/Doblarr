"""Versioned studio documents over the shared SQLite database.

Every write appends a revision. A caller that read revision `n` passes
`base_revision=n`; if somebody else wrote in between, the write is refused as a
conflict carrying the newer document, so the caller can merge instead of
silently replacing an edit. A document marked `frozen` accepts no further
revision at all: a judged version or a finished experiment variant is evidence,
and later work is recorded as a new record that links back to it.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from ..cues import now
from ..errors import DoblarrError

KINDS = ("session", "reference", "alignment", "experiment", "variant", "audition",
         "annotation", "evaluation", "casting", "import",
         # Media knowledge and adaptive audio (docs: media-knowledge.md). The
         # same revision, conflict and freeze rules hold: a character, a voice
         # profile or an accepted narrative revision is never edited in place.
         "series", "media", "character", "profile", "voice_asset", "assignment",
         "association", "claim", "narrative", "snapshot", "visual", "template",
         "decision", "feedback", "example", "migration",
         # A series' cast from a public catalogue (doblarr.published_cast).
         "published_cast",
         # Title research (docs/title-research.md): a cited answer to a person's
         # question, a reference script found online, a title's other names and ids.
         "research_run", "title_script", "title_info")
# A document larger than this is a sign that media or a whole transcript is
# being stored as state. Structured state stays small; artifacts go to disk.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class StudioConflict(DoblarrError):
    """A write was made against a revision that is no longer the latest."""

    http_status = 409

    def __init__(self, message: str, current: dict | None = None):
        super().__init__(message)
        self.current = current


class FrozenRecord(DoblarrError):
    """A frozen record was asked to change."""

    http_status = 409


def digest_id(*parts) -> str:
    """A short stable id from its defining parts."""
    from ..artifacts import digest

    return digest(list(parts))[:12]


def now_marker() -> str:
    """A timestamp for history entries and for ids that must be unique per call."""
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


def _row(row) -> dict:
    document = json.loads(row["document"])
    return {**document, "id": row["id"], "revision": row["revision"],
            "scope": row["scope"], "updated_at": row["created_at"]}


def get(db, kind: str, record_id: str, revision: int | None = None) -> dict | None:
    if revision is None:
        row = db.query_one(
            "SELECT * FROM studio_records WHERE kind=? AND id=? "
            "ORDER BY revision DESC LIMIT 1", (kind, record_id))
    else:
        row = db.query_one(
            "SELECT * FROM studio_records WHERE kind=? AND id=? AND revision=?",
            (kind, record_id, int(revision)))
    return _row(row) if row else None


def history(db, kind: str, record_id: str) -> list[dict]:
    rows = db.query("SELECT * FROM studio_records WHERE kind=? AND id=? ORDER BY revision",
                    (kind, record_id))
    return [_row(r) for r in rows]


def list_latest(db, kind: str, scope: str | None = None) -> list[dict]:
    """The latest revision of every record of `kind`, optionally in one scope."""
    sql = ("SELECT r.* FROM studio_records r WHERE r.kind=? AND r.revision = ("
           "SELECT MAX(n.revision) FROM studio_records n WHERE n.kind=r.kind AND n.id=r.id)")
    params: tuple = (kind,)
    if scope is not None:
        sql += " AND r.scope=?"
        params = (kind, scope)
    return [_row(r) for r in db.query(sql + " ORDER BY r.created_at, r.id", params)]


def put(db, kind: str, record_id: str, document: dict, *, scope: str = "",
        base_revision: int | None = None, create_only: bool = False) -> dict:
    """Append one revision. `base_revision=None` means "I did not read it"."""
    if kind not in KINDS:
        raise ValueError(f"unknown studio record kind {kind!r}")
    if not record_id or len(record_id) > 128:
        raise ValueError("a studio record needs a short id")
    body = {k: v for k, v in document.items()
            if k not in ("id", "revision", "scope", "updated_at")}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("studio state is too large; keep media and transcripts on disk")
    with db._lock, db._conn:
        latest = db._conn.execute(
            "SELECT revision, document, scope, created_at FROM studio_records "
            "WHERE kind=? AND id=? ORDER BY revision DESC LIMIT 1",
            (kind, record_id)).fetchone()
        if latest is not None:
            current = {**json.loads(latest[1]), "id": record_id, "revision": latest[0],
                       "scope": latest[2], "updated_at": latest[3]}
            if create_only:
                raise StudioConflict(f"{kind} {record_id} already exists", current)
            if current.get("frozen"):
                raise FrozenRecord(
                    f"{kind} {record_id} is frozen at revision {latest[0]}; record later "
                    "work as a new revision linked to it")
            if base_revision is not None and int(base_revision) != latest[0]:
                raise StudioConflict(
                    f"{kind} {record_id} changed since revision {base_revision} "
                    f"(now {latest[0]}); reload and apply your edit again", current)
            revision = latest[0] + 1
            scope = scope or latest[2]
        else:
            if base_revision not in (None, 0):
                raise StudioConflict(f"{kind} {record_id} does not exist any more")
            revision = 1
        created = now()
        db._conn.execute(
            "INSERT INTO studio_records (kind,id,revision,scope,document,created_at) "
            "VALUES (?,?,?,?,?,?)", (kind, record_id, revision, scope, encoded, created))
    return {**json.loads(encoded), "id": record_id, "revision": revision, "scope": scope,
            "updated_at": created}


def update(db, kind: str, record_id: str, patch: dict, *, base_revision: int | None = None,
           scope: str = "") -> dict:
    """Merge `patch` over the latest revision and append the result."""
    current = get(db, kind, record_id) or {}
    merged: dict[str, Any] = {**current, **patch}
    return put(db, kind, record_id, merged, scope=scope or current.get("scope", ""),
               base_revision=current.get("revision") if base_revision is None
               else base_revision)
