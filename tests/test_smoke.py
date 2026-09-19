"""Smoke tests — no heavy deps, no network. Verify the skeleton holds together."""

from pathlib import Path

from doblarr.config import Config
from doblarr.models import DubJob, Segment, Speaker


def test_config_defaults():
    cfg = Config.load("does-not-exist.yaml")
    assert cfg["voicebox"]["base_url"].startswith("http")
    assert cfg.work_dir == Path("./work")
    assert cfg["dub"]["voice_mode"] in {"clone", "preset"}


def test_segment_duration():
    seg = Segment(index=0, start=1.0, end=3.5, text_src="안녕하세요")
    assert seg.duration == 2.5


def test_dubjob_summary():
    job = DubJob(input_file=Path("movie.mkv"), source_lang="ko", target_lang="es")
    job.segments = [Segment(0, 0.0, 1.0, "a"), Segment(1, 1.0, 2.0, "b")]
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00")}
    s = job.summary()
    assert "ko -> es" in s and "2 segments" in s
