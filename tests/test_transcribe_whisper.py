"""Whisper transcription path — backends faked via sys.modules; no models, no network."""

import sys
import types
from pathlib import Path

import pytest

from doblarr.models import DubJob
from doblarr.stages import transcribe


def _job_with_audio(tmp_path, vocals=True) -> DubJob:
    src = tmp_path / "film.mkv"
    src.write_text("fake video", encoding="utf-8")
    job = DubJob(input_file=src, source_lang="ko", target_lang="es")
    audio = tmp_path / "film.source.wav"
    audio.write_text("audio", encoding="utf-8")
    job.source_audio = audio
    if vocals:
        v = tmp_path / "film.vocals.wav"
        v.write_text("vocals", encoding="utf-8")
        job.vocals = v
    return job


def _fake_whisperx(captured):
    mod = types.ModuleType("whisperx")

    def load_model(model, device, device_index=0, compute_type=None, language=None):
        captured.update(model=model, device=device, device_index=device_index,
                        compute_type=compute_type, language=language)

        class _Model:
            def transcribe(self, audio):
                captured["audio"] = audio
                return {"language": "ko", "segments": [
                    {"start": 0.5, "end": 2.0, "text": "안녕\n하세요"},
                    {"text": "aligned away"},            # dropped: no timings
                    {"start": 2.5, "end": 4.0, "text": "  "},  # dropped: blank
                ]}

        return _Model()

    def load_align_model(language_code=None, device=None):
        captured["align_lang"] = language_code
        captured["align_device"] = device
        return object(), {}

    def align(segments, align_model, metadata, audio, device):
        captured["aligned"] = True
        return {"segments": segments}

    mod.load_model = load_model
    mod.load_align_model = load_align_model
    mod.align = align
    return mod


def test_whisper_path_uses_whisperx(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper", whisper_model="medium")
    assert captured["model"] == "medium"            # whisper_model knob honored
    assert captured["language"] == "ko"             # source language passed through
    assert captured["audio"] == str(job.vocals)     # separated dialogue preferred
    assert captured["aligned"] is True              # word-accurate timings requested
    assert [(s.index, s.start, s.end, s.text_src) for s in job.segments] == [
        (0, 0.5, 2.0, "안녕 하세요")]
    assert not job.script_is_target                 # whisper text is source language


def test_whisper_uses_source_audio_when_no_vocals(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path, vocals=False)
    transcribe.run(job, tmp_path / "work", source="whisper")
    assert captured["audio"] == str(job.source_audio)


def test_whisper_align_failure_keeps_raw_timings(tmp_path, monkeypatch):
    mod = _fake_whisperx({})

    def no_align_model(language_code=None, device=None):
        raise ValueError(f"no align model for {language_code}")

    mod.load_align_model = no_align_model
    monkeypatch.setitem(sys.modules, "whisperx", mod)
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper")
    assert [(s.start, s.end, s.text_src) for s in job.segments] == [
        (0.5, 2.0, "안녕 하세요")]


def test_whisper_falls_back_to_faster_whisper(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisperx", None)  # import fails
    captured = {}
    fw = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model, device=None, device_index=0, compute_type=None):
            captured.update(model=model, device=device, device_index=device_index,
                            compute_type=compute_type)

        def transcribe(self, audio, language=None, word_timestamps=False):
            captured.update(audio=audio, language=language, words=word_timestamps)
            seg = types.SimpleNamespace(start=1.0, end=2.5, text="안녕")
            return iter([seg]), types.SimpleNamespace(language="ko")

    fw.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper", whisper_model="small")
    assert captured["model"] == "small"
    assert captured["language"] == "ko" and captured["words"] is True
    assert [(s.start, s.end, s.text_src) for s in job.segments] == [(1.0, 2.5, "안녕")]


def test_whisper_missing_backend_raises(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisperx", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    job = _job_with_audio(tmp_path)
    with pytest.raises(RuntimeError, match="no whisper backend is installed"):
        transcribe.run(job, tmp_path / "work", source="whisper")


def test_whisper_requires_extracted_audio(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisperx", None)
    job = DubJob(input_file=Path("film.mkv"), source_lang="ko", target_lang="es")
    with pytest.raises(RuntimeError, match="extract stage must run first"):
        transcribe.run(job, tmp_path / "work", source="whisper")


def test_whisper_dry_run_never_imports_backend(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "whisperx", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    job = DubJob(input_file=Path("film.mkv"), source_lang="ko", target_lang="es")
    assert transcribe.run(job, tmp_path / "work", source="whisper", dry_run=True) is None
    assert job.segments == []


def _gpu(monkeypatch, torch_cuda=2, ct2=0):
    """A fake torch with `torch_cuda` CUDA devices (0 = a CPU-only wheel)."""
    torch = types.ModuleType("torch")
    torch.__version__ = "2.8.0"
    torch.version = types.SimpleNamespace(cuda="12.4" if torch_cuda else None)
    torch.cuda = types.SimpleNamespace(is_available=lambda: bool(torch_cuda),
                                       device_count=lambda: torch_cuda,
                                       empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(transcribe.hardware, "ctranslate2_cuda_devices", lambda: ct2)


def test_whisperx_gets_the_chosen_device_and_index(tmp_path, monkeypatch):
    _gpu(monkeypatch)
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper",
                   compute={"device": "auto", "transcribe_device": "cuda:1"})
    assert (captured["device"], captured["device_index"]) == ("cuda", 1)
    assert captured["compute_type"] == "float16"
    assert captured["align_device"] == "cuda:1"
    assert job.metrics["devices"] == {"transcribe": "cuda:1"}


def test_legacy_transcribe_device_still_applies(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper",
                   options={"device": "cpu"}, compute={"device": "cuda"})
    assert captured["device"] == "cpu" and captured["compute_type"] == "int8"
    # the device never enters the script cache key
    assert "compute" not in job.transcription_options


def test_faster_whisper_gets_device_and_index(tmp_path, monkeypatch):
    _gpu(monkeypatch, torch_cuda=0, ct2=1)  # CPU torch, CTranslate2 sees a GPU
    monkeypatch.setitem(sys.modules, "whisperx", None)
    captured = {}
    fw = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model, device=None, device_index=0, compute_type=None):
            captured.update(device=device, device_index=device_index, compute_type=compute_type)

        def transcribe(self, audio, language=None, word_timestamps=False):
            return iter([types.SimpleNamespace(start=1.0, end=2.5, text="안녕")]), None

    fw.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper", compute={"device": "auto"})
    assert captured == {"device": "cuda", "device_index": 0, "compute_type": "float16"}


def test_alignment_on_a_ctranslate2_only_gpu_runs_on_cpu(tmp_path, monkeypatch):
    _gpu(monkeypatch, torch_cuda=0, ct2=1)
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path)
    transcribe.run(job, tmp_path / "work", source="whisper", compute={"device": "cuda"})
    assert captured["device"] == "cuda"
    assert captured["align_device"] == "cpu"


def test_an_explicit_missing_gpu_fails_before_loading(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "whisperx", _fake_whisperx(captured))
    job = _job_with_audio(tmp_path)
    with pytest.raises(transcribe.hardware.DeviceUnavailable, match="cuda:0.*pytorch.org"):
        transcribe.run(job, tmp_path / "work", source="whisper",
                       compute={"transcribe_device": "cuda:0"})
    assert "model" not in captured
