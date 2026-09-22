"""Reaction coverage: an explicit ledger, never an inserted guess.

Split in two halves, like the feature: what the subtitle parser is willing to
conclude, and what a coverage decision actually does to the audio.
"""

import math
import shutil
import struct
import wave

import pytest

from doblarr import reactions
from doblarr.budget import RequestBudget
from doblarr.cues import SOURCE, Span, ensure_identity, validate_events
from doblarr.models import DubJob, Segment
from doblarr.stages import prepare
from doblarr.stages.mix import placements

RATE = 48000


def _job(tmp_path, lines):
    media = tmp_path / "movie.mkv"
    media.write_text("video", encoding="utf-8")
    job = DubJob(input_file=media, source_lang="ja", target_lang="es")
    job.segments = [Segment(index, start, end, text)
                    for index, (start, end, text) in enumerate(lines)]
    for seg in job.segments:
        seg.source.spans = [Span(seg.start, seg.end, SOURCE)]
    ensure_identity(job)
    return job


def _audio(path, seconds=12.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(int(RATE * seconds)):
        value = 0.3 * math.sin(2 * math.pi * 330 * index / RATE)
        frames.append(struct.pack("<h", int(value * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


# -- what the parser will and will not conclude -----------------------------

def test_a_known_tag_becomes_a_typed_event_and_is_never_spoken(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "[laughter]"), (3.0, 4.0, "Hola.")])
    prepare.run(job)
    assert [s.text_src for s in job.segments] == ["Hola."]
    event = job.nonverbal[0]
    assert (event.type, event.category) == ("laugh", "vocal")
    assert event.text == "[laughter]" and event.evidence == "subtitle"
    # A label somebody typed is not a measurement.
    assert event.confidence is None
    validate_events(job.nonverbal)


def test_music_and_footsteps_are_background_events_not_voices(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "[music]"), (3.0, 4.0, "[footsteps]"),
                          (5.0, 6.0, "Hola.")])
    prepare.run(job)
    kinds = {(e.type, e.category, e.speaker) for e in job.nonverbal}
    assert kinds == {("music", "background", None), ("footsteps", "background", None)}


def test_an_unrecognised_bracketed_cue_keeps_its_words_and_stays_a_line(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "[speaking Korean]"), (3.0, 4.0, "Hola.")])
    prepare.run(job)
    # Not in the vocabulary, so it is not removed and not typed. Deleting a line
    # because a parser did not recognise it is the failure to avoid.
    assert [s.text_src for s in job.segments] == ["[speaking Korean]", "Hola."]
    assert job.nonverbal == []


def test_a_mixed_cue_keeps_its_words_and_still_records_the_reaction(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "[laughs] No puedo creerlo.")])
    prepare.run(job)
    assert [s.text_src for s in job.segments] == ["No puedo creerlo."]
    event = job.nonverbal[0]
    assert event.type == "laugh" and event.text == "[laughs]"
    # The original wording is kept, because the split could be wrong.
    assert event.checks["cue_text"] == "[laughs] No puedo creerlo."
    assert event.checks["position"] == "leading"
    assert job.metrics["mixed_cues_split"] == 1


def test_a_tag_in_the_middle_of_a_sentence_is_left_completely_alone(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "No [laughs] puedo creerlo.")])
    prepare.run(job)
    assert [s.text_src for s in job.segments] == ["No [laughs] puedo creerlo."]
    assert job.nonverbal == []


def test_an_event_id_survives_a_rerun_so_a_decision_is_not_orphaned(tmp_path):
    first = _job(tmp_path, [(1.0, 2.0, "[laughter]"), (3.0, 4.0, "Hola.")])
    prepare.run(first)
    second = _job(tmp_path, [(1.0, 2.0, "[laughter]"), (3.0, 4.0, "Hola.")])
    prepare.run(second)
    assert first.nonverbal[0].event_id == second.nonverbal[0].event_id


def test_a_person_can_add_an_event_no_subtitle_recorded(tmp_path):
    job = _job(tmp_path, [(3.0, 4.0, "Hola.")])
    reactions.inventory(job, {"mode": "review", "extra": [
        {"start": 6.0, "end": 6.8, "type": "gasp", "note": "she sees the door"}]})
    added = job.nonverbal[0]
    assert added.origin == "manual" and added.evidence == "manual"
    assert added.type == "gasp" and added.target.start == pytest.approx(6.0)


