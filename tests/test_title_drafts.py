"""Deterministic foundation tests. No provider or audio calls."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from doblarr.knowledge import Entry, KnowledgeSelection, Realization, save_entry, save_realization
from doblarr.knowledge.store import snapshot
from doblarr.knowledge.title_drafts import (
    AnalysisIdentity,
    Applicability,
    Candidate,
    Cue,
    Draft,
    Provenance,
    Review,
    SharedRevision,
    SourceIdentity,
    fingerprint,
    load_draft,
    review_history,
    review_view,
    save_draft,
    save_review,
)
from doblarr.store import MIGRATIONS, Database


@pytest.fixture
def draft():
    raw = (Path(__file__).parent / "fixtures/title-knowledge-scene.json").read_bytes()
    cues = tuple(Cue(**c) for c in json.loads(raw)["cues"])
    base = Draft(
        source=SourceIdentity(title_ref="episode-1", series_ref="series-1", edition="test",
                              track="subtitle-0", language="en", parser_revision="fixture-1",
                              content_hash=hashlib.sha256(raw).hexdigest()),
        analysis=AnalysisIdentity(chunking_revision="windows-1",
                                  instructions_hash=fingerprint("v1"),
                                  provider="fixture", model="fixture", model_revision=None,
                                  settings_hash=fingerprint({"budget": 100}),
                                  inherited_revisions_hash=fingerprint([])),
        cues=cues, completed_checkpoints=("chunk-0", "chunk-1"),
    )
    candidates = tuple(Candidate(
        key=key, kind="alias", statement=statement, subjects=("person-unresolved-1",),
        applicability=Applicability(title_refs=("episode-1",), scene_refs=("scene-1",)),
        evidence=(base.cue_id(ordinal),), confidence=None,
        uncertainties=("Spelling and speaker identity are unverified.",),
        conflict_group="person-1-name",
        provenance=(Provenance(checkpoint=f"chunk-{ordinal}", stage="extract",
                               input_hash=base.analysis_fingerprint),),
    ) for key, statement, ordinal in (("name-a", "Mira", 0), ("name-b", "Myra", 1)))
    return Draft(**{**base.model_dump(), "candidates": candidates})


def revised(draft, **changes):
    return Draft.model_validate({**draft.model_dump(), **changes})


def test_source_mapping_roundtrip_and_checkpoint_reopen(tmp_path, draft):
    path = tmp_path / "draft.db"
    db = Database(path)
    assert save_draft(db, draft, expected_revision=0) == 1
    db._conn.close()
    db = Database(path)
    assert load_draft(db, draft.id) == (1, draft)
    assert draft.cues[0].text == "  <i>Mira, wait.</i>  "
    assert draft.cues[0].original_label == draft.cues[1].original_label
    assert draft.cue_id(0) != draft.cue_id(1)
    assert draft.candidates[1].evidence == (draft.cue_id(1),)
    assert draft.cues[1].start_ms < draft.cues[0].end_ms  # overlap preserved
    assert "Ignore previous instructions" in draft.cues[2].text
    complete = revised(draft, state="complete")
    assert save_draft(db, complete, expected_revision=1) == 2
    assert load_draft(db, draft.id, 1)[1].state == "partial"


@pytest.mark.parametrize("state", ["partial", "complete", "cancelled", "failed"])
def test_hard_isolation_and_existing_personal_rule_semantics(tmp_path, draft, state):
    db = Database(tmp_path / "draft.db")
    entry = save_entry(db, Entry(phrase="Mira", locale="en", status="proposed"))
    save_realization(db, Realization(entry_id=entry.id, replacement="Mee-ra", engine="fixture"))
    before = snapshot(db)
    draft = revised(draft, state=state)
    save_draft(db, draft, expected_revision=0)
    candidate = draft.candidates[0]
    save_review(db, draft.id, Review(candidate_id=draft.candidate_id(candidate),
                                    proposal_hash=candidate.digest, decision="accept",
                                    reviewer="test-human"), expected_revision=0)
    assert snapshot(db) == before
    for frozen in (None, before):
        selection = KnowledgeSelection.load(db, locale="en", snapshot=frozen)
        assert selection.spoken("Mira Myra", engine="fixture")[0] == "Mee-ra Myra"
    assert db.query_one("SELECT COUNT(*) AS n FROM knowledge_entries")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM translation_memory")["n"] == 0


def test_conflicts_identity_and_human_overlay_survive_reanalysis(tmp_path, draft):
    db = Database(tmp_path / "draft.db")
    save_draft(db, draft, expected_revision=0)
    candidate = draft.candidates[0]
    review = Review(candidate_id=draft.candidate_id(candidate), proposal_hash=candidate.digest,
                    decision="edit", correction="Meera", reviewer="test-human", note="Name credit")
    save_review(db, draft.id, review, expected_revision=0)
    changed = candidate.model_copy(update={"statement": "Mira?"})
    rerun = revised(draft, candidates=(draft.candidates[1], changed))
    save_draft(db, rerun, expected_revision=1)
    assert rerun.candidate_id(changed) == review.candidate_id
    view = review_view(db, draft.id)
    assert len(view) == 2  # conflicting spelling is never silently discarded
    assert {r["proposal"]["conflict_group"] for r in view} == {"person-1-name"}
    edited = next(r for r in view if r["candidate_id"] == review.candidate_id)
    assert edited["stale"] and edited["needs_review"]
    assert edited["review"]["correction"] == "Meera"
    assert load_draft(db, draft.id, 1)[1].candidates[0].statement == "Mira"
    with pytest.raises(ValueError, match="proposal changed"):
        save_review(db, draft.id, review, expected_revision=1)
    refreshed = review.model_copy(update={"proposal_hash": changed.digest})
    save_review(db, draft.id, refreshed, expected_revision=1)
    assert not review_view(db, draft.id)[1]["needs_review"]
    save_draft(db, revised(rerun, candidates=()), expected_revision=2)
    assert review_history(db, draft.id) == [(1, review), (2, refreshed)]


def test_validation_and_atomic_stale_writers(tmp_path, draft):
    db = Database(tmp_path / "draft.db")
    save_draft(db, draft, expected_revision=0)
    other = Database(db.path)
    with pytest.raises(ValueError, match="draft changed"):
        save_draft(other, draft, expected_revision=0)
    with pytest.raises(ValueError, match="immutable"):
        save_draft(db, revised(draft, cues=(draft.cues[0].model_copy(update={"text": "edit"}),
                                           *draft.cues[1:])), expected_revision=1)
    candidate = draft.candidates[0]
    for changes in ({"evidence": ("missing",)}, {"confidence": float("nan")},
                    {"confidence": 1.1}, {"uncertainties": ()}, {"locale": "es-MX"},
                    {"active": True}, {"applicability": {"title_refs": ["episode-2"]}}):
        with pytest.raises(ValidationError):
            revised(draft, candidates=({**candidate.model_dump(), **changes},))
    with pytest.raises(ValueError, match="duplicate"):
        revised(draft, candidates=(candidate, candidate))
    with pytest.raises(ValueError, match="incomplete checkpoint"):
        revised(draft, completed_checkpoints=())
    with pytest.raises(ValueError, match="reassigned"):
        save_draft(db, revised(draft, candidates=(candidate.model_copy(
            update={"subjects": ("different-person",)}),)), expected_revision=1)
    assert load_draft(db, draft.id)[0] == 1
    review = Review(candidate_id=draft.candidate_id(candidate), proposal_hash=candidate.digest,
                    decision="reject", reviewer="test-human")
    save_review(db, draft.id, review, expected_revision=0)
    with pytest.raises(ValueError, match="review changed"):
        save_review(other, draft.id, review, expected_revision=0)
    assert len(review_history(db, draft.id)) == 1


def test_fingerprints_and_locale_proposals(draft):
    candidate = draft.candidates[0]
    shared = SharedRevision(id="reviewed-title", revision=1, content_hash=fingerprint("reviewed"))
    locale = Candidate(**{**candidate.model_dump(), "kind": "pronunciation", "locale": "es-MX",
                           "shared_revision": shared})
    localized = revised(draft, candidates=(locale,))
    assert localized.analysis_fingerprint == draft.analysis_fingerprint
    assert localized.digest != draft.digest
    assert localized.candidate_id(locale) != draft.candidate_id(candidate)
    assert locale.digest != locale.model_copy(update={"locale": "es-VE"}).digest
    assert locale.digest != locale.model_copy(update={
        "shared_revision": shared.model_copy(update={"revision": 2})}).digest
    for field, value in (("chunking_revision", "v2"), ("settings_hash", fingerprint("new")),
                         ("inherited_revisions_hash", fingerprint("new")), ("model_revision", "2")):
        changed = revised(draft, analysis={**draft.analysis.model_dump(), field: value})
        assert changed.id == draft.id
        assert changed.analysis_fingerprint != draft.analysis_fingerprint
    changed_source = revised(draft, candidates=(), source={**draft.source.model_dump(),
                                                          "edition": "other"})
    assert changed_source.id != draft.id
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_upgrade_existing_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for migration in MIGRATIONS[:7]:
        migration(conn)
    conn.execute("PRAGMA user_version=7")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.query_one("PRAGMA user_version")[0] == len(MIGRATIONS)
    assert db.query("SELECT * FROM title_drafts") == []
    db._conn.close()
    assert Database(path).query("SELECT * FROM title_draft_reviews") == []
