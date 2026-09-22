"""Cue identity, the versioned codec, migration and the shared request budget."""

import json
import os
import threading
from pathlib import Path

import pytest

from doblarr.benchmarks import example_script
from doblarr.budget import RequestBudget
from doblarr.cues import (
    CUE_SCHEMA_VERSION,
    FITTED,
    MONTAGE,
    NORMALIZED,
    RAW,
    SOURCE,
    UNKNOWN,
    Artifact,
    Placement,
    SchemaError,
    Selection,
    Span,
    Take,
    adopt_legacy,
    apply_cue_payload,
    check_schema,
    cue_payload,
    ensure_identity,
    merge_cues,
    resolve_cue,
    script_ref,
    split_cue,
    validate_cues,
)
from doblarr.fingerprints import generation, mix, processing, verification
from doblarr.models import DubJob, Segment, Speaker
from doblarr.review import apply_edits
from doblarr.stages import prepare
from doblarr.stages.common import load_script, save_script


def _job(tmp_path: Path, mtime: float = 1000.0) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (mtime, mtime))
    return DubJob(input_file=src, source_lang="ja", target_lang="es")


# --------------------------------------------------------------------------
# Phase A — the worked example
# --------------------------------------------------------------------------

def test_worked_example_has_no_ambiguous_times(tmp_path):
    """Two speakers, an overlap, an edited placement and a montage mapping."""
    job = example_script(tmp_path)
    payload = [cue_payload(s) for s in job.segments]

    assert {s.speaker for s in job.segments} == {"GINKO", "NUI"}
    first, second, moved, montage = job.segments

    # Overlapping source intervals belong to different speakers.
    assert first.source.spans[0].end > second.source.spans[0].start
    assert first.speaker != second.speaker

    # An edited target position does not move the recorded source interval.
    assert (moved.start, moved.end) == (6.0, 8.0)
    assert (moved.source.spans[0].start, moved.source.spans[0].end) == (5.4, 7.4)
    assert moved.placement.offset == 0.6

    # Montage time and source time are separate, named domains.
    assert montage.placement.montage.domain == MONTAGE
    assert montage.source.spans[0].domain == SOURCE
    assert montage.placement.montage.start != montage.source.spans[0].start

    # Every serialized time carries its domain.
    for record in payload:
        for span in record["source"]["spans"]:
            assert span["domain"] in {SOURCE, MONTAGE}
        if record["placement"]["montage"]:
            assert record["placement"]["montage"]["domain"] == MONTAGE

    # The audio chain names each role and what it derives from.
    audio = payload[0]["audio"]
    assert audio["takes"][0]["raw"]["role"] == RAW
    assert [a["role"] for a in audio["renders"]] == [NORMALIZED, FITTED]
    assert audio["renders"][1]["derived_from"] == NORMALIZED


# --------------------------------------------------------------------------
# Phase B — schema, round trip, migration, validation
# --------------------------------------------------------------------------

def test_cue_records_round_trip(tmp_path):
    job = example_script(tmp_path)
    for seg in job.segments:
        restored = Segment(seg.index, seg.start, seg.end, seg.text_src)
        apply_cue_payload(restored, cue_payload(seg))
        assert cue_payload(restored) == cue_payload(seg)


def test_script_cache_round_trips_cue_records(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 1.0, 3.0, "uno", text_translated="uno"),
                    Segment(1, 4.0, 5.0, "dos", text_translated="dos")]
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00")}
    job.segments[0].placement = Placement(onset=0.2)
    work = tmp_path / "work"
    save_script(job, work)

    fresh = _job(tmp_path)
    assert load_script(fresh, work)
    assert [s.cue_id for s in fresh.segments] == [s.cue_id for s in job.segments]
    assert fresh.segments[0].placement.onset == 0.2
    assert fresh.segments[0].source.spans[0].as_dict() == {
        "start": 1.0, "end": 3.0, "domain": SOURCE}
    assert fresh.script_ref == job.script_ref


