"""Select the requested source language, including ISO-639-2 tags."""

import json
from pathlib import Path

import pytest

from doblarr.models import DubJob
from doblarr.stages import extract


def test_extract_selects_japanese_and_invalidates_old_track(tmp_path, monkeypatch):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    job = DubJob(video, "ja", "es")
    streams = {"streams": [{"index": 1, "tags": {"language": "eng"}},
                           {"index": 2, "tags": {"language": "jpn"}}]}
    monkeypatch.setattr(extract, "run_ffprobe", lambda *a, **k: json.dumps(streams))
    calls = []

    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"audio")

    monkeypatch.setattr(extract, "run_ffmpeg", render)
    extract.run(job, tmp_path)
    assert calls[0][calls[0].index("-map") + 1] == "0:2"
    extract.run(job, tmp_path)
    assert len(calls) == 1
    job.source_lang = "en"
    extract.run(job, tmp_path)
    assert len(calls) == 2
    assert calls[1][calls[1].index("-map") + 1] == "0:1"


def test_missing_source_language_does_not_silently_pick_english(tmp_path, monkeypatch):
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    monkeypatch.setattr(extract, "run_ffprobe", lambda *a, **k: json.dumps(
        {"streams": [{"index": 1, "tags": {"language": "eng"}}]}))
    with pytest.raises(RuntimeError, match="no unambiguous ja"):
        extract.run(job, tmp_path)
