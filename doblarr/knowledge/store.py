"""SQLite storage for knowledge entries and realizations.

Every edit is an insert at a new revision; rows are never updated in place, so
a job frozen on (id, revision) pins is immune to later edits. The `origin`
column keeps local edits separate from installed pack content forever.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

from ..store import Database
from .models import Entry, Realization

log = logging.getLogger("doblarr.knowledge")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _entry_from_row(row) -> Entry:
    return Entry(
        id=row["id"],
        revision=row["revision"],
        kind=row["kind"],
        locale=row["locale"],
        coverage=tuple(json.loads(row["coverage"])),
        source_lang=row["source_lang"],
        source_form=row["source_form"],
        phrase=row["phrase"],
        sense=row["sense"],
        usage=row["usage"],
        examples=tuple(json.loads(row["examples"])),
        pronunciation=row["pronunciation"],
        ipa=row["ipa"],
        scope=row["scope"],
        scope_ref=row["scope_ref"],
        suppresses=row["suppresses"],
        status=row["status"],
        origin=row["origin"],
        license=row["license"],
        contributor=row["contributor"],
        review_history=tuple(json.loads(row["review_history"])),
        pack_id=row["pack_id"],
    )


def _realization_from_row(row) -> Realization:
    return Realization(
        id=row["id"],
        entry_id=row["entry_id"],
        revision=row["revision"],
        engine=row["engine"],
        model=row["model"],
        voice=row["voice"],
        replacement=row["replacement"],
        evidence=row["evidence"],
        status=row["status"],
        origin=row["origin"],
        pack_id=row["pack_id"],
    )


def save_entry(db: Database, entry: Entry) -> Entry:
    """Insert an entry at the next revision of its id (1 for a new id)."""
    entry.validate()
    row = db.query_one("SELECT MAX(revision) AS r FROM knowledge_entries WHERE id = ?", (entry.id,))
    revision = ((row["r"] if row else None) or 0) + 1
    entry = Entry(**{**entry.__dict__, "revision": revision})
    db.execute(
        "INSERT INTO knowledge_entries (id, revision, kind, locale, coverage, source_lang,"
        " source_form, phrase, sense, usage, examples, pronunciation, ipa, scope, scope_ref,"
        " suppresses, status, origin, license, contributor, review_history, pack_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry.id,
            entry.revision,
            entry.kind,
            entry.locale,
            json.dumps(list(entry.coverage)),
            entry.source_lang,
            entry.source_form,
            entry.phrase,
            entry.sense,
            entry.usage,
            json.dumps(list(entry.examples), ensure_ascii=False),
            entry.pronunciation,
            entry.ipa,
            entry.scope,
            entry.scope_ref,
            entry.suppresses,
            entry.status,
            entry.origin,
            entry.license,
            entry.contributor,
            json.dumps(list(entry.review_history), ensure_ascii=False),
            entry.pack_id,
            _now(),
        ),
    )
    return entry


def save_realization(db: Database, realization: Realization) -> Realization:
    realization.validate()
    row = db.query_one(
        "SELECT MAX(revision) AS r FROM knowledge_realizations WHERE id = ?", (realization.id,)
    )
    revision = ((row["r"] if row else None) or 0) + 1
    realization = Realization(**{**realization.__dict__, "revision": revision})
    db.execute(
        "INSERT INTO knowledge_realizations (id, entry_id, revision, engine, model, voice,"
        " replacement, evidence, status, origin, pack_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            realization.id,
            realization.entry_id,
            realization.revision,
            realization.engine,
            realization.model,
            realization.voice,
            realization.replacement,
            realization.evidence,
            realization.status,
            realization.origin,
            realization.pack_id,
            _now(),
        ),
    )
    return realization


def _latest(db: Database, table: str) -> list:
    rows = db.query(
        f"SELECT t.* FROM {table} t JOIN (SELECT id, MAX(revision) AS r FROM {table}"
        " GROUP BY id) latest ON t.id = latest.id AND t.revision = latest.r"
    )
    return rows


def _latest_within(rows, from_row, allowed: dict[str, int] | None) -> list:
    """Latest revision per id; installed rows count only at an active release's pin."""
    best: dict = {}
    for row in rows:
        record = from_row(row)
        if (record.origin != "local" and allowed is not None
                and allowed.get(record.id) != record.revision):
            continue
        if record.id not in best or record.revision > best[record.id].revision:
            best[record.id] = record
    return list(best.values())


