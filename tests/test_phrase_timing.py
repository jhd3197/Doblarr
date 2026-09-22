"""The phrase renderer, against real FFmpeg output rather than command strings.

A mocked command line proves the arguments were assembled. It cannot show that
the joins are silent, that every phrase survived, that the clip is the length
the recipe asked for, or that a second run reuses the first one's file — which
is the entire content of this stage.
"""

import math
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from doblarr import phrases
from doblarr.budget import RequestBudget
from doblarr.cues import (
    PHRASED,
    RAW,
    TRIMMED,
    Artifact,
    CueAudio,
    Selection,
    Take,
    ensure_identity,
)
from doblarr.models import DubJob, Segment
from doblarr.stages import boundaries, phrase_timing
from doblarr.stages.common import DryRunPlan

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RATE = 48000


def _write(path, blocks):
    """A mono 16-bit take built from (seconds, amplitude) blocks."""
    frames = []
    phase = 0
    for seconds, amplitude in blocks:
        for _ in range(int(RATE * seconds)):
            value = amplitude * math.sin(2 * math.pi * 330 * phase / RATE)
            frames.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000)))
            phase += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def _job(tmp_path, slot, blocks, *, text="Primera parte. Segunda parte."):
    media = tmp_path / "movie.mkv"
    media.write_text("video", encoding="utf-8")
    job = DubJob(input_file=media, source_lang="ja", target_lang="es")
    seg = Segment(0, 1.0, 1.0 + slot, text, text_translated=text)
    take_path = _write(tmp_path / "work" / "clips" / "line_0000.wav", blocks)
    seg.audio = CueAudio(
        takes=[Take(take_id="t0", fingerprint="gen-0", engine="tone", state="generated",
                    raw=Artifact(role=RAW, path=str(take_path), fingerprint="gen-0",
                                 duration=sum(b[0] for b in blocks)))],
        selection=Selection(take_id="t0", reason="auto"))
    seg.audio_clip = take_path
    job.segments = [seg]
    ensure_identity(job)
    return job, seg


def _options(**overrides):
    return {"mode": "phrase", **overrides}


