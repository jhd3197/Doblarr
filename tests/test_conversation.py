"""Turn-taking checks over the audio that was actually rendered.

The distinction under test is the one that matters: an overlap the original
already had is a preserved interruption, and an overlap between two lines that
did not overlap is a collision the fit introduced.
"""

import math
import shutil
import struct
import wave

import pytest

from doblarr import conversation
from doblarr.cues import (
    RAW,
    SOURCE,
    Artifact,
    CueAudio,
    Selection,
    Span,
    Take,
    ensure_identity,
)
from doblarr.models import DubJob, Segment

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RATE = 48000


# Enough silence for the detector to have a floor to measure against. A clip
# that is loud from the first sample to the last has no dynamic range, and an
# energy detector cannot find speech in it — which is a real property of the
# detector, not a quirk of the fixture.
EDGE = 0.05


def _clip(path, lead, speech):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    head = max(lead, EDGE)
    for index in range(int(RATE * (head + speech + EDGE))):
        value = 0.0 if not (RATE * head <= index < RATE * (head + speech)) else (
            0.3 * math.sin(2 * math.pi * 330 * index / RATE))
        frames.append(struct.pack("<h", int(value * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def _job(tmp_path, rows):
    """`rows` is (index, start, speaker, lead, speech, source_start, source_end)."""
    media = tmp_path / "movie.mkv"
    media.write_text("video", encoding="utf-8")
    job = DubJob(input_file=media, source_lang="ja", target_lang="es")
    for index, start, speaker, lead, speech, s_start, s_end in rows:
        seg = Segment(index, start, start + lead + speech, f"line {index}",
                      speaker=speaker, text_translated=f"line {index}")
        seg.source.spans = [Span(s_start, s_end, SOURCE)]
        path = _clip(tmp_path / "work" / f"line_{index}.wav", lead, speech)
        seg.audio = CueAudio(
            takes=[Take(take_id=f"t{index}", fingerprint=f"gen-{index}",
                        state="generated",
                        raw=Artifact(role=RAW, path=str(path),
                                     fingerprint=f"gen-{index}"))],
            selection=Selection(take_id=f"t{index}", reason="auto"))
        seg.audio_clip = path
        job.segments.append(seg)
    ensure_identity(job)
    return job


def _codes(seg):
    return {f.code for f in seg.findings if f.disposition != "obsolete"}


def test_two_turns_that_do_not_touch_raise_nothing(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.0, 0.0, 1.0),
                          (1, 2.0, "B", 0.0, 1.0, 2.0, 3.0)])
    summary = conversation.check(job, {"mode": "phrase"})
    assert summary["collisions"] == 0 and summary["measured"] == 2
    assert _codes(job.segments[0]) == set()


def test_an_overlap_the_original_had_is_preserved_and_labelled_as_intended(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.5, 0.0, 1.5),
                          (1, 1.0, "B", 0.0, 1.0, 1.0, 2.0)])
    summary = conversation.check(job, {"mode": "phrase"})
    assert summary["intended_overlaps"] == 1 and summary["collisions"] == 0
    assert "timing_overlap_intended" in _codes(job.segments[0])


def test_an_overlap_the_original_did_not_have_is_a_collision_with_its_neighbour(tmp_path):
    # The rendered lines run into each other; the source intervals do not.
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.5, 0.0, 0.6),
                          (1, 1.0, "B", 0.0, 1.0, 2.0, 3.0)])
    conversation.check(job, {"mode": "phrase"})
    finding = next(f for f in job.segments[0].findings if f.code == "timing_collision")
    assert finding.evidence["with_line"] == 1
    assert finding.evidence["seconds"] > 0
    assert finding.evidence["source_overlap"] == 0.0
    assert finding.severity == "warning"


def test_one_speaker_talking_over_themselves_is_called_what_it_is(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.5, 0.0, 0.6),
                          (1, 1.0, "A", 0.0, 1.0, 2.0, 3.0)])
    conversation.check(job, {"mode": "phrase"})
    assert "timing_self_overlap" in _codes(job.segments[0])


def test_a_reviewer_can_accept_an_overlap_and_it_binds_to_that_exact_render(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.5, 0.0, 0.6),
                          (1, 1.0, "B", 0.0, 1.0, 2.0, 3.0)])
    render = job.segments[0].audio.current().fingerprint
    job.timing_edits[job.segments[0].cue_id] = {
        "overlap": {"accepted": True, "inputs": render}}
    summary = conversation.check(job, {"mode": "phrase"})
    assert summary["accepted_overlaps"] == 1 and summary["collisions"] == 0
    assert "timing_overlap_accepted" in _codes(job.segments[0])

    # Re-render the line: the acceptance was about audio that no longer exists.
    job.segments[0].audio.takes[0].raw.fingerprint = "gen-0-new"
    again = conversation.check(job, {"mode": "phrase"})
    assert again["collisions"] == 1 and again["accepted_overlaps"] == 0
    stale = next(f for f in job.segments[0].findings if f.code == "timing_collision")
    assert stale.evidence["accepted"]["stale"] is True


def test_the_onset_is_read_from_the_render_not_from_the_cue_window(tmp_path):
    """A clip that opens on silence does not collide with the line before it."""
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.4, 0.0, 0.6),
                          (1, 1.0, "B", 0.8, 1.0, 2.0, 3.0)])
    summary = conversation.check(job, {"mode": "phrase"})
    assert summary["collisions"] == 0


def test_a_line_with_no_audio_is_reported_as_unmeasured_not_as_clean(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.0, 0.0, 1.0)])
    job.segments[0].audio = CueAudio()
    job.segments[0].audio_clip = None
    summary = conversation.check(job, {"mode": "phrase"})
    assert summary["measured"] == 0 and summary["unmeasured"] == 1


def test_a_dry_run_measures_nothing(tmp_path):
    job = _job(tmp_path, [(0, 0.0, "A", 0.0, 1.5, 0.0, 0.6),
                          (1, 1.0, "B", 0.0, 1.0, 2.0, 3.0)])
    assert conversation.check(job, {"mode": "phrase"}, dry_run=True) == {}
    assert "conversation" not in job.metrics
