"""D05 — source measurement, one level owner, bounds and fallback.

Numeric only. These fixtures are tones with known amplitudes, so they can
prove that a known relative level survives fitting and mixing arithmetic. They
cannot show that a quiet line is intelligible in a real scene; that is a
listening gate and is recorded separately.
"""

import math
import shutil
import struct
import wave

import pytest

from doblarr import levels
from doblarr.cues import FITTED, LEVELED, NORMALIZED, RAW, Artifact, Span
from doblarr.models import DubJob, Segment, Speaker

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RATE = 48000
# The relative-level tolerance the earlier proposal proposed. It holds for
# sufficiently long, isolated, unclamped fixtures; clamped and fallback cases
# are asserted separately because they are different outcomes, not misses.
TOLERANCE_DB = 2.0


def tone(path, amplitude, seconds=2.0, hz=220.0, silence=0.0):
    """A tone with an optional silent lead, so a floor exists to measure against."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(round(RATE * (silence + seconds))):
        position = index / RATE
        value = 0.0 if position < silence else amplitude * math.sin(
            2 * math.pi * hz * (position - silence))
        frames.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def concat(path, pieces):
    """One track built from (start, amplitude, seconds) pieces, with gaps of silence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = max(start + seconds for start, _a, seconds in pieces) + 0.5
    samples = [0.0] * round(RATE * total)
    for start, amplitude, seconds in pieces:
        offset = round(RATE * start)
        for index in range(round(RATE * seconds)):
            samples[offset + index] = amplitude * math.sin(2 * math.pi * 220 * index / RATE)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000)) for v in samples))
    return path


def measured_db(path):
    return levels.analyze(path)["speech_db"]


def scene(tmp_path, amplitudes):
    """A job whose source has one cue per amplitude, with a separated stem.

    `vocals` and `background` are distinct files, which is what tells the level
    owner that separation really ran — without it every measurement is honestly
    flagged as contaminated.
    """
    pieces, segments = [], []
    position = 1.0
    for index, amplitude in enumerate(amplitudes):
        pieces.append((position, amplitude, 1.5))
        seg = Segment(index, position, position + 1.5, f"line {index}",
                      text_translated=f"linea {index}")
        seg.cue_id = f"cue-{index}"
        seg.source.spans = [Span(position, position + 1.5, "source")]
        segments.append(seg)
        position += 2.5
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00")}
    job.segments = segments
    job.source_track = concat(tmp_path / "source.wav", pieces)
    job.source_audio = job.source_track
    job.vocals = concat(tmp_path / "vocals.wav", pieces)
    job.background = tone(tmp_path / "bed.wav", 0.01, 12.0)
    job.artifacts_dir = tmp_path / "work"
    return job


def fitted(job, amplitudes, tmp_path):
    """Give every cue a fitted derivative at a uniform generated level."""
    for seg, amplitude in zip(job.segments, amplitudes, strict=True):
        path = tone(tmp_path / "gen" / f"line{seg.index}.wav", amplitude, 1.4, silence=0.2)
        seg.audio.put_render(Artifact(role=FITTED, path=str(path),
                                      fingerprint=f"fit-{seg.index}", derived_from=RAW))
        seg.audio_clip = path


OPTIONS = {"mode": "follow_source", "measure_source": True, "strength": 1.0}


# -- measurement -----------------------------------------------------------

def test_known_relative_source_levels_are_measured_within_tolerance(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.075, 0.60])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert [s.measurement.state for s in job.segments] == ["measured"] * 6
    quiet, loud = job.segments[4].measurement, job.segments[5].measurement
    assert quiet.units == "dBFS-rms-speech" and quiet.method == levels.METHOD
    # 0.075 is 12 dB under 0.30; 0.60 is 6 dB over it.
    assert quiet.relative_db == pytest.approx(-12.0, abs=TOLERANCE_DB)
    assert loud.relative_db == pytest.approx(6.0, abs=TOLERANCE_DB)
    assert job.dialogue_baseline["scope"] in ("global", "speaker")


