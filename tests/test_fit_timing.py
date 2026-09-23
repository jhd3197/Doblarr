"""Fit-timing stage — measure clips vs slots, atempo-stretch the overflows."""

import logging
import os
from pathlib import Path

from doblarr.models import DubJob, Segment
from doblarr.stages import fit_timing
from doblarr.stages.common import DryRunPlan
from doblarr.stages.fit_timing import _atempo_chain


def _job(tmp_path: Path, slots: list[tuple[float, float]]) -> DubJob:
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (1000, 1000))
    job = DubJob(input_file=src, source_lang="ko", target_lang="es")
    clips = tmp_path / "work" / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    for i, (start, end) in enumerate(slots):
        clip = clips / f"line_{i:04d}.wav"
        clip.write_text("audio", encoding="utf-8")
        os.utime(clip, (2000, 2000))
        job.segments.append(Segment(i, start, end, f"line {i}", audio_clip=clip))
    return job


def _spy(monkeypatch, lengths: dict[str, float]):
    """Fake ffprobe durations by clip stem; record ffmpeg calls."""
    calls = []
    monkeypatch.setattr(fit_timing, "run_ffprobe",
                        lambda args, **k: str(lengths[Path(args[-1]).stem]))
    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"audio")
    monkeypatch.setattr(fit_timing, "run_ffmpeg", render)
    return calls


def test_atempo_chain_splits_out_of_range_factors():
    assert _atempo_chain(1.25) == "atempo=1.2500"
    assert _atempo_chain(5.0) == "atempo=2.0,atempo=2.0,atempo=1.2500"
    assert _atempo_chain(0.3) == "atempo=0.5,atempo=0.6000"


def test_dry_run_and_disabled_short_circuit(tmp_path):
    job = _job(tmp_path, [(0.0, 1.0)])
    assert isinstance(fit_timing.run.__wrapped__(job, tmp_path / "work", dry_run=True),
                      DryRunPlan)
    assert fit_timing.run(job, tmp_path / "work", dry_run=True) is None
    assert fit_timing.run(job, tmp_path / "work", enabled=False) is None


def test_fitting_clips_left_alone(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 2.0), (3.0, 4.0)])
    calls = _spy(monkeypatch, {"line_0000": 1.5, "line_0001": 0.9})
    fit_timing.run(job, tmp_path / "work")
    assert calls == []  # short/exact clips are never resampled
    assert job.segments[0].audio_clip.parent.name == "clips"


def test_long_clip_stretched_and_repointed(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 2.0)])
    calls = _spy(monkeypatch, {"line_0000": 2.4})  # 1.2x over the 2s slot
    fit_timing.run(job, tmp_path / "work")
    assert len(calls) == 1
    assert "atempo=1.2000" in calls[0]
    assert Path(calls[0][-1]).parent.name == "fit"
    assert job.segments[0].audio_clip.parent.name == "fit"


def test_overlong_clip_clamped_and_warned(tmp_path, monkeypatch, caplog):
    job = _job(tmp_path, [(0.0, 1.0), (2.0, 3.0)])
    calls = _spy(monkeypatch, {"line_0000": 5.0, "line_0001": 0.5})
    with caplog.at_level(logging.WARNING, logger="doblarr.fit_timing"):
        fit_timing.run(job, tmp_path / "work")
    assert "atempo=1.3000" in calls[0]  # clamped to MAX_STRETCH, not 5.0x
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("line 0" in m and "over even at max" in m and "line 1" in m
               for m in warns)


def test_fresh_fitted_clips_skip_but_repoint(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 2.0)])
    monkeypatch.setattr(fit_timing, "run_ffprobe",
                        lambda args, **k: "2.4")
    calls = []

    def fake_ffmpeg(args, **k):
        calls.append(args)
        dest = Path(args[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("fit", encoding="utf-8")
        os.utime(dest, (3000, 3000))

    monkeypatch.setattr(fit_timing, "run_ffmpeg", fake_ffmpeg)
    fit_timing.run(job, tmp_path / "work")
    assert len(calls) == 1

    job2 = _job(tmp_path, [(0.0, 2.0)])
    fit_timing.run(job2, tmp_path / "work")
    assert len(calls) == 1  # cached: no second stretch
    assert job2.segments[0].audio_clip.parent.name == "fit"


# -- steady pacing (timing.pacing) --------------------------------------------

def _old_factor(actual, slot):
    """The whole-clip rule before pacing existed, written out independently."""
    if actual <= slot * 1.02:
        return 1.0
    return min(actual / slot, 1.3)


def _factors(job):
    return [round(float(Path(s.audio_clip).stem.split(".", 1)[1]), 4)
            if Path(s.audio_clip).parent.name == "fit" else 1.0
            for s in job.segments]


def test_pacing_off_reproduces_the_old_factors_exactly(tmp_path, monkeypatch):
    slots = [(0.0, 2.0), (2.5, 4.0), (5.0, 6.0), (7.0, 9.5), (10.0, 11.0), (12.0, 13.0)]
    lengths = {"line_0000": 2.0, "line_0001": 1.9, "line_0002": 1.25,
               "line_0003": 2.5, "line_0004": 3.0, "line_0005": 1.019}
    job = _job(tmp_path, slots)
    calls = _spy(monkeypatch, lengths)
    fit_timing.run(job, tmp_path / "work", options={"pacing": "off", "max_stretch": 1.1})
    expected = [_old_factor(lengths[f"line_{i:04d}"], end - start)
                for i, (start, end) in enumerate(slots)]
    assert _factors(job) == [round(f, 4) for f in expected]
    assert len(calls) == sum(1 for f in expected if f != 1.0)
    # the old fingerprint: no pacing policy in it
    fitted = job.segments[2].audio.render(fit_timing.FITTED)
    assert fitted is not None


def test_whole_mode_honours_timing_max_stretch(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 1.0)])
    calls = _spy(monkeypatch, {"line_0000": 2.0})
    fit_timing.run(job, tmp_path / "work", options={"pacing": "speaker", "max_stretch": 1.15})
    assert "atempo=1.1500" in calls[0]
    assert "timing_overflow" in job.segments[0].issues