def test_legacy_snapshot_migrates_deterministically_and_never_claims_raw(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    job = _job(tmp_path)
    clip = tmp_path / "line_0000.wav"
    clip.write_bytes(b"audio")
    legacy = {
        "transcription_options": {}, "translation_options": {},
        "audio": None, "script_lang": None,
        "identity": {"input": str(job.input_file.resolve()), "source_lang": "ja",
                     "target_lang": "es", "subtitles": None},
        "script_is_target": False, "speakers": ["SPEAKER_00"],
        "segments": [{"index": 0, "start": 1.0, "end": 3.0, "text_src": "uno",
                      "speaker": "SPEAKER_00", "words": [], "issues": [],
                      "delivery": "", "voice": None, "revision": 0,
                      "source_start": None, "tts_text": None, "applied_rules": [],
                      "translation_provenance": {}, "memory_context": {},
                      "text_translated": "uno"}],
    }
    path = work / "movie.script.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    os.utime(path, (2000.0, 2000.0))

    first = _job(tmp_path)
    assert load_script(first, work)
    seg = first.segments[0]
    assert seg.cue_id and seg.lineage.origin == "legacy" and seg.lineage.legacy_index == 0
    assert seg.source.spans[0].as_dict() == {"start": 1.0, "end": 3.0, "domain": SOURCE}
    # A pre-schema script cache never stored a clip path, so no audio is claimed.
    assert seg.audio.raw() is None and seg.audio.renders == []

    # Repeating the migration produces the same cue IDs.
    path.write_text(json.dumps(legacy), encoding="utf-8")
    os.utime(path, (2000.0, 2000.0))
    second = _job(tmp_path)
    assert load_script(second, work)
    assert second.segments[0].cue_id == seg.cue_id


def test_legacy_clip_is_adopted_as_unknown_not_raw(tmp_path):
    """A stored clip may already be normalized or fitted; it is never called raw."""
    seg = Segment(0, 2.0, 4.0, "uno", audio_clip=tmp_path / "line_0000.wav")
    seg.source_start = 40.0  # a montage row: its target time is not its source time
    adopt_legacy(seg, "script-ref")
    assert seg.audio.raw() is None
    assert [a.role for a in seg.audio.renders] == [UNKNOWN]
    assert seg.audio.renders[0].proven is False
    assert seg.source.spans[0].as_dict() == {"start": 40.0, "end": 42.0, "domain": SOURCE}
    assert seg.placement.montage.as_dict() == {"start": 2.0, "end": 4.0, "domain": MONTAGE}
    assert adopt_legacy.__doc__  # deterministic: same snapshot, same identity
    other = Segment(0, 2.0, 4.0, "uno")
    adopt_legacy(other, "script-ref")
    assert other.cue_id == seg.cue_id


def test_future_schema_fails_clearly(tmp_path):
    with pytest.raises(SchemaError, match="Upgrade Doblarr"):
        check_schema(CUE_SCHEMA_VERSION + 1, "script cache")
    assert check_schema(None) == 0
    assert check_schema(CUE_SCHEMA_VERSION) == CUE_SCHEMA_VERSION


def test_future_script_cache_is_rejected_not_guessed(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 1.0, 2.0, "uno")]
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00")}
    work = tmp_path / "work"
    path = save_script(job, work)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cue_schema"] = CUE_SCHEMA_VERSION + 5
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (2000.0, 2000.0))
    with pytest.raises(SchemaError, match="cue schema"):
        load_script(_job(tmp_path), work)


@pytest.mark.parametrize("bad", [
    {"start": 2.0, "end": 1.0, "domain": SOURCE},
    {"start": -1.0, "end": 1.0, "domain": SOURCE},
    {"start": 0.0, "end": float("inf"), "domain": SOURCE},
    {"start": 0.0, "end": 1.0, "domain": "wall-clock"},
    {"start": "soon", "end": 1.0, "domain": SOURCE},
])
def test_malformed_spans_are_actionable_errors(bad):
    with pytest.raises(SchemaError):
        Span.from_dict(bad)


def test_validation_catches_duplicate_ids_and_dangling_references(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 0.0, 1.0, "uno"), Segment(1, 1.0, 2.0, "dos")]
    ensure_identity(job)
    validate_cues(job.segments, job.cue_lineage)

    job.segments[1].cue_id = job.segments[0].cue_id
    with pytest.raises(SchemaError, match="duplicate cue id"):
        validate_cues(job.segments, job.cue_lineage)

    job.segments[1].cue_id = "other"
    job.segments[1].audio.selection = Selection(take_id="missing")
    with pytest.raises(SchemaError, match="does not have"):
        validate_cues(job.segments, job.cue_lineage)

    job.segments[1].audio.selection = None
    job.segments[1].audio.renders = [Artifact(role=NORMALIZED, path="a"),
                                     Artifact(role=NORMALIZED, path="b")]
    with pytest.raises(SchemaError, match="two artifacts for one role"):
        validate_cues(job.segments, job.cue_lineage)


def test_target_edits_cannot_move_source_ranges(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 1.0, 3.0, "uno", text_translated="uno")]
    ensure_identity(job)
    original = job.segments[0].source.spans[0].as_dict()
    apply_edits(job, {"0": {"start": 4.0, "end": 6.0, "text": "otra"}})
    assert (job.segments[0].start, job.segments[0].end) == (4.0, 6.0)
    assert job.segments[0].source.spans[0].as_dict() == original
    assert job.segments[0].placement.offset == pytest.approx(3.0)


