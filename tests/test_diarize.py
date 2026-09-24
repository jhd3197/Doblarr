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

    def fake_load(token, device=None):
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


def _fake_torch(monkeypatch, cuda_devices):
    torch = types.ModuleType("torch")
    torch.__version__ = "2.8.0"
    torch.version = types.SimpleNamespace(cuda="12.4")
    torch.cuda = types.SimpleNamespace(is_available=lambda: bool(cuda_devices),
                                       device_count=lambda: cuda_devices,
                                       empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    torch.device = lambda name: ("device", name)
    monkeypatch.setitem(sys.modules, "torch", torch)


def test_pipeline_moves_to_the_chosen_device(tmp_path, monkeypatch):
    _fake_torch(monkeypatch, 2)
    moved = []

    class Pipe:
        def to(self, device):
            moved.append(device)

    class FakePipeline:
        @staticmethod
        def from_pretrained(model_id, use_auth_token=None):
            return Pipe()

    _fake_pyannote(monkeypatch)
    sys.modules["pyannote.audio"].Pipeline = FakePipeline
    device = diarize.hardware.resolve_device("diarize", {"diarize_device": "cuda:1"})
    diarize._load_pipeline("token", device)
    assert moved == [("device", "cuda:1")]


def test_cpu_pipeline_is_not_moved(monkeypatch):
    moved = []

    class Pipe:
        def to(self, device):
            moved.append(device)

    class FakePipeline:
        @staticmethod
        def from_pretrained(model_id, use_auth_token=None):
            return Pipe()

    _fake_pyannote(monkeypatch)
    sys.modules["pyannote.audio"].Pipeline = FakePipeline
    diarize._load_pipeline("token", diarize.hardware.Device())
    assert moved == []


def test_a_missing_explicit_gpu_fails_rather_than_losing_the_cast(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "fake-token")
    _fake_pyannote(monkeypatch)
    job = _job(tmp_path, [Segment(0, 0.0, 4.0, "a")])
    with pytest.raises(diarize.hardware.DeviceUnavailable, match="diarize"):
        diarize.run(job, enabled=True, compute={"device": "cuda"})
    assert not job.speakers
