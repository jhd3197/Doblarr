"""Fit-timing stage — measure clips vs slots, atempo-stretch the overflows."""

import logging
import os
from pathlib import Path

from doblarr.models import DubJob, Segment
from doblarr.stages import fit_timing
from doblarr.stages.common import DryRunPlan
from doblarr.stages.fit_timing import _atempo_chain


def _job(tmp_path: Path, slots: list[tuple[float, float]]) -> DubJob:
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
    monkeypatch.setattr(fit_timing, "run_ffmpeg", lambda args, **k: calls.append(args))
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
    assert Path(calls[0][-1]).parent.name == "clips-fit"
    assert job.segments[0].audio_clip.parent.name == "clips-fit"


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
    assert job2.segments[0].audio_clip.parent.name == "clips-fit"