def _duration(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


# -- the two owners ---------------------------------------------------------

def test_whole_clip_mode_leaves_this_stage_completely_out_of_it(tmp_path):
    job, seg = _job(tmp_path, 3.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options={"mode": "whole"})
    assert seg.phrasing.state == "bypassed" and seg.phrasing.mode == "whole"
    assert seg.audio.render(PHRASED) is None


def test_a_dry_run_plans_nothing_and_writes_nothing(tmp_path):
    job, seg = _job(tmp_path, 1.5, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    assert isinstance(
        phrase_timing.run.__wrapped__(job, tmp_path / "work", options=_options(),
                                      dry_run=True), DryRunPlan)
    assert seg.audio.render(PHRASED) is None
    assert not (tmp_path / "work" / "clips" / "phrased").exists()


# -- rendering --------------------------------------------------------------

def test_a_line_that_fits_reuses_its_prepared_take_untouched(tmp_path):
    job, seg = _job(tmp_path, 4.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options())
    assert seg.phrasing.state == "applied"
    assert "already fits" in seg.phrasing.reason
    assert seg.audio.render(PHRASED) is None          # no new file at all
    assert Path(seg.audio_clip) == Path(seg.audio.raw().path)
    assert job.metrics["phrase_rendered"] == 0


def test_padding_is_spent_and_the_rendered_clip_is_the_length_planned(tmp_path):
    # 1.6s of speech with 0.6s of padding between, in a 2.0s window.
    job, seg = _job(tmp_path, 2.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    plan = seg.phrasing
    assert plan.state == "applied" and plan.moved > 0.1
    rendered = seg.audio.render(PHRASED)
    assert rendered is not None and rendered.derived_from == RAW
    # Measured, not asserted from the recipe.
    assert _duration(rendered.path) == pytest.approx(plan.planned_duration, abs=0.03)
    assert plan.actual_duration == pytest.approx(_duration(rendered.path), abs=0.001)
    assert plan.max_stretch == 1.0    # padding alone was enough


def test_a_protected_pause_survives_the_fit_and_speech_compresses_instead(tmp_path):
    job, seg = _job(tmp_path, 1.9, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=0.3))
    plan = seg.phrasing
    gap = next(p for p in plan.pauses if p.origin == "auto")
    assert gap.protected
    assert gap.planned == pytest.approx(gap.measured, abs=0.01)
    assert plan.max_stretch > 1.0
    # And the pause is still audible in the output: two runs, not one.
    bounds = boundaries.inspect(Path(seg.audio.render(PHRASED).path))
    runs, _lead, _tail, _gaps = phrases.runs_from(
        bounds, phrases.settings(_options()))
    assert len(runs) == 2


def test_every_phrase_reaches_the_output_and_none_is_duplicated(tmp_path):
    job, seg = _job(tmp_path, 2.0, [(0.5, 0.3), (0.5, 0.0), (0.5, 0.3), (0.5, 0.0),
                                    (0.5, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    plan = seg.phrasing
    assert len(plan.phrases) == 3
    placed = [p["phrase"] for p in plan.pieces if p["kind"] == "speech"]
    assert placed == [p.phrase_id for p in plan.phrases]      # in order, once each
    # Speech seconds are conserved up to the compression that was actually
    # applied — nothing was dropped to make the numbers work.
    assert plan.speech_out == pytest.approx(plan.speech_in / plan.max_stretch, rel=0.08)


def test_the_render_is_reused_rather_than_rebuilt_on_a_second_identical_run(tmp_path):
    job, seg = _job(tmp_path, 2.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    first = seg.audio.render(PHRASED)
    stamp = (first.path, os.stat(first.path).st_mtime_ns)
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    second = seg.audio.render(PHRASED)
    assert (second.path, os.stat(second.path).st_mtime_ns) == stamp
    assert second.fingerprint == first.fingerprint


def test_changing_a_setting_re_renders_from_the_take_not_from_the_last_render(tmp_path):
    job, seg = _job(tmp_path, 2.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    loose = seg.audio.render(PHRASED)
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=0.3))
    tight = seg.audio.render(PHRASED)
    assert tight.path != loose.path and tight.fingerprint != loose.fingerprint
    # The input is the raw take both times. Reading its own previous output is
    # how a clip accumulates stretching run after run.
    assert tight.derived_from == RAW


def test_it_reads_the_prepared_derivative_when_boundary_trimming_ran(tmp_path):
    job, seg = _job(tmp_path, 2.0, [(0.4, 0.0), (0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    trimmed = _write(tmp_path / "work" / "clips" / "prepared.wav",
                     [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    seg.audio.put_render(Artifact(role=TRIMMED, path=str(trimmed), fingerprint="trim-0",
                                  derived_from=RAW, duration=2.2))
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    assert seg.audio.render(PHRASED).derived_from == TRIMMED


# -- when it does not fit ---------------------------------------------------

def test_a_line_that_cannot_fit_is_still_rendered_at_the_bounded_maximum(tmp_path):
    job, seg = _job(tmp_path, 0.8, [(0.8, 0.3), (0.3, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(), max_attempts=0)
    plan = seg.phrasing
    assert plan.state == "infeasible"
    assert seg.audio.render(PHRASED) is not None     # the words are still there
    assert plan.max_stretch <= 1.3 + 1e-9
    assert "timing_overflow" in seg.issues
    codes = {f.code for f in seg.findings}
    assert "timing_infeasible" in codes


def test_an_existing_take_that_fits_is_preferred_over_asking_for_a_rewrite(tmp_path):
    job, seg = _job(tmp_path, 1.2, [(0.8, 0.3), (0.3, 0.0), (0.8, 0.3)])
    short = _write(tmp_path / "work" / "clips" / "line_0000.alt.wav",
                   [(0.4, 0.3), (0.2, 0.0), (0.4, 0.3)])
    seg.audio.takes.append(Take(
        take_id="t1", fingerprint="gen-1", engine="tone", state="generated",
        origin="candidate",
        raw=Artifact(role=RAW, path=str(short), fingerprint="gen-1", duration=1.0)))

    class Translator:
        provider_calls = 0
        last_usage: list = []

        def shorten(self, *args, **kwargs):
            raise AssertionError("a rewrite must not be requested while a take fits")

    budget = RequestBudget(0)
    phrase_timing.run(job, tmp_path / "work", options=_options(),
                      translator=Translator(), regenerate=lambda seg: None,
                      budget=budget, max_attempts=2)
    assert seg.audio.selection.take_id == "t1"
    assert seg.audio.selection.reason == "restored"
    assert job.metrics["phrase_take_repairs"] == 1
    # Choosing a take that already exists is not a provider request.
    assert budget.snapshot()["spent"] == 0


def test_a_rewrite_is_charged_to_the_one_shared_budget_and_can_be_refused(tmp_path):
    job, seg = _job(tmp_path, 0.6, [(0.8, 0.3), (0.3, 0.0), (0.8, 0.3)])
    asked = []

    class Translator:
        provider_calls = 0
        last_usage: list = []

        def shorten(self, text, language, budget):
            asked.append(budget)
            return "Corto."

    budget = RequestBudget(1)
    budget.charge("asr")            # already spent by something else
    phrase_timing.run(job, tmp_path / "work", options=_options(),
                      translator=Translator(), regenerate=lambda seg: None,
                      budget=budget, max_attempts=2)
    assert asked == []              # refused before the request was made
    assert job.metrics["timing_repairs_refused"] == 1
    assert "budget is exhausted" in seg.phrasing.reason


# -- reviewer control -------------------------------------------------------

def test_a_bypass_leaves_the_take_exactly_as_generated(tmp_path):
    job, seg = _job(tmp_path, 1.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    job.timing_edits[seg.cue_id] = {"bypass": True}
    phrase_timing.run(job, tmp_path / "work", options=_options())
    assert seg.phrasing.state == "bypassed" and seg.phrasing.bypassed
    assert seg.audio.render(PHRASED) is None
    assert seg.audio_clip == Path(seg.audio.raw().path)


def test_a_hard_anchor_moves_a_phrase_and_the_landing_is_measured(tmp_path):
    job, seg = _job(tmp_path, 3.0, [(0.6, 0.3), (0.4, 0.0), (0.6, 0.3)])
    plan_id = f"{seg.cue_id}:p1"
    job.timing_edits[seg.cue_id] = {
        "anchors": [{"phrase": plan_id, "edge": "start", "at": 1.6}]}
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    plan = seg.phrasing
    piece = next(p for p in plan.pieces if p.get("phrase") == plan_id)
    assert piece["at"] == pytest.approx(1.6)
    anchor = next(a for a in plan.anchors if a.kind == "hard")
    # `observed` comes from listening to the rendered file, not from the recipe.
    assert anchor.observed == pytest.approx(1.6, abs=0.12)


def test_turning_phrase_timing_off_again_drops_its_derivative(tmp_path):
    job, seg = _job(tmp_path, 2.0, [(0.8, 0.3), (0.6, 0.0), (0.8, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    assert seg.audio.render(PHRASED) is not None
    phrase_timing.run(job, tmp_path / "work", options={"mode": "whole"})
    assert seg.audio.render(PHRASED) is None
    assert seg.audio_clip == Path(seg.audio.raw().path)


# -- drift and signal preservation ------------------------------------------

def test_no_error_accumulates_across_a_line_with_many_phrases(tmp_path):
    """Each piece lands where the recipe put it, including the last one.

    Concatenating four stretched pieces is exactly where a per-piece rounding
    error would compound into an audible drift by the end of the line, so the
    check is against the *measured* positions of every run in the output.
    """
    blocks = []
    for _ in range(4):
        blocks += [(0.45, 0.3), (0.35, 0.0)]
    job, seg = _job(tmp_path, 2.5, blocks[:-1])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    plan = seg.phrasing
    assert len(plan.phrases) == 4 and plan.state == "applied"
    rendered = Path(seg.audio.render(PHRASED).path)
    bounds = boundaries.inspect(rendered)
    runs, _lead, _tail, _gaps = phrases.runs_from(bounds, phrases.settings(_options()))
    assert len(runs) == 4
    placed = [p for p in plan.pieces if p["kind"] == "speech"]
    for piece, (start, _end) in zip(placed, runs, strict=True):
        # 40 ms covers the detector's own frame size and the handles; it does
        # not cover a drift that grows piece by piece.
        assert start == pytest.approx(piece["at"], abs=0.05)
    last = placed[-1]
    assert runs[-1][0] - last["at"] == pytest.approx(runs[0][0] - placed[0]["at"],
                                                     abs=0.03)


def test_the_speech_that_goes_in_comes_back_out(tmp_path):
    """Nothing is dropped and nothing is doubled, measured on the real output."""
    job, seg = _job(tmp_path, 1.4, [(0.6, 0.3), (0.5, 0.0), (0.6, 0.3)])
    phrase_timing.run(job, tmp_path / "work", options=_options(protect_pause=1.0))
    plan = seg.phrasing
    rendered = Path(seg.audio.render(PHRASED).path)
    bounds = boundaries.inspect(rendered)
    runs, _lead, _tail, _gaps = phrases.runs_from(bounds, phrases.settings(_options()))
    heard = sum(end - start for start, end in runs)
    # Speech in, divided by the compression actually applied, is speech out.
    assert heard == pytest.approx(plan.speech_in / plan.max_stretch, rel=0.1)
    assert len(runs) == 2          # two runs in, two runs out: none merged, none split
