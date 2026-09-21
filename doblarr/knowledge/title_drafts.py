"""Local, inactive subtitle knowledge. No conversion to active Entry records exists.

All text is untrusted evidence. This module performs no provider calls, prompt
execution, export, speaker assignment, or production context resolution.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..store import Database

Text = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def fingerprint(value: object) -> str:
    """Canonical data hash; callers must pass data, never secrets or runtime objects."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @property
    def digest(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class SourceIdentity(Record):
    title_ref: Text
    series_ref: str = ""
    edition: Text
    track: Text
    language: Text
    content_hash: Digest  # SHA-256 of the original subtitle bytes, before parsing
    parser_revision: Text


class Cue(Record):
    ordinal: int = Field(ge=0)  # sequence position, not a possibly repeated subtitle label
    original_label: str = ""
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str  # preserve markup, whitespace, spelling, and instruction-like dialogue

    @model_validator(mode="after")
    def interval(self) -> Cue:
        if self.end_ms < self.start_ms:
            raise ValueError("cue end precedes start")
        return self


class Applicability(Record):
    # Explicit title allowlist avoids accidentally inheriting later revelations.
    title_refs: tuple[Text, ...] = Field(min_length=1)
    scene_refs: tuple[Text, ...] = ()  # empty means the listed titles, not the entire series


class AnalysisIdentity(Record):
    chunking_revision: Text
    instructions_hash: Digest
    provider: Text
    model: Text
    model_revision: str | None  # unknown stays unknown
    settings_hash: Digest
    inherited_revisions_hash: Digest


class Provenance(Record):
    checkpoint: Text
    stage: Literal["extract", "merge", "refine", "locale"]
    input_hash: Digest


class SharedRevision(Record):
    id: Text
    revision: int = Field(ge=1)
    content_hash: Digest


class Candidate(Record):
    # Assigned by reconciliation, retained across reruns; alternatives have distinct keys.
    key: Text
    kind: Literal["character", "alias", "term", "relationship", "summary", "delivery",
                  "localized_name", "terminology", "register", "honorific", "pronunciation"]
    statement: Text
    subjects: tuple[Text, ...] = ()  # stable entity keys, never diarization speaker IDs
    applicability: Applicability
    evidence: tuple[Text, ...] = Field(min_length=1)
    provenance: tuple[Provenance, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainties: tuple[Text, ...] = Field(min_length=1)
    conflict_group: str = ""  # all alternatives remain in the draft, no automatic winner
    locale: str | None = None
    shared_revision: SharedRevision | None = None

    @model_validator(mode="after")
    def layer(self) -> Candidate:
        localized = self.kind in {
            "localized_name", "terminology", "register", "honorific", "pronunciation"
        }
        if localized != bool(self.locale) or localized != (self.shared_revision is not None):
            raise ValueError("locale proposals require locale and accepted shared revision")
        return self


class Draft(Record):
    schema_version: Literal[1] = 1
    source: SourceIdentity
    analysis: AnalysisIdentity
    cues: tuple[Cue, ...]
    candidates: tuple[Candidate, ...] = ()
    state: Literal["partial", "complete", "cancelled", "failed"] = "partial"
    completed_checkpoints: tuple[Text, ...] = ()

    @property
    def id(self) -> str:
        return self.source.digest

    def cue_id(self, ordinal: int) -> str:
        return fingerprint([self.id, "cue", ordinal])

    def candidate_id(self, candidate: Candidate) -> str:
        return fingerprint([self.id, "candidate", candidate.key, candidate.locale])

    @property
    def analysis_fingerprint(self) -> str:
        return fingerprint([self.schema_version, self.source.digest, self.analysis.digest,
                            [c.digest for c in self.cues]])

    @model_validator(mode="after")
    def references(self) -> Draft:
        if tuple(c.ordinal for c in self.cues) != tuple(range(len(self.cues))):
            raise ValueError("cue ordinals must preserve contiguous original sequence")
        cue_ids = {self.cue_id(c.ordinal) for c in self.cues}
        ids = [self.candidate_id(c) for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate identity; alternatives need distinct keys")
        for candidate in self.candidates:
            if not set(candidate.evidence) <= cue_ids:
                raise ValueError("candidate references unknown source cues")
            if self.source.title_ref not in candidate.applicability.title_refs:
                raise ValueError("candidate applicability must include its evidence title")
            if not {p.checkpoint for p in candidate.provenance} <= set(self.completed_checkpoints):
                raise ValueError("candidate references an incomplete checkpoint")
        return self


class Review(Record):
    candidate_id: Digest
    proposal_hash: Digest
    decision: Literal["accept", "edit", "reject", "defer"]
    reviewer: Text
    note: str = ""
    correction: str | None = None

    @model_validator(mode="after")
    def edited(self) -> Review:
        if self.decision == "edit" and not (self.correction or "").strip():
            raise ValueError("an edit requires a human correction")
        if self.decision != "edit" and self.correction is not None:
            raise ValueError("only an edit may carry a correction")
        return self


def save_draft(db: Database, draft: Draft, *, expected_revision: int) -> int:
    """Append a checkpoint atomically; stale writers cannot overwrite newer work."""
    draft = Draft.model_validate(draft.model_dump())
    with db._lock, db._conn:
        # Acquire a SQLite write lock before reading, including across connections.
        db._conn.execute("BEGIN IMMEDIATE")
        row = db._conn.execute(
            "SELECT revision, document FROM title_drafts WHERE id=? ORDER BY revision DESC LIMIT 1",
            (draft.id,),
        ).fetchone()
        revision = row["revision"] if row else 0
        if revision != expected_revision:
            raise ValueError("draft changed; reload before saving")
        if row:
            previous = Draft.model_validate_json(row["document"])
            if previous.cues != draft.cues:
                raise ValueError("original cues are immutable; use a new source/parser identity")
            old = {previous.candidate_id(c): c for c in previous.candidates}
            for candidate in draft.candidates:
                prior = old.get(draft.candidate_id(candidate))
                if prior and (prior.kind, prior.subjects) != (candidate.kind, candidate.subjects):
                    raise ValueError("candidate keys cannot be reassigned to another kind/entity")
        revision += 1
        db._conn.execute(
            "INSERT INTO title_drafts(id,revision,title_ref,document) VALUES(?,?,?,?)",
            (draft.id, revision, draft.source.title_ref, draft.model_dump_json()),
        )
        return revision


def load_draft(db: Database, draft_id: str, revision: int | None = None) -> tuple[int, Draft]:
    sql = "SELECT revision,document FROM title_drafts WHERE id=?"
    params: tuple = (draft_id,)
    if revision is not None:
        sql += " AND revision=?"
        params += (revision,)
    row = db.query_one(sql + " ORDER BY revision DESC LIMIT 1", params)
    if row is None:
        raise KeyError(draft_id)
    return row["revision"], Draft.model_validate_json(row["document"])


def save_review(db: Database, draft_id: str, review: Review, *,
                expected_revision: int) -> int:
    """Append a human overlay against an exact proposal; acceptance is NOT activation."""
    review = Review.model_validate(review.model_dump())
    with db._lock, db._conn:
        db._conn.execute("BEGIN IMMEDIATE")
        _, draft = load_draft(db, draft_id)
        candidate = next((c for c in draft.candidates
                          if draft.candidate_id(c) == review.candidate_id), None)
        if candidate is None or candidate.digest != review.proposal_hash:
            raise ValueError("proposal changed or missing; reload before reviewing")
        row = db._conn.execute(
            "SELECT MAX(revision) AS r FROM title_draft_reviews"
            " WHERE draft_id=? AND candidate_id=?",
            (draft_id, review.candidate_id),
        ).fetchone()
        revision = row["r"] or 0
        if revision != expected_revision:
            raise ValueError("review changed; reload before saving")
        revision += 1
        db._conn.execute(
            "INSERT INTO title_draft_reviews(draft_id,candidate_id,revision,document)"
            " VALUES(?,?,?,?)",
            (draft_id, review.candidate_id, revision, review.model_dump_json()),
        )
        return revision


def review_history(db: Database, draft_id: str) -> list[tuple[int, Review]]:
    return [(r["revision"], Review.model_validate_json(r["document"])) for r in db.query(
        "SELECT revision,document FROM title_draft_reviews WHERE draft_id=?"
        " ORDER BY candidate_id,revision", (draft_id,),
    )]


def review_view(db: Database, draft_id: str) -> list[dict]:
    """Review-only projection. Stale edits stay visible; vanished candidates stay in history."""
    _, draft = load_draft(db, draft_id)
    latest = {review.candidate_id: (revision, review)
              for revision, review in review_history(db, draft_id)}
    result = []
    for candidate in draft.candidates:
        identity = draft.candidate_id(candidate)
        revision, review = latest.get(identity, (0, None))
        stale = review is not None and review.proposal_hash != candidate.digest
        result.append({"candidate_id": identity, "proposal": candidate.model_dump(mode="json"),
                       "review_revision": revision,
                       "review": review.model_dump(mode="json") if review else None,
                       "needs_review": review is None or stale or review.decision == "defer",
                       "stale": stale})
    return result