class _Shortener:
    """A translator that halves a line; counts its requests."""

    def __init__(self):
        self.asked = []

    def shorten(self, text, lang, budget):
        self.asked.append(text)
        return text[: max(1, len(text) // 2)]


def test_neighbouring_lines_keep_one_pace_and_the_long_one_is_repaired(tmp_path, monkeypatch):
    # One speaker, three 2 s slots: one take needs 1.28x, two fit with room.
    job = _job(tmp_path, [(0.0, 2.0), (2.5, 4.5), (5.0, 7.0)])
    lengths = {"line_0000": 1.8, "line_0001": 2.56, "line_0002": 1.8, "line_0001r": 2.2}
    calls = _spy(monkeypatch, lengths)
    translator = _Shortener()
    regenerated = []

    def regenerate(seg):
        clip = Path(seg.audio_clip).with_name(f"line_{seg.index:04d}r.wav")
        clip.write_text("audio", encoding="utf-8")
        os.utime(clip, (2000, 2000))
        seg.audio_clip = clip
        regenerated.append(seg.index)

    before = {s.index for s in job.segments if lengths[f"line_{s.index:04d}"] > s.duration * 1.3}
    fit_timing.run(job, tmp_path / "work", translator=translator, regenerate=regenerate,
                   options={"pacing": "speaker"})
    assert regenerated == [1]                     # repaired before accepting 1.28x
    factors = _factors(job)
    assert max(factors) - min(factors) <= 0.10 + 1e-9
    assert factors[1] == 1.1                      # 2.2 s in 2 s
    after = {s.index for s in job.segments if "timing_overflow" in s.issues}
    assert after <= before
    assert calls  # the long line is still compressed, just not alone


def test_a_line_that_fits_takes_the_group_pace_instead_of_snapping_to_one(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 2.0), (2.5, 4.5), (5.0, 7.0)])
    _spy(monkeypatch, {"line_0000": 2.4, "line_0001": 2.4, "line_0002": 1.9})
    fit_timing.run(job, tmp_path / "work", options={"pacing": "speaker"})
    base = (2.4 + 2.4 + 1.9) / 6.0
    factors = _factors(job)
    assert factors[:2] == [1.2, 1.2]
    assert factors[2] == round(base - 0.10, 4)    # sped to the group, not left at 1.0
    off = _job(tmp_path / "off", [(0.0, 2.0), (2.5, 4.5), (5.0, 7.0)])
    fit_timing.run(off, tmp_path / "off" / "work", options={"pacing": "off"})
    assert _factors(off)[2] == 1.0


def test_other_speakers_and_other_scenes_are_paced_separately(tmp_path, monkeypatch):
    job = _job(tmp_path, [(0.0, 2.0), (2.5, 4.5), (30.0, 32.0)])
    job.segments[1].speaker = "B"
    _spy(monkeypatch, {"line_0000": 2.4, "line_0001": 1.9, "line_0002": 1.9})
    fit_timing.run(job, tmp_path / "work", options={"pacing": "speaker"})
    assert _factors(job) == [1.2, 1.0, 1.0]
    groups = job.metrics["pacing"]["groups"]
    assert set(groups) == {"SPEAKER_00#0", "SPEAKER_00#1", "B#0"}


def test_fit_fingerprint_follows_the_pacing_settings(tmp_path, monkeypatch):
    def fingerprint(options):
        job = _job(tmp_path / str(len(seen)), [(0.0, 2.0)])
        _spy(monkeypatch, {"line_0000": 2.4})
        fit_timing.run(job, tmp_path / str(len(seen)) / "work", options=options)
        seen.append(job.segments[0].audio.render(fit_timing.FITTED).fingerprint)
        return seen[-1]

    seen: list[str] = []
    first = fingerprint({"pacing": "speaker"})
    assert fingerprint({"pacing": "speaker"}) == first          # unchanged run: same
    assert fingerprint({"pacing": "speaker", "pace_local_range": 0.05}) != first
    assert fingerprint({"pacing": "off"}) != first
