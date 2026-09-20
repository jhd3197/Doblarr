from pathlib import Path
from types import SimpleNamespace

import pytest

from doblarr.models import DubJob, Segment
from doblarr.stages import fit_timing, prepare, translate


def test_scene_batches_have_context_budgets_and_checkpoint_before_failure(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [Segment(i, i * 3, i * 3 + 2, f"line {i}") for i in range(5)]
    calls, checkpoints = [], []

    def batch(segments, source, target, **kw):
        calls.append((segments, kw))
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return ["translated " + s["text"] for s in segments]

    translator = SimpleNamespace(translate_batch=batch)
    with pytest.raises(RuntimeError):
        translate.run(
            job,
            translator,
            batch_size=2,
            glossary={"name": "Nombre"},
            checkpoint=lambda: checkpoints.append(1),
        )
    assert checkpoints == [1]
    assert sum(bool(s.text_translated) for s in job.segments) == 2
    assert calls[0][0][0]["target_chars"] == 28
    assert len(calls[0][1]["context"]) == 5
    assert calls[0][1]["glossary"] == {"name": "Nombre"}
    translate.run(job, translator, batch_size=2)
    assert all(s.text_translated for s in job.segments)
    assert calls[2][0][0]["text"] == "line 2"


def test_cleanup_merges_only_unfinished_same_speaker_speech(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [
        Segment(0, 0, 1, "[music]"),
        Segment(1, 1, 2, "I think", speaker="A"),
        Segment(2, 2.1, 3, "we should go.", speaker="A"),
        Segment(3, 3.1, 4, "Wait", speaker="B"),
    ]
    prepare.run(job)
    assert [s.index for s in job.segments] == [1, 3]
    assert job.segments[0].text_src == "I think we should go."
    assert job.segments[0].end == 3


def test_timing_repair_only_regenerates_overlong_clip(tmp_path, monkeypatch):
    clip = tmp_path / "long.wav"
    clip.write_bytes(b"audio")
    other = tmp_path / "short.wav"
    other.write_bytes(b"audio")
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [
        Segment(0, 0, 2, "Hello", text_translated="A very long greeting", audio_clip=clip),
        Segment(1, 3, 5, "Bye", text_translated="Bye", audio_clip=other),
    ]
    durations = {clip: 4, other: 1}
    monkeypatch.setattr(fit_timing, "_duration", lambda p, **kw: durations[p])
    calls = []

    def regenerate(seg):
        calls.append(seg.index)
        durations[Path(seg.audio_clip)] = 1.8

    translator = SimpleNamespace(shorten=lambda *args: "Hello!")
    fit_timing.run(job, tmp_path, translator=translator, regenerate=regenerate)
    assert calls == [0]
    assert job.segments[0].text_translated == "Hello!"
    assert not job.segments[0].issues


def test_timing_retries_are_bounded_and_unresolved_lines_flagged(tmp_path, monkeypatch):
    clip = tmp_path / "long.wav"
    clip.write_bytes(b"audio")
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [Segment(0, 0, 1, "a very long line", audio_clip=clip)]
    monkeypatch.setattr(fit_timing, "_duration", lambda *a, **kw: 4)
    monkeypatch.setattr(
        fit_timing, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"fit")
    )
    calls = []
    translator = SimpleNamespace(shorten=lambda text, *args: text[:-1])
    fit_timing.run(
        job,
        tmp_path,
        translator=translator,
        regenerate=lambda seg: calls.append(seg.index),
        max_attempts=2,
    )
    assert calls == [0, 0]
    assert "timing_overflow" in job.segments[0].issues
