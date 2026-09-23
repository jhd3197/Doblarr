"""Private, immutable, exact complete-line translation reuse.

One indexed query per segment, bounded by the job's frozen sequence watermark.
Unknown context never permits reuse. No case folding, fuzzy matching or pivot
language lookup: punctuation, negation and locale all matter.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..artifacts import digest
from ..languages import parse


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=100)
    revision: int = Field(default=1, ge=1)
    source_lang: str
    target_locale: str
    source_text: str = Field(min_length=1, max_length=10000)
    target_text: str = Field(min_length=1, max_length=10000)
    context: dict = Field(default_factory=dict)
    duration: float = Field(gt=0, le=600)
    status: Literal["proposed", "reviewed", "retired"] = "proposed"
    reviewer: str = Field(default="", max_length=200)
    meaning_reviewed: bool = False
    naturalness_reviewed: bool = False
    timing_reviewed: bool = False

    @field_validator("source_lang", "target_locale")
    @classmethod
    def canonical_language(cls, value):
        result = parse(value)
        if result is None or result == "und":
            raise ValueError("a known source and target language are required")
        return result

    @field_validator("source_text", "target_text")
    @classmethod
    def text(cls, value):
        result = normalize_text(value)
        if not result:
            raise ValueError("dialogue must not be empty")
        return result


def watermark(db) -> int:
    return db.query_one("SELECT COALESCE(MAX(seq), 0) AS n FROM translation_memory")["n"]


def save(db, entry: MemoryEntry) -> MemoryEntry:
    if len(json.dumps(entry.context)) > 65536:
        raise ValueError("memory context exceeds 64 KB")
    with db._lock, db._conn:
        row = db._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM translation_memory WHERE id=?", (entry.id,),
        ).fetchone()
        entry = entry.model_copy(update={"revision": row[0] + 1})
        db._conn.execute(
            "INSERT INTO translation_memory"
            " (id,revision,source_lang,target_locale,source_text,context_hash,document)"
            " VALUES (?,?,?,?,?,?,?)",
            (entry.id, entry.revision, entry.source_lang, entry.target_locale,
             entry.source_text, digest(entry.context), entry.model_dump_json()),
        )
    return entry


def scene_context(job, position: int, glossary: dict | None,
                  synopsis: str | None = None) -> dict:
    segment = job.segments[position]
    options = job.translation_options
    # Keep only guidance relevant to this complete line in the reuse key.
    relevant = {k: v for k, v in (glossary or {}).items() if k in segment.text_src}
    # `prepass` is how the synopsis was made, not what it says; the synopsis
    # itself is identified below, and only when there is one, so contexts from
    # runs without a prep pass keep the key they always had.
    settings = {k: v for k, v in options.items()
                if k not in {"reuse_memory", "glossary", "character_notes", "prepass"}}
    extra = {"synopsis": digest(synopsis)[:16]} if synopsis else {}
    return {
        "before": [{"text": s.text_src, "speaker": s.speaker}
                   for s in job.segments[max(0, position - 3):position]],
        "after": [{"text": s.text_src, "speaker": s.speaker}
                  for s in job.segments[position + 1:position + 4]],
        "register": options.get("character_notes", {}).get(segment.speaker, ""),
        "speaker": segment.speaker,
        "settings": settings,
        "glossary": relevant,
        **extra,
    }


def lookup(db, *, source_lang, target_locale, source_text, context, duration,
           target_chars, cutoff) -> tuple[MemoryEntry | None, str]:
    if not context.get("register") or not all(
        key in context for key in ("before", "after", "settings", "speaker", "glossary")
    ):
        return None, "unknown-context"
    rows = db.query(
        "SELECT m.document FROM translation_memory m"
        " WHERE m.source_lang=? AND m.target_locale=? AND m.source_text=?"
        " AND m.context_hash=? AND m.seq<=?"
        " AND NOT EXISTS (SELECT 1 FROM translation_memory newer"
        " WHERE newer.id=m.id AND newer.seq>m.seq AND newer.seq<=?) LIMIT 101",
        (source_lang, target_locale, normalize_text(source_text), digest(context), cutoff, cutoff),
    )
    if len(rows) > 100:
        return None, "too-many-candidates"
    eligible = []
    for row in rows:
        entry = MemoryEntry.model_validate_json(row["document"])
        if (entry.status == "reviewed" and entry.reviewer and entry.meaning_reviewed
                and entry.naturalness_reviewed and entry.timing_reviewed
                and entry.context == context and abs(entry.duration - duration) <= 0.05
                and len(entry.target_text) <= target_chars):
            eligible.append(entry)
    if len({e.target_text for e in eligible}) > 1:
        return None, "conflicting-reviewed-lines"
    return (eligible[0], "exact-reviewed-context") if eligible else (None, "no-eligible-match")


def suggestions(db, *, source_lang, target_locale, source_text, cutoff) -> list[dict]:
    rows = db.query(
        "SELECT m.document FROM translation_memory m WHERE source_lang=?"
        " AND target_locale=? AND source_text=? AND seq<=?"
        " AND NOT EXISTS (SELECT 1 FROM translation_memory n"
        " WHERE n.id=m.id AND n.seq>m.seq AND n.seq<=?) ORDER BY seq DESC LIMIT 5",
        (source_lang, target_locale, normalize_text(source_text), cutoff, cutoff),
    )
    result = []
    for row in rows:
        entry = MemoryEntry.model_validate_json(row["document"])
        if entry.status != "retired":
            result.append({"source": entry.source_text, "candidate": entry.target_text,
                           "status": entry.status, "context": entry.context,
                           "instruction": "Suggestion only: verify meaning in the current scene."})
    return result
