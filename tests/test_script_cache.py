"""Script-cache tests — transcript/translation survive a crashed job."""

import os
from pathlib import Path

from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import diarize, transcribe
from doblarr.stages.common import load_script, save_script, script_path


def _job(tmp_path: Path) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    os.utime(src, (1000.0, 1000.0))
    return DubJob(input_file=src, source_lang="ja", target_lang="es")


def _scripted_job(tmp_path: Path) -> DubJob:
    job = _job(tmp_path)
    job.segments = [
        Segment(index=0, start=1.0, end=3.0, text_src="原文一",
                speaker="NARRATOR", text_translated="línea uno"),
        Segment(index=1, start=4.0, end=6.5, text_src="原文二",
                speaker="NARRATOR", text_translated="línea dos"),
    ]
    job.speakers = {"NARRATOR": Speaker(label="NARRATOR")}
    return job


def test_script_roundtrip(tmp_path):
    job = _scripted_job(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    p = save_script(job, work)
    assert p == script_path(job, work)

    fresh = _job(tmp_path)
    assert load_script(fresh, work) == p
    assert [s.text_src for s in fresh.segments] == ["原文一", "原文二"]
    assert fresh.segments[0].text_translated == "línea uno"
    assert fresh.segments[1].end == 6.5
    assert list(fresh.speakers) == ["NARRATOR"]


def test_load_script_respects_freshness_and_force(tmp_path):
    job = _scripted_job(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    p = save_script(job, work)
    os.utime(p, (500.0, 500.0))  # older than the input file -> stale
    assert load_script(_job(tmp_path), work) is None
    os.utime(p, (2000.0, 2000.0))
    assert load_script(_job(tmp_path), work, force=True) is None  # force bypasses


def test_transcribe_restores_cached_script(tmp_path):
    job = _scripted_job(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    save_script(job, work)

    fresh = _job(tmp_path)
    # subtitles source with no subtitle track would raise — the cache must win
    transcribe.run(fresh, work, source="subtitles")
    assert len(fresh.segments) == 2
    assert fresh.segments[0].text_translated == "línea uno"


def test_diarize_skips_when_speakers_restored(tmp_path):
    job = _scripted_job(tmp_path)  # speakers already assigned
    diarize.run(job, enabled=True, dry_run=False)
    assert list(job.speakers) == ["NARRATOR"]  # untouched, no pyannote needed