def test_a_too_short_source_interval_is_insufficient_not_quiet(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    short = job.segments[0]
    short.source.spans = [Span(1.0, 1.1, "source")]
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert short.measurement.state == "insufficient"
    assert short.measurement.relative_db is None
    assert not short.measurement.trusted


def test_an_overlapped_cue_is_never_trusted(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    other = job.segments[1]
    other.speaker = "SPEAKER_01"
    other.source.spans = [Span(1.2, 2.2, "source")]   # across cue 0
    job.speakers["SPEAKER_01"] = Speaker("SPEAKER_01")
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert job.segments[0].measurement.overlapped
    assert not job.segments[0].measurement.trusted
    assert "another speaker" in job.segments[0].measurement.exclusions[0]


def test_without_a_separated_stem_every_measurement_is_contaminated(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    job.vocals = job.source_audio          # separation did not run
    job.background = job.source_audio
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert all(s.measurement.contaminated for s in job.segments)
    assert all(not s.measurement.trusted for s in job.segments)
    assert job.segments[0].measurement.source == "source-stream"


def test_a_sparse_scene_falls_back_instead_of_inventing_a_baseline(tmp_path):
    job = scene(tmp_path, [0.3, 0.3])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert job.dialogue_baseline["scope"] == "fallback"
    assert "clean source measurements" in job.dialogue_baseline["reason"]
    assert all(s.measurement.relative_db is None for s in job.segments)


def test_missing_source_audio_is_reported_not_guessed(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    job.source_track = job.source_audio = job.vocals = job.background = None
    baseline = levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    assert baseline["scope"] == "missing"
    assert all(s.measurement.state == "missing" for s in job.segments)


# -- processing ------------------------------------------------------------

def test_follow_source_preserves_a_known_contrast_after_fitting(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.075, 0.60])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    # Every generated take is the same level: the contrast can only come from
    # the source measurement, not from the generator.
    fitted(job, [0.3] * 6, tmp_path)
    # Bounds wide enough for this contrast; the clamp is its own test.
    levels.process(job, {**OPTIONS, "max_cut_db": 16.0, "max_boost_db": 12.0})

    rendered = {s.index: measured_db(s.audio.render(LEVELED).path) for s in job.segments}
    ordinary = rendered[0]
    assert rendered[4] - ordinary == pytest.approx(-12.0, abs=TOLERANCE_DB)
    assert rendered[5] - ordinary == pytest.approx(6.0, abs=TOLERANCE_DB)
    assert job.segments[4].level.outcome == "applied"
    assert job.segments[4].level.mode == "follow_source"


def test_gain_is_bounded_and_a_clamp_is_reported_as_a_clamp(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.02])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {**OPTIONS, "max_cut_db": 4.0})

    quiet = job.segments[4].level
    assert quiet.outcome == "clamped"
    assert quiet.applied_db == pytest.approx(-4.0)
    assert quiet.requested_db < -4.0
    assert quiet.clamped_db < 0     # exactly what the bound took away


def test_untrusted_source_evidence_falls_back_visibly(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    job.vocals = job.source_audio          # no separation: nothing is trusted
    job.background = job.source_audio
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, OPTIONS)

    assert all(s.level.outcome == "fallback" for s in job.segments)
    assert all(s.level.applied_db == 0 for s in job.segments)
    # It still reaches the baseline target: falling back means "no performance
    # gain", not "no level processing".
    assert all(s.audio.render(LEVELED) is not None for s in job.segments)
    assert measured_db(job.segments[0].audio.render(LEVELED).path) == pytest.approx(
        -20.0, abs=TOLERANCE_DB)


def test_a_manual_gain_wins_over_the_automatic_one(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.075])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {**OPTIONS, "gains": {"cue-4": -3.0}})

    decision = job.segments[4].level
    assert decision.manual and decision.applied_db == pytest.approx(-3.0)
    assert "manual per-line gain" in decision.reason
    rendered = measured_db(job.segments[4].audio.render(LEVELED).path)
    assert rendered - measured_db(job.segments[0].audio.render(LEVELED).path) == (
        pytest.approx(-3.0, abs=TOLERANCE_DB))


def test_peak_protection_holds_the_baseline_back_not_the_contrast(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.9])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    # A target the ordinary lines reach comfortably, so only the line carrying
    # a large performance boost runs into the ceiling.
    levels.process(job, {**OPTIONS, "target_db": -8.0, "peak_ceiling": 0.5,
                         "max_boost_db": 12.0})

    loud = job.segments[4].level
    assert loud.peak_limited and loud.peak <= 0.5
    assert loud.applied_db > 0       # the performance gain survived the ceiling
    assert "peak ceiling" in loud.reason
    peak = levels.analyze(job.segments[4].audio.render(LEVELED).path)["peak"]
    assert peak <= 0.55
    # The contrast with an ordinary line is still there, just lower overall.
    ordinary = measured_db(job.segments[0].audio.render(LEVELED).path)
    assert measured_db(job.segments[4].audio.render(LEVELED).path) > ordinary