def latest_entries(db: Database, installed_pins: dict | None = None) -> list[Entry]:
    """Latest local rules plus installed rules at their ACTIVE release pins.

    Without `installed_pins` (from active_installed_pins), installed rows are
    unfiltered — only valid when no pack has more than one installed release.
    """
    if installed_pins is None:
        return [_entry_from_row(r) for r in _latest(db, "knowledge_entries")]
    rows = db.query("SELECT * FROM knowledge_entries")
    return _latest_within(rows, _entry_from_row, installed_pins.get("entries", {}))


def latest_realizations(db: Database, installed_pins: dict | None = None) -> list[Realization]:
    if installed_pins is None:
        return [_realization_from_row(r) for r in _latest(db, "knowledge_realizations")]
    rows = db.query("SELECT * FROM knowledge_realizations")
    return _latest_within(rows, _realization_from_row, installed_pins.get("realizations", {}))


# -- installed packs ---------------------------------------------------------


def save_pack_release(
    db: Database,
    *,
    pack_id: str,
    release: str,
    name: str,
    manifest: dict,
    content_hash: str,
    source: str,
    distribution: str,
) -> None:
    db.execute(
        "INSERT INTO knowledge_packs (pack_id, release, name, manifest, content_hash,"
        " source, distribution, active, installed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            pack_id, release, name, json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            content_hash, source, distribution, 0, _now(),
        ),
    )


def pack_releases(db: Database, pack_id: str | None = None) -> list[dict]:
    if pack_id:
        rows = db.query(
            "SELECT * FROM knowledge_packs WHERE pack_id = ? ORDER BY rowid", (pack_id,)
        )
    else:
        rows = db.query("SELECT * FROM knowledge_packs ORDER BY pack_id, rowid")
    return [dict(r) for r in rows]


def set_active_release(db: Database, pack_id: str, release: str) -> None:
    with db._lock, db._conn:
        db._conn.execute("UPDATE knowledge_packs SET active = 0 WHERE pack_id = ?", (pack_id,))
        db._conn.execute(
            "UPDATE knowledge_packs SET active = 1 WHERE pack_id = ? AND release = ?",
            (pack_id, release),
        )


def active_installed_pins(db: Database) -> dict:
    """Entry/realization pins of every ACTIVE pack release (for release-aware resolution)."""
    pins: dict[str, dict[str, int]] = {"entries": {}, "realizations": {}}
    for row in db.query("SELECT manifest FROM knowledge_packs WHERE active = 1"):
        manifest = json.loads(row["manifest"])
        for entry in manifest.get("entries", []):
            pins["entries"][entry["id"]] = entry["revision"]
            for realization in entry.get("realizations", []):
                pins["realizations"][realization["id"]] = realization["revision"]
    return pins


def entries_at(db: Database, pinned: dict[str, int]) -> list[Entry]:
    """Fetch exact pinned revisions; a missing revision is surfaced and skipped."""
    out = []
    for entry_id, revision in pinned.items():
        row = db.query_one(
            "SELECT * FROM knowledge_entries WHERE id = ? AND revision = ?",
            (entry_id, revision),
        )
        if row is None:
            log.warning("knowledge: frozen entry %s@%s is missing; skipping it", entry_id, revision)
            continue
        out.append(_entry_from_row(row))
    return out


def realizations_at(db: Database, pinned: dict[str, int]) -> list[Realization]:
    out = []
    for realization_id, revision in pinned.items():
        row = db.query_one(
            "SELECT * FROM knowledge_realizations WHERE id = ? AND revision = ?",
            (realization_id, revision),
        )
        if row is None:
            log.warning(
                "knowledge: frozen realization %s@%s is missing; skipping it",
                realization_id,
                revision,
            )
            continue
        out.append(_realization_from_row(row))
    return out


