"""Diarize stage — speaker assignment and the pyannote real path (all faked)."""

import sys
import types
from collections import namedtuple

import pytest

from doblarr.models import DubJob, Segment
from doblarr.stages import diarize

Turn = namedtuple("Turn", ["start", "end"])


def _fake_pyannote(monkeypatch):
    """Make `import pyannote.audio` succeed without the real package."""
    pkg = types.ModuleType("pyannote")
    sub = types.ModuleType("pyannote.audio")
    pkg.audio = sub
    monkeypatch.setitem(sys.modules, "pyannote", pkg)
    monkeypatch.setitem(sys.modules, "pyannote.audio", sub)


class FakeDiarization:
    """Mimics pyannote's Annotation.itertracks(yield_label=True)."""

    def __init__(self, tracks):
        self._tracks = tracks  # list of (start, end, label)

    def itertracks(self, yield_label=False):
        assert yield_label
        for start, end, label in self._tracks:
            yield Turn(start, end), None, label


def _job(tmp_path, segments) -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    job = DubJob(input_file=src, source_lang="ko", target_lang="es")
    job.vocals = tmp_path / "movie.vocals.wav"
    job.segments = segments
    return job


def test_assign_speakers_by_max_overlap(tmp_path):
    job = _job(tmp_path, [
        Segment(0, 0.0, 4.0, "a"),    # mostly A
        Segment(1, 4.5, 6.0, "b"),    # mostly B
        Segment(2, 3.0, 5.0, "c"),    # straddles A/B evenly -> earlier turn wins
        Segment(3, 50.0, 52.0, "d"),  # outside every turn -> keeps SPEAKER_00
    ])
    diar = FakeDiarization([(0.0, 4.0, "SPEAKER_01"), (4.0, 8.0, "SPEAKER_02")])
    diarize._assign_speakers(job, diar)
    assert [s.speaker for s in job.segments] == [
        "SPEAKER_01", "SPEAKER_02", "SPEAKER_01", "SPEAKER_00"]
    assert list(job.speakers) == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]


def test_real_path_with_fake_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "fake-token")
    _fake_pyannote(monkeypatch)
    seen = {}

    class FakePipe:
        def __call__(self, audio):
            seen["audio"] = audio
            return FakeDiarization([(0.0, 5.0, "SPEAKER_00"), (5.0, 9.0, "SPEAKER_01")])

    def fake_load(token):
        seen["token"] = token
        return FakePipe()

    monkeypatch.setattr(diarize, "_load_pipeline", fake_load)
    job = _job(tmp_path, [Segment(0, 0.0, 4.0, "a"), Segment(1, 6.0, 8.0, "b")])
    diarize.run(job, enabled=True, dry_run=False)
    assert seen == {"token": "fake-token", "audio": str(job.vocals)}
    assert [s.speaker for s in job.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert list(job.speakers) == ["SPEAKER_00", "SPEAKER_01"]


def test_real_path_requires_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "fake-token")
    _fake_pyannote(monkeypatch)
    job = _job(tmp_path, [])
    job.vocals = None
    job.source_audio = None
    with pytest.raises(RuntimeError, match="extract/separate"):
        diarize.run(job, enabled=True, dry_run=False)


def test_pipeline_load_failure_degrades_to_narrator(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "bad-token")
    _fake_pyannote(monkeypatch)

    def boom(token):
        raise OSError("401 Unauthorized")

    monkeypatch.setattr(diarize, "_load_pipeline", boom)
    job = _job(tmp_path, [])
    diarize.run(job, enabled=True, dry_run=False)
    assert list(job.speakers) == ["NARRATOR"]