def test_legacy_mode_leaves_loudness_to_the_pre_fit_pass(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {"mode": "legacy"})
    assert all(s.audio.render(LEVELED) is None for s in job.segments)
    assert all(s.level.outcome == "bypassed" for s in job.segments)
    assert not levels.owns_processing({"mode": "legacy"})
    assert levels.owns_processing({"mode": "follow_source"})


def test_turning_the_feature_off_drops_its_previous_derivative(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, OPTIONS)
    assert job.segments[0].audio.render(LEVELED) is not None

    levels.process(job, {"mode": "off"})
    seg = job.segments[0]
    assert seg.audio.render(LEVELED) is None
    assert str(seg.audio_clip) == seg.audio.render(FITTED).path


def test_rerunning_does_not_accumulate_gain(tmp_path):
    job = scene(tmp_path, [0.30, 0.30, 0.30, 0.30, 0.075])
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, OPTIONS)
    once = measured_db(job.segments[4].audio.render(LEVELED).path)
    levels.process(job, OPTIONS)
    twice = measured_db(job.segments[4].audio.render(LEVELED).path)
    # The second pass reads the fitted input again, not its own output.
    assert twice == pytest.approx(once, abs=0.05)
    assert job.segments[4].audio.render(LEVELED).derived_from == FITTED


def test_the_level_owner_reads_past_a_legacy_normalized_artifact(tmp_path):
    """A run that switches to the new owner must not stack on the old pass."""
    job = scene(tmp_path, [0.3] * 5)
    levels.measure_sources(job, OPTIONS, work_dir=tmp_path / "work")
    fitted(job, [0.3] * 5, tmp_path)
    seg = job.segments[0]
    # A leftover pre-fit artifact from a legacy run. `fitted` is downstream of
    # it, so the level owner must read the fitted file.
    seg.audio.renders.insert(0, Artifact(role=NORMALIZED, path=str(seg.audio_clip),
                                         fingerprint="old", derived_from=RAW))
    levels.process(job, OPTIONS)
    assert seg.audio.render(LEVELED).derived_from == FITTED


# -- the mix -------------------------------------------------------------

def mixed(tmp_path, job, dialogue, bed):
    """A mixed track and a bed to measure a line's real margin against."""
    pieces = [(seg.start, dialogue, seg.end - seg.start) for seg in job.segments]
    job.dubbed_track = concat(tmp_path / "mixed.wav", pieces)
    job.background = tone(tmp_path / "bedonly.wav", bed, 20.0)
    return job


def test_a_quiet_line_masked_by_the_bed_is_reported_not_raised(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {"mode": "consistent"})
    # Dialogue barely above a loud bed: mathematically levelled, practically
    # buried.
    mixed(tmp_path, job, dialogue=0.25, bed=0.22)
    flagged = levels.check_mix(job, {"mode": "consistent"}, work_dir=tmp_path / "work")

    assert flagged == len(job.segments)
    finding = next(f for f in job.segments[0].findings
                   if f.code == "quiet_line_masked")
    assert finding.kind == "performance" and finding.severity == "warning"
    assert finding.evidence["margin_db"] < levels.MIN_MARGIN_DB
    assert "mix decision" in finding.evidence["note"]
    # Reported only: the rendered audio is untouched.
    assert all(s.level.outcome == "applied" for s in job.segments)


def test_a_line_that_sits_clear_of_the_bed_raises_nothing(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {"mode": "consistent"})
    mixed(tmp_path, job, dialogue=0.4, bed=0.02)
    assert levels.check_mix(job, {"mode": "consistent"}, work_dir=tmp_path / "work") == 0
    assert not any(f.code == "quiet_line_masked" and f.disposition == "open"
                   for s in job.segments for f in s.findings)


def test_without_a_separated_bed_the_margin_is_not_guessed(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {"mode": "consistent"})
    job.dubbed_track = concat(tmp_path / "mixed.wav",
                              [(s.start, 0.25, s.end - s.start) for s in job.segments])
    job.background = job.source_audio      # separation did not run
    assert levels.check_mix(job, {"mode": "consistent"}, work_dir=tmp_path / "work") == 0
    assert not any(f.code == "quiet_line_masked" for s in job.segments for f in s.findings)


def test_the_legacy_path_is_not_second_guessed_by_the_mix_check(tmp_path):
    job = scene(tmp_path, [0.3] * 5)
    fitted(job, [0.3] * 5, tmp_path)
    levels.process(job, {"mode": "legacy"})
    mixed(tmp_path, job, dialogue=0.25, bed=0.22)
    assert levels.check_mix(job, {"mode": "legacy"}, work_dir=tmp_path / "work") == 0
