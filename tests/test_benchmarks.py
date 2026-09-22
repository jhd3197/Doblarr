"""The offline fixture harness: a real end-to-end run with real FFmpeg.

Tone fixtures pin numeric and structural invariants. They say nothing about how
the dub sounds; that judgement needs real listening material.
"""

import json
import shutil

import pytest

from doblarr.benchmarks import (
    BOUNDARY_SCENE,
    LOCAL_COUNTERS,
    SCENE,
    baseline,
    compare,
    record,
)
from doblarr.cues import FITTED, NORMALIZED, RAW, SOURCE

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    return baseline(tmp_path_factory.mktemp("baseline"))


def test_baseline_runs_offline_through_every_layer(observed):
    # One non-spoken cue is dropped from synthesis; four lines are generated.
    assert observed["segments"] == 4
    assert observed["tts_requests"] == 4
    assert observed["metrics"]["tts_generated"] == 4
    assert observed["roles_available"] == [FITTED, NORMALIZED, RAW]
    assert observed["source_reference"]["stream_index"] is not None
    assert observed["source_reference"]["time_base"] == SOURCE

    for line in observed["lines"]:
        assert line["cue_id"] and line["take_id"]
        assert line["origin"] == "import"
        assert line["source_spans"][0]["domain"] == SOURCE
        assert line["rendered_role"] in {NORMALIZED, FITTED}


def test_baseline_keeps_nonspoken_evidence_without_synthesizing_it(observed):
    assert [e["text"] for e in observed["nonverbal"]] == ["[laughter]"]
    event = observed["nonverbal"][0]
    assert event["coverage"] == "uncovered" and event["type"] == "unknown"
    assert event["source"][0]["domain"] == SOURCE
    assert event["cue_id"] not in {line["cue_id"] for line in observed["lines"]}


def test_baseline_reports_its_request_budget(observed):
    spent = observed["metrics"]["request_budget"]
    assert spent["limit"] == 0 and spent["exhausted"] is False
    # The overlong line triggers exactly one shared repair charge.
    assert spent["by_kind"] == {"timing_repair": 1}
    assert spent["spent"] == sum(spent["by_kind"].values())


def test_overlong_line_is_fitted_and_reported(observed):
    overlong = [line for line in observed["lines"] if FITTED in line["roles"]]
    assert len(overlong) == 1
    assert ["timing_overflow", "open"] in [list(f) for f in overlong[0]["findings"]]
    assert observed["metrics"]["timing_flags"] == 1


def test_baseline_is_reproducible_and_comparable(tmp_path):
    # The same source document, rendered cold twice into separate work dirs —
    # which is exactly the shape of a before/after comparison.
    media = tmp_path / "fixture-media"
    first = record(tmp_path / "a", tmp_path / "a.json", note="first", media_root=media)
    second = record(tmp_path / "b", tmp_path / "b.json", note="second", media_root=media)
    left = json.loads(first.read_text(encoding="utf-8"))
    right = json.loads(second.read_text(encoding="utf-8"))

    assert [line["cue_id"] for line in left["lines"]] == [
        line["cue_id"] for line in right["lines"]]
    comparable = {k: v for k, v in left["metrics"].items() if k not in LOCAL_COUNTERS}
    assert comparable == {k: v for k, v in right["metrics"].items() if k not in LOCAL_COUNTERS}
    assert left["tts_requests"] == right["tts_requests"] == 4

    difference = compare(first, second)
    assert difference["tts_requests"] == [4, 4]
    assert difference["changed_cues"] == [] and difference["removed_cues"] == []
    assert difference["changed_counters"] == {}
    # Hashes of local file identity are labelled, not reported as a real change.
    assert set(difference["changed_local_counters"]) <= LOCAL_COUNTERS


def test_scene_covers_the_documented_fixture_cases():
    assert {c.speaker for c in SCENE} == {"SPEAKER_00", "SPEAKER_01"}
    assert any(c.lead > 0 and c.tail > 0 for c in SCENE)           # quiet onset/tail
    assert len({c.level for c in SCENE}) > 1                       # varied levels
    assert any(a.end > b.start for a, b in zip(SCENE, SCENE[1:], strict=False))  # overlap
    assert any(c.spoken > (c.end - c.start) for c in SCENE)        # overruns its slot


def test_boundary_preparation_removes_needless_timing_repairs(tmp_path):
    """Plan 02's comparison: same padded scene, preparation off then on."""
    media = tmp_path / "padded"
    off = baseline(tmp_path / "off", BOUNDARY_SCENE, media_root=media)
    on = baseline(tmp_path / "on", BOUNDARY_SCENE,
                  {"boundaries.trim": True, "boundaries.edge_fade_ms": 8},
                  media_root=media)

    # Padding was being mistaken for an overlong line on every cue.
    assert off["stretched_lines"] == len(off["lines"])
    assert on["stretched_lines"] < off["stretched_lines"]
    # The speech itself was never regenerated to achieve that.
    assert on["tts_requests"] == off["tts_requests"]
    assert on["metrics"].get("timing_repairs", 0) == off["metrics"].get("timing_repairs", 0)

    assert all(line["trim"] == "bypassed" for line in off["lines"])
    assert all(line["trim"] == "trimmed" for line in on["lines"])
    assert "trimmed" in on["roles_available"] and "trimmed" not in off["roles_available"]

    # A deliberate mid-line hesitation is still there afterwards.
    assert on["pauses_kept"] == 1
    hesitant = next(line for line in on["lines"] if line["pauses_kept"])
    assert hesitant["active_duration"] > SCENE[0].spoken

    # Cue identity, source intervals and takes are untouched by preparation.
    assert [line["cue_id"] for line in on["lines"]] == [line["cue_id"] for line in off["lines"]]
    assert [line["source_spans"] for line in on["lines"]] == [
        line["source_spans"] for line in off["lines"]]
    assert [line["take_id"] for line in on["lines"]] == [line["take_id"] for line in off["lines"]]


def test_conservative_handles_leave_edges_already_smooth(tmp_path):
    """With a protective handle the trim lands in silence, so no fade is needed."""
    on = baseline(tmp_path / "on", BOUNDARY_SCENE,
                  {"boundaries.trim": True, "boundaries.edge_fade_ms": 8},
                  media_root=tmp_path / "padded")
    assert on["edge_fades"] == 0
    assert "edged" not in on["roles_available"]
