"""Speech-boundary detection, reversible trimming and protected clip edges.

Fixtures are generated tone, so they pin durations, offsets, retained content
and cache behavior. They say nothing about whether a real voice sounds right at
its boundaries; that is the listening gate recorded in the plan.
"""

import math
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from doblarr.cues import CLIP, EDGED, FITTED, NORMALIZED, RAW, TRIMMED, Artifact, Take
from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import boundaries, fit_timing, quality

ffmpeg_only = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
RATE = 48000


def tone(path, parts, rate=RATE, channels=1):
    """Write a clip from (seconds, amplitude) parts. Amplitude 0 is silence.

    `channels > 1` writes the signal into the LAST channel only, so a detector
    that averages channels would read it as quieter than it is.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    position = 0
    for seconds, amplitude in parts:
        for _ in range(round(rate * seconds)):
            raw = amplitude * math.sin(2 * math.pi * 220 * position / rate) * 32000
            value = max(-32768, min(32767, int(raw)))  # amplitude > 1 clips, as intended
            frames.append(struct.pack("<h", 0) * (channels - 1) + struct.pack("<h", value))
            position += 1
    with wave.open(str(path), "wb") as out:
        out.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def duration(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _job(tmp_path, slot=(0.0, 2.0)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (1000.0, 1000.0))
    job = DubJob(input_file=src, source_lang="ja", target_lang="es")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", voicebox_profile_id="voice")}
    job.segments = [Segment(0, slot[0], slot[1], "uno", text_translated="uno")]
    return job


def _with_raw(job, path, fingerprint="gen-1"):
    seg = job.segments[0]
    seg.cue_id = seg.cue_id or "cue-1"
    seg.audio.takes.append(Take(take_id="t1", fingerprint=fingerprint,
                                raw=Artifact(role=RAW, path=str(path),
                                             fingerprint=fingerprint)))
    seg.audio.selection = None
    seg.audio_clip = path
    return seg


ON = {"trim": True}


# --------------------------------------------------------------------------
# Phase A — detection
# --------------------------------------------------------------------------

def test_padding_is_located_and_internal_pauses_are_reported_not_removed(tmp_path):
    clip = tone(tmp_path / "a.wav",
                [(0.5, 0.0), (0.4, 0.4), (0.3, 0.0), (0.4, 0.4), (0.6, 0.0)])
    bounds = boundaries.measure(clip)
    assert bounds.first == pytest.approx(0.5, abs=0.03)
    assert bounds.last == pytest.approx(1.6, abs=0.03)
    # The 0.3 s gap between the two phrases is evidence, never a trim target.
    assert len(bounds.silences) == 1
    gap_start, gap_end = bounds.silences[0]
    assert gap_end - gap_start == pytest.approx(0.3, abs=0.05)
    assert bounds.separation > 20


def test_silent_take_has_no_active_region(tmp_path):
    bounds = boundaries.measure(tone(tmp_path / "s.wav", [(1.0, 0.0)]))
    assert bounds.first is None and bounds.last is None
    prepared = boundaries.decide(bounds, 1.0, boundaries._settings(ON))
    assert prepared.decision == "empty"
    assert prepared.lead == 0 and prepared.tail == 0


def test_speech_in_one_channel_is_not_lost(tmp_path):
    """Channel policy: combine by maximum, so a one-sided take still reads."""
    mono = boundaries.measure(tone(tmp_path / "m.wav", [(0.4, 0.0), (0.6, 0.4)]))
    stereo = boundaries.measure(
        tone(tmp_path / "st.wav", [(0.4, 0.0), (0.6, 0.4)], channels=2))
    assert stereo.first == pytest.approx(mono.first, abs=0.02)
    assert stereo.last == pytest.approx(mono.last, abs=0.02)
    assert stereo.speech_db == pytest.approx(mono.speech_db, abs=0.5)


def test_low_dynamic_range_take_is_uncertain_not_trimmed(tmp_path):
    """A quiet, even take: energy alone cannot say where the speech begins."""
    bounds = boundaries.measure(tone(tmp_path / "w.wav", [(1.0, 0.02)]))
    prepared = boundaries.decide(bounds, 1.0, boundaries._settings(ON))
    assert prepared.decision == "uncertain"
    assert "between speech and noise" in prepared.reason
    assert prepared.lead == 0 and prepared.tail == 0
    # Audible but unknowable is not the same as empty: the take is kept whole.
    assert prepared.decision != "empty"


def test_a_quiet_region_is_not_unwanted_silence_by_definition(tmp_path):
    """A whisper after a pause is speech; only the padding around it may go."""
    clip = tone(tmp_path / "q.wav", [(0.5, 0.0), (0.5, 0.5), (0.4, 0.05), (0.5, 0.0)])
    bounds = boundaries.measure(clip)
    assert bounds.last == pytest.approx(1.4, abs=0.06)  # the quiet tail is kept


def test_clipped_input_is_flagged_not_refused(tmp_path):
    bounds = boundaries.measure(tone(tmp_path / "c.wav", [(0.3, 0.0), (0.6, 1.2)]))
    assert bounds.clipped
    prepared = boundaries.decide(bounds, 1.0, boundaries._settings(ON))
    assert prepared.clipped and prepared.decision == "trimmed"


def test_trim_is_bounded_and_records_the_clamp(tmp_path):
    clip = tone(tmp_path / "long.wav", [(3.0, 0.0), (0.5, 0.4), (3.0, 0.0)])
    bounds = boundaries.measure(clip)
    prepared = boundaries.decide(bounds, 1.0, boundaries._settings(
        {"trim": True, "max_trim_seconds": 1.0}))
    assert prepared.lead == 1.0 and prepared.tail == 1.0
    assert "clamped" in prepared.reason


def test_nothing_to_trim_is_kept(tmp_path):
    # Speech starts and ends inside the protective handle: nothing to remove.
    bounds = boundaries.measure(tone(tmp_path / "t.wav", [(0.04, 0.0), (1.0, 0.4), (0.04, 0.0)]))
    prepared = boundaries.decide(bounds, 1.0, boundaries._settings(ON))
    assert prepared.decision == "kept" and prepared.reason == "no removable boundary padding"
    assert prepared.active_duration == pytest.approx(1.0, abs=0.06)


# --------------------------------------------------------------------------
# Phase B — reversible preparation
# --------------------------------------------------------------------------

@ffmpeg_only
def test_trim_keeps_the_raw_take_and_is_described_by_its_offsets(tmp_path):
    raw = tone(tmp_path / "clips" / "line_0000.wav",
               [(0.6, 0.0), (0.8, 0.4), (0.7, 0.0)])
    original = raw.read_bytes()
    job = _job(tmp_path)
    seg = _with_raw(job, raw)

    prepared = boundaries.prepare(seg, ON)
    assert prepared.decision == "trimmed"
    assert raw.read_bytes() == original  # the raw generation is never modified
    trimmed = seg.audio.render(TRIMMED)
    assert trimmed is not None and trimmed.derived_from == RAW
    # Speech plus one protective handle on each side survives.
    assert duration(Path(trimmed.path)) == pytest.approx(0.8 + 2 * 0.06, abs=0.05)
    assert prepared.lead == pytest.approx(0.6 - 0.06, abs=0.03)
    assert prepared.active.domain == CLIP
    assert seg.audio_clip == Path(trimmed.path)

    # Rerunning reuses the derivative instead of trimming a trimmed file.
    stamp = Path(trimmed.path).stat().st_mtime_ns
    boundaries.prepare(seg, ON)
    assert Path(seg.audio.render(TRIMMED).path).stat().st_mtime_ns == stamp


@ffmpeg_only
def test_internal_pause_survives_trimming(tmp_path):
    raw = tone(tmp_path / "clips" / "line_0000.wav",
               [(0.5, 0.0), (0.4, 0.4), (0.3, 0.0), (0.4, 0.4), (0.5, 0.0)])
    job = _job(tmp_path)
    seg = _with_raw(job, raw)
    prepared = boundaries.prepare(seg, ON)
    trimmed = boundaries.measure(Path(seg.audio.render(TRIMMED).path))
    assert len(trimmed.silences) == 1  # the pause is still there
    assert len(prepared.silences) == 1
    assert duration(Path(seg.audio.render(TRIMMED).path)) == pytest.approx(1.22, abs=0.06)


@ffmpeg_only
def test_intended_onset_is_re_inserted_after_trimming(tmp_path):
    """Removing generator padding must not make the line speak early."""
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.9, 0.0), (0.6, 0.4)])
    job = _job(tmp_path)
    seg = _with_raw(job, raw)
    seg.placement.onset = 0.25  # the reviewer wants a quarter-second wait

    prepared = boundaries.prepare(seg, ON)
    assert prepared.decision == "trimmed" and prepared.onset == 0.25
    trimmed = boundaries.measure(Path(seg.audio.render(TRIMMED).path))
    assert trimmed.first == pytest.approx(0.25 + 0.06, abs=0.04)
    # The cue itself did not move; only the take's dead air went.
    assert (seg.start, seg.end) == (0.0, 2.0)
    assert seg.source.spans == []


@ffmpeg_only
def test_empty_generation_is_never_trimmed_into_a_plausible_line(tmp_path):
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(2.0, 0.0)])
    job = _job(tmp_path)
    seg = _with_raw(job, raw)
    seg.issues = ["silence"]
    prepared = boundaries.prepare(seg, ON)
    assert prepared.decision == "empty"
    assert seg.audio.render(TRIMMED) is None
    assert seg.audio_clip == raw


def test_bypassed_when_off_and_reason_is_recorded(tmp_path):
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.5, 0.0), (0.5, 0.4)])
    job = _job(tmp_path)
    seg = _with_raw(job, raw)
    prepared = boundaries.prepare(seg, {"trim": False})
    assert prepared.decision == "bypassed" and prepared.reason == "boundaries.trim is off"
    assert seg.audio.render(TRIMMED) is None


@ffmpeg_only
def test_turning_trimming_off_again_invalidates_what_came_from_it(tmp_path):
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.6, 0.0), (0.8, 0.4)])
    job = _job(tmp_path)
    seg = _with_raw(job, raw)
    boundaries.prepare(seg, ON)
    seg.audio.put_render(Artifact(role=NORMALIZED, path=str(tmp_path / "n.wav"),
                                  fingerprint="n1", derived_from=TRIMMED))
    assert seg.audio.current().role == NORMALIZED

    boundaries.prepare(seg, {"trim": False})
    assert seg.audio.render(TRIMMED) is None
    assert seg.audio.render(NORMALIZED) is None  # it was derived from the trim
    assert seg.audio_clip == raw


@ffmpeg_only
def test_trimming_removes_an_unnecessary_timing_repair(tmp_path):
    """The whole point: padding should not be mistaken for an overlong line."""
    padded = [(0.9, 0.0), (1.4, 0.4), (0.9, 0.0)]  # 3.2s of file, 1.4s of speech
    results = {}
    for name, options in (("off", {"trim": False}), ("on", ON)):
        job = _job(tmp_path / name, slot=(0.0, 1.8))
        raw = tone(tmp_path / name / "clips" / "line_0000.wav", padded)
        _with_raw(job, raw)
        quality.run(job, normalize=False, boundary_options=options)
        fit_timing.run(job, tmp_path / name / "work")
        results[name] = (job.segments[0].audio.render(FITTED), job.metrics)

    # Untrimmed, the 3.2 s file overruns its 1.8 s slot and gets stretched.
    assert results["off"][0] is not None
    # Trimmed, the 1.52 s of speech fits and nothing is resampled at all.
    assert results["on"][0] is None
    assert results["on"][1]["trim_trimmed"] == 1


@ffmpeg_only
def test_a_short_utterance_is_never_stretched_to_fill_its_slot(tmp_path):
    job = _job(tmp_path, slot=(0.0, 5.0))
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.3, 0.0), (0.6, 0.4)])
    seg = _with_raw(job, raw)
    quality.run(job, normalize=False, boundary_options=ON)
    fit_timing.run(job, tmp_path / "work")
    assert seg.audio.render(FITTED) is None
    assert duration(Path(seg.audio_clip)) < 1.0


# --------------------------------------------------------------------------
# Phase C — protected edges
# --------------------------------------------------------------------------

@ffmpeg_only
def test_a_hard_cut_edge_is_faded_and_a_smooth_one_is_left_alone(tmp_path):
    job = _job(tmp_path)
    job.segments = [
        Segment(0, 0, 2, "cut", text_translated="cut"),
        Segment(1, 3, 5, "smooth", text_translated="smooth"),
    ]
    # A clip that starts and ends at full amplitude clicks at both boundaries.
    hard = tone(tmp_path / "clips" / "hard.wav", [(0.8, 0.5)])
    # A clip that starts and ends in silence is already smooth.
    soft = tone(tmp_path / "clips" / "soft.wav", [(0.1, 0.0), (0.6, 0.5), (0.1, 0.0)])
    for seg, path in zip(job.segments, (hard, soft), strict=True):
        seg.cue_id = f"cue-{seg.index}"
        seg.audio.put_render(Artifact(role=FITTED, path=str(path),
                                      fingerprint=f"f-{seg.index}"))
        seg.audio_clip = path

    boundaries.finish_edges(job, {"edge_fade_ms": 8})
    assert job.metrics["edge_fades"] == 1
    edged = job.segments[0].audio.render(EDGED)
    assert edged is not None and edged.derived_from == FITTED
    assert job.segments[1].audio.render(EDGED) is None
    assert job.segments[1].audio_clip == soft

    # The fade removes the discontinuity without shortening the clip.
    assert duration(Path(edged.path)) == pytest.approx(duration(hard), abs=0.01)
    head, tail = boundaries.edge_peaks(Path(edged.path))
    before_head, before_tail = boundaries.edge_peaks(hard)
    assert before_head > 0.4 and before_tail > 0.4
    assert head < before_head / 5 and tail < before_tail / 5


@ffmpeg_only
def test_edge_fade_never_eats_a_very_short_clip(tmp_path):
    job = _job(tmp_path)
    tiny = tone(tmp_path / "clips" / "tiny.wav", [(0.02, 0.5)])
    seg = job.segments[0]
    seg.cue_id = "cue-1"
    seg.audio.put_render(Artifact(role=FITTED, path=str(tiny), fingerprint="f"))
    seg.audio_clip = tiny
    boundaries.finish_edges(job, {"edge_fade_ms": 50})
    edged = seg.audio.render(EDGED)
    assert duration(Path(edged.path)) == pytest.approx(0.02, abs=0.005)


@ffmpeg_only
def test_changing_fade_settings_reuses_speech_and_makes_a_new_render(tmp_path):
    job = _job(tmp_path)
    clip = tone(tmp_path / "clips" / "hard.wav", [(0.8, 0.5)])
    seg = job.segments[0]
    seg.cue_id = "cue-1"
    seg.audio.put_render(Artifact(role=FITTED, path=str(clip), fingerprint="f"))
    seg.audio_clip = clip

    boundaries.finish_edges(job, {"edge_fade_ms": 8})
    first = seg.audio.render(EDGED)
    boundaries.finish_edges(job, {"edge_fade_ms": 20})
    second = seg.audio.render(EDGED)
    assert second.fingerprint != first.fingerprint
    assert second.path != first.path
    assert Path(first.path).is_file()  # the earlier render is still there
    assert clip.is_file() and seg.audio.render(FITTED).path == str(clip)


def test_edges_are_skipped_when_disabled_or_dry_run(tmp_path):
    job = _job(tmp_path)
    seg = job.segments[0]
    seg.audio.put_render(Artifact(role=FITTED, path=str(tmp_path / "x.wav"),
                                  fingerprint="f"))
    # Off is both eases at 0 (and the older single fade left at 0).
    boundaries.finish_edges(job, {"edge_fade_ms": 0, "edge_fade_in_ms": 0,
                                  "edge_fade_out_ms": 0})
    boundaries.finish_edges(job, {"edge_fade_ms": 8}, dry_run=True)
    assert seg.audio.render(EDGED) is None
    assert "edge_fades" not in job.metrics


def test_voices_ease_in_short_and_out_longer_by_default():
    fade_in, fade_out, curve = boundaries.edge_fades(boundaries._settings(None))
    assert (fade_in, fade_out, curve) == (0.012, 0.040, "hsin")
    assert fade_in < fade_out  # a consonant keeps its attack; a vowel dies away


def test_the_older_single_fade_still_means_what_it_did():
    assert boundaries.edge_fades(boundaries._settings({"edge_fade_ms": 8})) == \
        (0.008, 0.008, "tri")
    assert boundaries.edge_fades(boundaries._settings({"edge_fade_ms": 500}))[0] == 0.050
    # the exit may be longer than the entrance, but it is bounded too
    assert boundaries.edge_fades(boundaries._settings({"edge_fade_out_ms": 900}))[1] == 0.150
    with pytest.raises(ValueError, match="edge_fade_curve"):
        boundaries.edge_fades(boundaries._settings({"edge_fade_curve": "bounce"}))


@ffmpeg_only
def test_a_line_eases_in_and_out_by_default_and_a_silent_edge_is_untouched(
        tmp_path, monkeypatch):
    job = _job(tmp_path)
    job.segments = [Segment(0, 0, 2, "cut", text_translated="cut"),
                    Segment(1, 3, 5, "smooth", text_translated="smooth")]
    hard = tone(tmp_path / "clips" / "hard.wav", [(0.8, 0.5)])
    # silent at the start, cut off at the end
    tail = tone(tmp_path / "clips" / "tail.wav", [(0.1, 0.0), (0.7, 0.5)])
    for seg, path in zip(job.segments, (hard, tail), strict=True):
        seg.cue_id = f"cue-{seg.index}"
        seg.audio.put_render(Artifact(role=FITTED, path=str(path),
                                      fingerprint=f"f-{seg.index}"))
        seg.audio_clip = path
    chains = []
    real = boundaries.run_ffmpeg

    def spy(args, **kwargs):
        chains.append(args[args.index("-af") + 1])
        real(args, **kwargs)

    monkeypatch.setattr(boundaries, "run_ffmpeg", spy)
    boundaries.finish_edges(job, {})
    assert job.metrics["edge_fades"] == 2
    assert chains[0] == ("afade=t=in:st=0:d=0.012:curve=hsin,"
                         "afade=t=out:st=0.76:d=0.04:curve=hsin")
    assert chains[1] == "afade=t=out:st=0.76:d=0.04:curve=hsin"  # no fade-in on silence
    edged = Path(job.segments[0].audio.render(EDGED).path)
    assert duration(edged) == pytest.approx(duration(hard), abs=0.01)
    head, end = boundaries.edge_peaks(edged)
    assert head < 0.1 and end < 0.1


@ffmpeg_only
def test_preparation_survives_the_script_cache(tmp_path):
    from doblarr.stages.common import load_script, save_script

    job = _job(tmp_path)
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.6, 0.0), (0.8, 0.4)])
    seg = _with_raw(job, raw)
    boundaries.prepare(seg, ON)
    before = seg.preparation.as_dict()
    save_script(job, tmp_path / "work")

    resumed = _job(tmp_path)
    assert load_script(resumed, tmp_path / "work")
    assert resumed.segments[0].preparation.as_dict() == before
    assert resumed.segments[0].audio.render(TRIMMED).path == seg.audio.render(TRIMMED).path


@ffmpeg_only
def test_normalization_reads_the_trim_and_never_its_own_output(tmp_path):
    """Gain must not stack: two passes normalize the same upstream artifact."""
    job = _job(tmp_path)
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.5, 0.0), (1.0, 0.4), (0.5, 0.0)])
    seg = _with_raw(job, raw)

    quality.run(job, boundary_options=ON)
    first = seg.audio.render(NORMALIZED)
    assert first.derived_from == TRIMMED
    level = quality.inspect_pcm(Path(first.path))["rms_db"]

    quality.run(job, boundary_options=ON)
    second = seg.audio.render(NORMALIZED)
    assert second.fingerprint == first.fingerprint and second.path == first.path
    assert quality.inspect_pcm(Path(second.path))["rms_db"] == pytest.approx(level, abs=0.01)
    assert seg.audio.render(TRIMMED).derived_from == RAW


@ffmpeg_only
def test_preparation_runs_after_the_raw_checks_see_the_untrimmed_take(tmp_path):
    """Content verification gets the raw take; the trim is available too."""
    job = _job(tmp_path)
    raw = tone(tmp_path / "clips" / "line_0000.wav", [(0.5, 0.0), (1.0, 0.4), (0.5, 0.0)])
    seg = _with_raw(job, raw)
    inspected = []

    original = quality.check_clip

    def spy(segment, *args, **kwargs):
        inspected.append(Path(segment.audio_clip))
        return original(segment, *args, **kwargs)

    quality.check_clip = spy
    try:
        quality.run(job, normalize=False, boundary_options=ON)
    finally:
        quality.check_clip = original
    assert inspected == [raw]  # checked before anything was removed
    assert seg.audio.raw().path == str(raw)
    assert seg.audio.render(TRIMMED) is not None  # both are available, no new TTS