def snapshot(db: Database) -> dict:
    """Pin the latest applicable revision of every rule — a job's frozen state.

    Installed pack content is pinned at the active release, so a later pack
    update or rollback never changes a job that was already queued.
    """
    from .memory import watermark

    pins = active_installed_pins(db)
    return {
        "version": 1,
        "memory_cutoff": watermark(db),
        "entries": {e.id: e.revision for e in latest_entries(db, pins)},
        "realizations": {r.id: r.revision for r in latest_realizations(db, pins)},
    }


def with_pack_releases(db: Database, frozen: dict, releases: dict[str, str]) -> dict:
    """Replace only requested packs with exact installed recipe releases."""
    releases = dict(releases)
    pending = list(releases)
    checked = set()
    while pending:
        pack_id = pending.pop()
        if pack_id in checked:
            continue
        checked.add(pack_id)
        row = db.query_one("SELECT manifest FROM knowledge_packs WHERE pack_id=? AND release=?",
                           (pack_id, releases[pack_id]))
        if row is None:
            raise ValueError(f"missing required pack {pack_id}@{releases[pack_id]}")
        for dependency, revision in json.loads(row["manifest"]).get("dependencies", {}).items():
            if dependency in releases and releases[dependency] != revision:
                raise ValueError(f"conflicting pinned releases for {dependency}")
            releases[dependency] = revision
            pending.append(dependency)
    frozen = {**frozen, "entries": dict(frozen.get("entries", {})),
              "realizations": dict(frozen.get("realizations", {}))}
    for pack_id, release in releases.items():
        row = db.query_one("SELECT manifest FROM knowledge_packs WHERE pack_id=? AND release=?",
                           (pack_id, release))
        if row is None:
            raise ValueError(f"missing required pack {pack_id}@{release}")
        for table, key in (("knowledge_entries", "entries"),
                           ("knowledge_realizations", "realizations")):
            for record in db.query(f"SELECT DISTINCT id FROM {table} WHERE pack_id=?", (pack_id,)):
                frozen[key].pop(record["id"], None)
        for entry in json.loads(row["manifest"]).get("entries", []):
            frozen["entries"][entry["id"]] = entry["revision"]
            for realization in entry.get("realizations", []):
                frozen["realizations"][realization["id"]] = realization["revision"]
    frozen["pack_releases"] = dict(releases)
    return frozen


def get_entry(db: Database, entry_id: str) -> Entry | None:
    row = db.query_one(
        "SELECT * FROM knowledge_entries WHERE id = ? ORDER BY revision DESC LIMIT 1",
        (entry_id,),
    )
    return _entry_from_row(row) if row else None


def get_realization(db: Database, realization_id: str) -> Realization | None:
    row = db.query_one(
        "SELECT * FROM knowledge_realizations WHERE id = ? ORDER BY revision DESC LIMIT 1",
        (realization_id,),
    )
    return _realization_from_row(row) if row else None


def realizations_for(db: Database, entry_id: str) -> list[Realization]:
    rows = db.query(
        "SELECT t.* FROM knowledge_realizations t JOIN (SELECT id, MAX(revision) AS r"
        " FROM knowledge_realizations GROUP BY id) latest"
        " ON t.id = latest.id AND t.revision = latest.r WHERE t.entry_id = ?",
        (entry_id,),
    )
    return [_realization_from_row(r) for r in rows]


def suppressed_by(db: Database, entry_id: str) -> list[Entry]:
    """Latest-revision rules that disable this entry."""
    return [e for e in latest_entries(db) if e.suppresses == entry_id]


def search_entries(
    db: Database,
    *,
    locale: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[Entry], int]:
    """Latest revisions matching the filters, paginated (1-based pages)."""
    filters = []
    params = []
    for column, value in (("locale", locale), ("kind", kind), ("scope", scope), ("status", status)):
        if value:
            filters.append(f"{column} = ?")
            params.append(value)
    if q:
        filters.append("(instr(casefold(phrase), ?) OR instr(casefold(source_form), ?)"
                       " OR instr(casefold(usage), ?) OR instr(casefold(sense), ?))")
        params.extend([q.casefold()] * 4)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    count_row = db.query_one(
        _VISIBLE + " SELECT COUNT(*) AS n FROM visible" + where, tuple(params),
    )
    assert count_row is not None
    total = count_row["n"]
    rows = db.query(_VISIBLE + " SELECT * FROM visible" + where
                    + " ORDER BY casefold(phrase), id LIMIT ? OFFSET ?",
                    (*params, page_size, (max(1, page) - 1) * page_size))
    return [_entry_from_row(row) for row in rows], total


