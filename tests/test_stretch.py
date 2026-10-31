"""timing.stretcher — atempo or Rubber Band for both timing owners."""

import os
from pathlib import Path

import pytest

from doblarr import phrases, stretch
from doblarr.models import DubJob, Segment
from doblarr.stages import fit_timing


def test_tempo_filter_per_stretcher():
    assert stretch.tempo_filter(1.25) == "atempo=1.2500"
    assert stretch.tempo_filter(5.0, "atempo") == "atempo=2.0,atempo=2.0,atempo=1.2500"
    rb = stretch.tempo_filter(1.25, "rubberband")
    assert rb.startswith("rubberband=tempo=1.2500:")
    assert "atempo" not in rb  # one filter, no chain: rubberband takes any factor


def test_unknown_stretcher_is_refused():
    assert stretch.normalize(None) == "atempo"
    assert stretch.normalize("RubberBand") == "rubberband"
    with pytest.raises(ValueError):
        stretch.normalize("sox")
    with pytest.raises(ValueError):
        phrases.settings({"stretcher": "sox"})


def test_missing_rubberband_falls_back_to_atempo(monkeypatch):
    monkeypatch.setattr(stretch, "_has_rubberband", lambda: False)
    assert stretch.resolve("rubberband") == "atempo"
    monkeypatch.setattr(stretch, "_has_rubberband", lambda: True)
    assert stretch.resolve("rubberband") == "rubberband"


def _job(tmp_path: Path) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    job = DubJob(input_file=src, source_lang="ja", target_lang="es")
    clips = tmp_path / "work" / "clips"
    clips.mkdir(parents=True)
    clip = clips / "line_0000.wav"
    clip.write_text("audio", encoding="utf-8")
    os.utime(clip, (2000, 2000))
    job.segments.append(Segment(0, 0.0, 2.0, "Kaito: ya voy", audio_clip=clip))
    return job


def _fit(tmp_path, monkeypatch, options):
    job = _job(tmp_path)
    calls = []
    monkeypatch.setattr(fit_timing, "media_duration", lambda path, **k: 2.4)

    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"audio")

    monkeypatch.setattr(fit_timing, "run_ffmpeg", render)
    monkeypatch.setattr(stretch, "_has_rubberband", lambda: True)
    fit_timing.run(job, tmp_path / "work", options=options)
    return job, calls


def test_fit_timing_uses_rubberband_when_set(tmp_path, monkeypatch):
    job, calls = _fit(tmp_path, monkeypatch, {"stretcher": "rubberband", "pacing": "off"})
    assert any(a.startswith("rubberband=tempo=1.2000") for a in calls[0])
    clip = Path(job.segments[0].audio_clip)
    assert clip.parent.name == "fit" and clip.name.endswith(".rubberband.wav")


def test_stretcher_changes_the_fit_fingerprint(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, _ = _fit(tmp_path / "a", monkeypatch, {"pacing": "off"})
    b, _ = _fit(tmp_path / "b", monkeypatch, {"pacing": "off", "stretcher": "rubberband"})
    fa = a.segments[0].audio.render("fitted").fingerprint
    fb = b.segments[0].audio.render("fitted").fingerprint
    assert fa != fb
