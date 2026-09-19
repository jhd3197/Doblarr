"""Stage checkpointing tests — fresh artifacts skip, force bypasses."""

import os
from pathlib import Path

from doblarr.models import DubJob, Segment
from doblarr.stages import extract, mix, mux, synthesize
from doblarr.stages.common import CachedPlan, cached


def _job(tmp_path: Path, mtime: float = 1000.0) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (mtime, mtime))
    return DubJob(input_file=src, source_lang="ko", target_lang="es")


def _fresh(path: Path, mtime: float = 2000.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("artifact", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _spy_ffmpeg(monkeypatch, module):
    """Replace a stage module's run_ffmpeg with a recording no-op."""
    calls = []
    monkeypatch.setattr(module, "run_ffmpeg", lambda *a, **k: calls.append(a))
    return calls


def test_cached_helper(tmp_path):
    job = _job(tmp_path)
    fresh = _fresh(tmp_path / "work" / "out.wav")
    stale = _fresh(tmp_path / "work" / "old.wav", mtime=500.0)
    assert cached(fresh, job.input_file) is not None
    assert cached([fresh], job.input_file) is not None
    assert cached(stale, job.input_file) is None              # older than input
    assert cached(tmp_path / "work" / "missing.wav", job.input_file) is None
    assert cached(fresh, job.input_file, force=True) is None  # force bypasses
    assert cached([], job.input_file) is None                 # nothing declared
    assert cached(fresh, tmp_path / "no-input.mkv") is None   # input unverifiable


def test_extract_skips_fresh_artifact(tmp_path, monkeypatch):
    job = _job(tmp_path)
    work = tmp_path / "work"
    _fresh(work / "movie.source.wav")
    calls = _spy_ffmpeg(monkeypatch, extract)
    assert extract.run(job, work) is None          # skip returns None (decorator)
    assert job.source_audio == work / "movie.source.wav"  # planning still ran
    assert calls == []                              # ffmpeg never invoked


def test_extract_force_and_stale_rerun(tmp_path, monkeypatch):
    job = _job(tmp_path)
    work = tmp_path / "work"
    calls = _spy_ffmpeg(monkeypatch, extract)
    _fresh(work / "movie.source.wav", mtime=500.0)  # stale: older than the input
    extract.run(job, work)
    assert len(calls) == 1
    # force bypasses even a fresh artifact
    _fresh(work / "movie.source.wav")
    extract.run(job, work, force=True)
    assert len(calls) == 2


def test_synthesize_checkpoint_restores_clip_paths(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 0.0, 1.0, "a"), Segment(1, 1.0, 2.0, "b")]
    work = tmp_path / "work"
    _fresh(work / "clips" / "line_0000.wav")
    _fresh(work / "clips" / "line_0001.wav")
    synthesize.run(job, vb=None, work_dir=work)  # no voicebox client touched
    assert job.segments[0].audio_clip == work / "clips" / "line_0000.wav"
    assert job.segments[1].audio_clip.name == "line_0001.wav"


def test_synthesize_partial_clips_do_not_skip(tmp_path):
    job = _job(tmp_path)
    job.segments = [Segment(0, 0.0, 1.0, "a"), Segment(1, 1.0, 2.0, "b")]
    work = tmp_path / "work"
    _fresh(work / "clips" / "line_0000.wav")  # line_0001 missing
    clips = [work / "clips" / f"line_{s.index:04d}.wav" for s in job.segments]
    assert cached(clips, job.input_file) is None


def test_mix_and_mux_skip(tmp_path, monkeypatch):
    job = _job(tmp_path)
    work = tmp_path / "work"
    _fresh(work / "movie.es.dub.wav")
    assert isinstance(mix.run.__wrapped__(job, work), CachedPlan)  # pre-decorator
    mix_calls = _spy_ffmpeg(monkeypatch, mix)
    mix.run(job, work)
    assert job.dubbed_track == work / "movie.es.dub.wav"
    assert mix_calls == []

    out = tmp_path / "output"
    _fresh(out / "movie.mkv")
    mux_calls = _spy_ffmpeg(monkeypatch, mux)
    mux.run(job, out)
    assert job.output_file == out / "movie.mkv"
    assert mux_calls == []