# -- what a decision does ---------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
class TestCoverage:
    def _prepared(self, tmp_path, decisions=None, **options):
        job = _job(tmp_path, [(1.0, 2.0, "Hola."), (9.0, 10.5, "[laughter]")])
        prepare.run(job)
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.source_audio = job.source_track
        job.artifacts_dir = tmp_path / "work"
        event = job.nonverbal[0]
        config = {"mode": "retain", **options}
        if decisions:
            config["events"] = {event.event_id: decisions}
        reactions.process(job, config, work_dir=tmp_path / "work")
        return job, job.nonverbal[0]

    def test_coverage_off_decides_nothing_and_places_nothing(self, tmp_path):
        job, event = self._prepared(tmp_path, mode="off")
        assert event.coverage == "unresolved" and event.artifact is None
        assert "off for this run" in event.reason

    def test_an_event_with_no_decision_stays_unresolved_and_says_why(self, tmp_path):
        job, event = self._prepared(tmp_path)
        assert event.coverage == "unresolved" and event.artifact is None
        codes = {f.code for f in event.findings}
        assert "reaction_uncovered" in codes
        note = next(f for f in event.findings if f.code == "reaction_uncovered")
        assert "not proof" in note.evidence["note"]

    def test_retaining_cuts_the_original_once_and_the_mix_places_it(self, tmp_path):
        job, event = self._prepared(tmp_path, {"decision": "retain"})
        assert event.coverage == "retained" and event.placed
        assert event.artifact.duration == pytest.approx(1.66, abs=0.05)
        placed = [row for row in placements(job) if row.kind == "event"]
        assert len(placed) == 1
        assert placed[0].start == pytest.approx(9.0)

    def test_switching_to_a_replacement_does_not_layer_the_two(self, tmp_path):
        job, event = self._prepared(tmp_path, {"decision": "retain"})
        retained = event.artifact.path
        asset = _audio(tmp_path / "sounds" / "laugh.wav", seconds=1.0)
        job2, event2 = self._prepared(
            tmp_path, {"decision": "replace", "asset": str(asset)})
        assert event2.coverage == "replaced"
        assert event2.artifact.path != retained
        assert len([row for row in placements(job2) if row.kind == "event"]) == 1

    def test_a_missing_replacement_is_a_visible_gap_not_a_silent_one(self, tmp_path):
        job, event = self._prepared(
            tmp_path, {"decision": "replace", "asset": str(tmp_path / "nope.wav")})
        assert event.coverage == "unavailable" and event.artifact is None
        assert "not on disk" in event.reason
        assert "reaction_uncovered" in {f.code for f in event.findings}

    def test_asking_an_engine_that_cannot_laugh_is_recorded_as_unsupported(self, tmp_path):
        job, event = self._prepared(tmp_path, {"decision": "replace"}, generate=True)
        assert event.coverage == "unsupported"
        assert event.checks["engine_nonverbal"] == "unknown"
        finding = next(f for f in event.findings if f.code == "reaction_unsupported")
        assert "not applied" in finding.evidence["note"]

    def test_omitting_places_nothing_and_says_it_was_deliberate(self, tmp_path):
        job, event = self._prepared(tmp_path, {"decision": "omit"})
        assert event.coverage == "omitted" and event.artifact is None
        assert placements(job) == [row for row in placements(job)
                                   if row.kind != "event"]

    def test_a_reaction_beside_dialogue_is_never_cut_from_the_original(self, tmp_path):
        job = _job(tmp_path, [(1.0, 2.0, "[laughs] No puedo creerlo.")])
        prepare.run(job)
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.artifacts_dir = tmp_path / "work"
        event = job.nonverbal[0]
        reactions.process(job, {"mode": "retain",
                                "events": {event.event_id: {"decision": "retain"}}},
                          work_dir=tmp_path / "work")
        event = job.nonverbal[0]
        assert event.coverage == "unresolved" and event.artifact is None
        assert "original actor's voice" in event.reason
        assert "reaction_unclean" in {f.code for f in event.findings}

    def test_a_speaking_line_across_the_window_is_flagged_but_not_fatal(self, tmp_path):
        job = _job(tmp_path, [(9.2, 10.0, "Hola."), (9.0, 10.5, "[laughter]")])
        prepare.run(job)
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.artifacts_dir = tmp_path / "work"
        event = job.nonverbal[0]
        reactions.process(job, {"mode": "retain",
                                "events": {event.event_id: {"decision": "retain"}}},
                          work_dir=tmp_path / "work")
        event = job.nonverbal[0]
        assert event.coverage == "retained"
        assert "reaction_unclean" in {f.code for f in event.findings}

    def test_two_events_over_the_same_moment_do_not_both_play(self, tmp_path):
        job = _job(tmp_path, [(9.0, 10.5, "[laughter]"), (9.2, 10.2, "[applause]")])
        prepare.run(job)
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.artifacts_dir = tmp_path / "work"
        decisions = {e.event_id: {"decision": "retain"} for e in job.nonverbal}
        reactions.process(job, {"mode": "retain", "events": decisions},
                          work_dir=tmp_path / "work")
        states = sorted(e.coverage for e in job.nonverbal)
        assert states == ["retained", "unresolved"]
        blocked = next(e for e in job.nonverbal if e.coverage == "unresolved")
        assert "play the sound twice" in blocked.reason
        assert "reaction_duplicate" in {f.code for f in blocked.findings}

    def test_a_leakage_screen_is_charged_and_its_silence_proves_nothing(self, tmp_path):
        class Recognizer:
            def __init__(self, text):
                self.text = text
                self.calls = 0

            def transcribe(self, path, language=""):
                self.calls += 1
                return {"text": self.text}

        job = _job(tmp_path, [(9.0, 10.5, "[laughter]")])
        prepare.run(job)
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.artifacts_dir = tmp_path / "work"
        event = job.nonverbal[0]
        vb = Recognizer("no me digas")
        budget = RequestBudget(0)
        reactions.process(job, {"mode": "retain", "leakage_check": True,
                                "events": {event.event_id: {"decision": "retain"}}},
                          vb=vb, budget=budget, work_dir=tmp_path / "work")
        event = job.nonverbal[0]
        assert vb.calls == 1 and budget.snapshot()["by_kind"] == {"reaction_screen": 1}
        assert event.coverage == "retained"      # a suspicion is not a rejection
        finding = next(f for f in event.findings if f.code == "reaction_unclean")
        assert "suspicion to listen to" in finding.evidence["note"]

    def test_an_audition_montage_places_no_reaction_at_all(self, tmp_path):
        job = _job(tmp_path, [(9.0, 10.5, "[laughter]")])
        prepare.run(job)
        job.kind = "audition"
        job.source_track = _audio(tmp_path / "work" / "source.wav")
        job.artifacts_dir = tmp_path / "work"
        event = job.nonverbal[0]
        reactions.process(job, {"mode": "retain",
                                "events": {event.event_id: {"decision": "retain"}}},
                          work_dir=tmp_path / "work")
        event = job.nonverbal[0]
        assert event.coverage == "unresolved" and event.artifact is None
        assert "own timeline" in event.reason

    def test_the_summary_never_claims_complete_coverage(self, tmp_path):
        job, _event = self._prepared(tmp_path)
        summary = job.metrics["coverage"]
        assert summary["unresolved"] == 1 and summary["placed"] == 0
        assert "not a claim that every sound" in summary["note"]



    def test_review_mode_prepares_the_sound_without_shipping_it(self, tmp_path):
        """Hearing a candidate reaction and putting it in the dub are separate."""
        job, event = self._prepared(tmp_path, {"decision": "retain"}, mode="review")
        assert event.coverage == "retained" and event.rendered
        assert event.in_mix is False and event.placed is False
        assert "not placed in the dub" in event.reason
        assert [row for row in placements(job) if row.kind == "event"] == []
        assert job.metrics["coverage"]["rendered"] == 1
        assert job.metrics["coverage"]["placed"] == 0

    def test_retain_mode_places_the_same_prepared_sound(self, tmp_path):
        job, event = self._prepared(tmp_path, {"decision": "retain"}, mode="retain")
        assert event.rendered and event.in_mix and event.placed
        assert len([row for row in placements(job) if row.kind == "event"]) == 1

# -- settings ---------------------------------------------------------------

def test_coverage_is_off_by_default():
    assert reactions.settings({})["mode"] == "off"
    assert not reactions.enabled({})


def test_an_unknown_coverage_mode_is_refused():
    with pytest.raises(ValueError, match="off, review or retain"):
        reactions.settings({"mode": "always"})


def test_engine_capability_is_asked_and_unknown_when_it_cannot_answer():
    assert reactions.engine_capability("qwen", None) == "unknown"

    class Silent:
        pass

    assert reactions.engine_capability("qwen", Silent()) == "unknown"

    class Honest:
        nonverbal_engines = ["special"]

    assert reactions.engine_capability("qwen", Honest()) == "unsupported"
    assert reactions.engine_capability("special", Honest()) == "supported"
