from pathlib import Path
from types import SimpleNamespace

import pytest

from doblarr.models import DubJob, Segment
from doblarr.review import apply_edits
from doblarr.stages import diarize, extract, mix, prepare


def test_cleanup_preserves_parenthetical_dialogue(tmp_path):
    job = DubJob(tmp_path / "film.mkv", "en", "es")
    job.segments = [
        Segment(i, i * 3, i * 3 + 1, text)
        for i, text in enumerate(["(No!)", "[We can hear you.]", "(laughing)", "[MUSIC]", "♪"])
    ]
    prepare.run(job)
    assert [s.text_src for s in job.segments] == ["(No!)", "[We can hear you.]"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_config_line_edits_reject_nonfinite_times(tmp_path, value):
    job = DubJob(tmp_path / "film.mkv", "en", "es")
    job.segments = [Segment(0, 0, 1, "Hello")]
    with pytest.raises(ValueError, match="positive time window"):
        apply_edits(job, {"0": {"end": value}})


def test_extract_recovers_from_interrupted_receipt(tmp_path, monkeypatch):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"video")
    job = DubJob(source, "en", "es")
    calls = []
    monkeypatch.setattr(extract, "_select_audio_stream", lambda *a: 1)

    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"audio")

    monkeypatch.setattr(extract, "run_ffmpeg", render)
    extract.run(job, tmp_path)
    extract.run(job, tmp_path)
    assert len(calls) == 1
    job.source_audio.with_suffix(".json").write_text('{"input":')
    extract.run(job, tmp_path)
    assert len(calls) == 2


def test_aligned_dialogue_splits_at_speaker_change(tmp_path):
    job = DubJob(tmp_path / "film.mkv", "en", "es")
    job.segments = [
        Segment(
            0,
            0,
            4,
            "Hello there. Yes!",
            words=[
                {"word": "Hello", "start": 0, "end": 1},
                {"word": "there.", "start": 1, "end": 2},
                {"word": "Yes!", "start": 3, "end": 4},
            ],
        )
    ]
    turns = [
        (SimpleNamespace(start=0, end=2), None, "A"),
        (SimpleNamespace(start=3, end=4), None, "B"),
    ]
    diarize._assign_speakers(job, SimpleNamespace(itertracks=lambda **kw: iter(turns)))
    assert [(s.index, s.speaker, s.text_src, s.start, s.end) for s in job.segments] == [
        (0, "A", "Hello there.", 0, 2),
        (1, "B", "Yes!", 3, 4),
    ]


def test_large_mix_rebuilds_only_changed_bus(tmp_path, monkeypatch):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"video")
    job = DubJob(source, "en", "es")
    job.source_audio = tmp_path / "bed.wav"
    job.source_audio.write_bytes(b"bed")
    for i in range(50):
        clip = tmp_path / f"line_{i}.wav"
        clip.write_bytes(b"clip")
        job.segments.append(Segment(i, i * 2, i * 2 + 1, "Hi", audio_clip=clip))
    calls = []

    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"mixed audio")

    monkeypatch.setattr(mix, "run_ffmpeg", render)
    monkeypatch.setattr(mix, "_duration", lambda *a, **kw: 102)
    mix.run(job, tmp_path)
    assert len(calls) == 4
    assert max(args.count("-i") for args in calls) <= 24
    job.segments[0].audio_clip.write_bytes(b"a new take")
    mix.run(job, tmp_path)
    assert len(calls) == 6
    assert job.metrics["mix_bus_cache_hits"] == 2