_VISIBLE = """
    WITH active_pins AS (
        SELECT json_extract(e.value, '$.id') AS id,
               json_extract(e.value, '$.revision') AS revision
        FROM knowledge_packs p, json_each(p.manifest, '$.entries') e WHERE p.active=1
    ), visible AS (
        SELECT k.* FROM knowledge_entries k
        WHERE (k.origin='local' AND NOT EXISTS (
            SELECT 1 FROM knowledge_entries n WHERE n.id=k.id AND n.revision>k.revision
        )) OR (k.origin='installed' AND (k.id,k.revision) IN (SELECT id,revision FROM active_pins))
    )
"""


def coverage_counts(db: Database) -> dict[str, dict[str, int]]:
    """Aggregate actual active-release counts in SQLite without loading the corpus."""
    counts: dict[str, dict[str, int]] = {}
    rows = db.query(_VISIBLE + " SELECT locale,status,COUNT(*) AS n FROM visible"
                    " WHERE suppresses='' GROUP BY locale,status")
    for row in rows:
        bucket = counts.setdefault(
            row["locale"], {"entries": 0, "reviewed": 0, "proposed": 0, "needs-retest": 0}
        )
        bucket["entries"] += row["n"]
        if row["status"] in ("reviewed", "proposed", "needs-retest"):
            bucket[row["status"]] += row["n"]
    return counts


def _fetch_one(target, sql: str, params: tuple):
    """query_one on a Database, or fetchone on a raw connection inside a transaction."""
    if isinstance(target, Database):
        return target.query_one(sql, params)
    return target.execute(sql, params).fetchone()


def insert_entry_version(db, entry: Entry) -> bool:
    """Insert at the record's pinned revision (recipe/pack import). False when it exists.

    An identical existing row is a no-op; a different row at the same (id, revision)
    is a conflict — reported by the caller, never overwritten.
    """
    entry.validate()
    row = _fetch_one(
        db,
        "SELECT * FROM knowledge_entries WHERE id = ? AND revision = ?",
        (entry.id, entry.revision),
    )
    if row is not None:
        return _entry_from_row(row) == entry
    db.execute(
        "INSERT INTO knowledge_entries (id, revision, kind, locale, coverage, source_lang,"
        " source_form, phrase, sense, usage, examples, pronunciation, ipa, scope, scope_ref,"
        " suppresses, status, origin, license, contributor, review_history, pack_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry.id,
            entry.revision,
            entry.kind,
            entry.locale,
            json.dumps(list(entry.coverage)),
            entry.source_lang,
            entry.source_form,
            entry.phrase,
            entry.sense,
            entry.usage,
            json.dumps(list(entry.examples), ensure_ascii=False),
            entry.pronunciation,
            entry.ipa,
            entry.scope,
            entry.scope_ref,
            entry.suppresses,
            entry.status,
            entry.origin,
            entry.license,
            entry.contributor,
            json.dumps(list(entry.review_history), ensure_ascii=False),
            entry.pack_id,
            _now(),
        ),
    )
    return True


def insert_realization_version(db, realization: Realization) -> bool:
    realization.validate()
    row = _fetch_one(
        db,
        "SELECT * FROM knowledge_realizations WHERE id = ? AND revision = ?",
        (realization.id, realization.revision),
    )
    if row is not None:
        return _realization_from_row(row) == realization
    db.execute(
        "INSERT INTO knowledge_realizations (id, entry_id, revision, engine, model, voice,"
        " replacement, evidence, status, origin, pack_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            realization.id,
            realization.entry_id,
            realization.revision,
            realization.engine,
            realization.model,
            realization.voice,
            realization.replacement,
            realization.evidence,
            realization.status,
            realization.origin,
            realization.pack_id,
            _now(),
        ),
    )
    return True
