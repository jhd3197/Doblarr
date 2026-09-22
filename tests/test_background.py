"""Background integrity: what the bed is, what may have been taken out of it,
and what may have been left in. Every answer here is bounded by what separation
can actually establish, which is less than it looks like.
"""

import math
import shutil
import struct
import wave

import pytest

from doblarr import background, reactions
from doblarr.budget import RequestBudget
from doblarr.cues import SOURCE, Span, ensure_identity
from doblarr.models import DubJob, Segment
from doblarr.stages import prepare

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RATE = 48000


def _audio(path, seconds=14.0, amplitude=0.3, quiet=None):
    """A tone, optionally near-silent over one (start, end) window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(int(RATE * seconds)):
        position = index / RATE
        level = amplitude
        if quiet and quiet[0] <= position < quiet[1]:
            level = 0.0005
        frames.append(struct.pack(
            "<h", int(level * math.sin(2 * math.pi * 330 * index / RATE) * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def _job(tmp_path, lines):
    media = tmp_path / "movie.mkv"
    media.write_text("video", encoding="utf-8")
    job = DubJob(input_file=media, source_lang="ja", target_lang="es")
    job.segments = [Segment(index, start, end, text)
                    for index, (start, end, text) in enumerate(lines)]
    for seg in job.segments:
        seg.source.spans = [Span(seg.start, seg.end, SOURCE)]
    ensure_identity(job)
    job.artifacts_dir = tmp_path / "work"
    return job


def test_an_unseparated_run_is_labelled_as_the_original_mix(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Hola.")])
    job.source_audio = _audio(tmp_path / "work" / "source.wav")
    job.background = job.source_audio        # the no-Demucs fallback
    described = background.kind(job)
    assert described["separated"] is False
    assert "original mix" in described["label"]
    assert "still in the bed" in described["note"]


def test_a_separated_run_says_the_bed_is_an_estimate(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Hola.")])
    job.source_audio = _audio(tmp_path / "work" / "source.wav")
    job.background = _audio(tmp_path / "work" / "bed.wav")
    described = background.kind(job)
    assert described["separated"] is True
    assert "not a studio M&E stem" in described["note"]


def test_a_sound_the_separator_removed_is_reported_with_both_numbers(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Hola."), (9.0, 10.5, "[laughter]")])
    prepare.run(job)
    job.source_track = _audio(tmp_path / "work" / "source.wav")
    job.source_audio = job.source_track
    # The bed is near-silent exactly where the laugh was, which is what a
    # removed vocal reaction looks like.
    job.background = _audio(tmp_path / "work" / "bed.wav", quiet=(8.5, 11.0))
    event = job.nonverbal[0]
    reactions.process(job, {"mode": "retain",
                            "events": {event.event_id: {"decision": "omit"}}},
                      work_dir=tmp_path / "work")
    background.check(job, {"mode": "retain"}, work_dir=tmp_path / "work")
    finding = next(f for f in job.nonverbal[0].findings
                   if f.code == "background_missing_effect")
    assert finding.evidence["drop_db"] >= background.MISSING_DB
    assert finding.evidence["source_db"] is not None
    # It says what else produces the same measurement.
    assert "quiet sound in a loud scene" in finding.evidence["note"]
    assert job.metrics["background"]["missing"] == 1


def test_no_missing_effect_is_claimed_without_a_separated_bed(tmp_path):
    job = _job(tmp_path, [(9.0, 10.5, "[laughter]")])
    prepare.run(job)
    job.source_track = _audio(tmp_path / "work" / "source.wav")
    job.source_audio = job.source_track
    job.background = job.source_audio
    reactions.process(job, {"mode": "retain"}, work_dir=tmp_path / "work")
    summary = background.check(job, {"mode": "retain"}, work_dir=tmp_path / "work")
    assert summary["missing"] == 0 and summary["separated"] is False


def test_words_heard_in_a_dialogue_free_window_are_a_suspicion_with_its_caveats(tmp_path):
    class Recognizer:
        def __init__(self):
            self.calls = []

        def transcribe(self, path, language=""):
            self.calls.append(path)
            return {"text": "quien esta ahi", "confidence": 0.71}

    job = _job(tmp_path, [(1.0, 2.0, "Hola."), (9.0, 10.0, "Adios.")])
    job.source_track = _audio(tmp_path / "work" / "source.wav")
    job.source_audio = job.source_track
    job.background = _audio(tmp_path / "work" / "bed.wav")
    vb = Recognizer()
    budget = RequestBudget(0)
    summary = background.check(job, {"mode": "retain", "leakage_check": True},
                               work_dir=tmp_path / "work", vb=vb, budget=budget)
    assert summary["leaks"] >= 1 and summary["screened"] >= 1
    finding = next(f for seg in job.segments for f in seg.findings
                   if f.code == "background_leakage")
    assert finding.confidence == pytest.approx(0.71)
    assert "reverberation and cross-talk" in finding.evidence["note"]
    assert budget.snapshot()["by_kind"]["background_screen"] == summary["screened"]


def test_a_silent_recognizer_is_recorded_as_nothing_found_not_as_a_clean_bed(tmp_path):
    class Silent:
        def transcribe(self, path, language=""):
            return {"text": ""}

    job = _job(tmp_path, [(1.0, 2.0, "Hola."), (9.0, 10.0, "Adios.")])
    job.source_track = _audio(tmp_path / "work" / "source.wav")
    job.source_audio = job.source_track
    job.background = _audio(tmp_path / "work" / "bed.wav")
    summary = background.check(job, {"mode": "retain", "leakage_check": True},
                               work_dir=tmp_path / "work", vb=Silent())
    assert summary["leaks"] == 0 and summary["screened"] >= 1
    codes = {f.code for seg in job.segments for f in seg.findings}
    # No "clean" finding exists to raise. Recognition hearing nothing is not a
    # result worth recording as one.
    assert "background_clean" not in codes


def test_screening_is_skipped_entirely_when_it_was_not_asked_for(tmp_path):
    class Loud:
        def __init__(self):
            self.calls = 0

        def transcribe(self, path, language=""):
            self.calls += 1
            return {"text": "algo"}

    job = _job(tmp_path, [(1.0, 2.0, "Hola."), (9.0, 10.0, "Adios.")])
    job.source_track = _audio(tmp_path / "work" / "source.wav")
    job.source_audio = job.source_track
    job.background = _audio(tmp_path / "work" / "bed.wav")
    vb = Loud()
    summary = background.check(job, {"mode": "retain"}, work_dir=tmp_path / "work", vb=vb)
    assert vb.calls == 0 and summary["screened"] == 0


def test_coverage_off_reports_the_bed_and_measures_nothing(tmp_path):
    job = _job(tmp_path, [(1.0, 2.0, "Hola.")])
    job.source_audio = _audio(tmp_path / "work" / "source.wav")
    job.background = _audio(tmp_path / "work" / "bed.wav")
    summary = background.check(job, {"mode": "off"}, work_dir=tmp_path / "work")
    assert summary["separated"] is True and summary["screened"] == 0
    assert job.metrics["background"] == summary