def test_split_and_merge_retire_ids_and_refuse_to_redirect_edits(tmp_path):
    job = _job(tmp_path)
    parent = Segment(0, 0.0, 2.0, "uno dos", text_translated="uno dos")
    job.segments = [parent]
    ensure_identity(job)
    parent_id = parent.cue_id

    children = [Segment(0, 0.0, 1.0, "uno", text_translated="uno"),
                Segment(1, 1.0, 2.0, "dos", text_translated="dos")]
    split_cue(job, parent, children)
    job.segments = children
    assert job.cue_lineage[parent_id] == [c.cue_id for c in children]
    assert parent_id not in {c.cue_id for c in children}
    validate_cues(job.segments, job.cue_lineage)

    with pytest.raises(SchemaError, match="split or merged into"):
        resolve_cue(job.segments, parent_id, job.cue_lineage)
    with pytest.raises(SchemaError, match="split or merged into"):
        apply_edits(job, {"0": {"cue": parent_id, "text": "nueva"}})

    # Merging the two children again mints a third ID and retires both, and the
    # chain from the original parent is flattened onto the surviving cue.
    merged = children[0]
    merge_cues(job, list(children), merged)
    job.segments = [merged]
    assert job.cue_lineage[children[1].cue_id] == [merged.cue_id]
    assert job.cue_lineage[parent_id] == [merged.cue_id]
    assert merged.cue_id not in job.cue_lineage
    validate_cues(job.segments, job.cue_lineage)


def test_prepare_merges_cues_and_keeps_nonspoken_evidence(tmp_path):
    job = _job(tmp_path)
    job.segments = [
        Segment(0, 0.0, 1.0, "primera parte"),
        Segment(1, 1.1, 2.0, "y la segunda."),
        Segment(2, 3.0, 4.0, "[laughter]"),
    ]
    ensure_identity(job)
    before = [s.cue_id for s in job.segments]
    prepare.run(job)

    assert [s.text_src for s in job.segments] == ["primera parte y la segunda."]
    merged = job.segments[0]
    assert merged.cue_id not in before
    assert merged.lineage.origin == "merge" and merged.lineage.parents == before[:2]
    assert [(s.start, s.end) for s in merged.source.spans] == [(0.0, 1.0), (1.1, 2.0)]
    assert job.cue_lineage[before[0]] == [merged.cue_id]

    # The removed cue is not synthesized, but its timed evidence survives.
    assert job.nonverbal[0]["cue_id"] == before[2]
    assert job.nonverbal[0]["type"] == "unknown"
    assert job.nonverbal[0]["source"] == [{"start": 3.0, "end": 4.0, "domain": SOURCE}]
    validate_cues(job.segments, job.cue_lineage)


def test_cue_ids_are_independent_of_wording_and_position(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 0.0, 1.0, "uno", text_translated="uno")]
    ensure_identity(job)
    before = job.segments[0].cue_id
    job.segments[0].text_translated = "completamente distinto"
    job.segments[0].index = 41
    job.segments[0].start, job.segments[0].end = 90.0, 92.0
    job.segments[0].voice = "another-voice"
    ensure_identity(job)
    assert job.segments[0].cue_id == before

    # And they are scoped to the source document, not the target language.
    assert script_ref(job.input_file, "ja") == script_ref(job.input_file, "ja")
    assert script_ref(job.input_file, "ja") != script_ref(job.input_file, "ko")


# --------------------------------------------------------------------------
# Phase C — fingerprints, selection and the shared budget
# --------------------------------------------------------------------------

def test_fingerprint_namespaces_are_independent():
    request = {"same": "inputs"}
    ids = {generation(request), processing(request), mix(request), verification(request)}
    assert len(ids) == 4


def test_changing_take_invalidates_only_derived_audio(tmp_path):
    seg = Segment(0, 0.0, 1.0, "uno")
    first = Take(take_id="a", fingerprint="gen-a",
                 raw=Artifact(role=RAW, path=str(tmp_path / "a.wav")))
    seg.audio.takes.append(first)
    seg.audio.selection = Selection(take_id="a")
    seg.audio.put_render(Artifact(role=NORMALIZED, path=str(tmp_path / "a.norm.wav")))
    seg.audio.put_render(Artifact(role=FITTED, path=str(tmp_path / "a.fit.wav")))
    assert seg.audio.current().role == FITTED

    seg.audio.takes.append(Take(take_id="b", fingerprint="gen-b",
                                raw=Artifact(role=RAW, path=str(tmp_path / "b.wav"))))
    seg.audio.selection = Selection(take_id="b", previous="a")
    seg.audio.drop_renders((NORMALIZED, FITTED))
    assert seg.audio.renders == []
    assert seg.audio.current().path.endswith("b.wav")
    assert seg.audio.take("a").raw.path.endswith("a.wav")  # the old raw survives


def test_request_budget_is_shared_bounded_and_cancellable():
    budget = RequestBudget(limit=2)
    assert budget.charge("quality_retry")
    assert budget.charge("timing_repair")
    assert not budget.charge("quality_retry")  # the cap is shared, not per stage
    assert budget.exhausted and budget.remaining == 0
    assert budget.snapshot()["by_kind"] == {"quality_retry": 1, "timing_repair": 1}
    assert budget.snapshot()["refused"] == 1

    cancel = threading.Event()
    open_budget = RequestBudget(limit=0, cancel=cancel)
    assert open_budget.charge("quality_retry")
    assert open_budget.remaining is None  # 0 means counted, never capped
    cancel.set()
    assert not open_budget.charge("quality_retry")
