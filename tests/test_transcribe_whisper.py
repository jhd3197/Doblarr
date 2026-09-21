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

    def load_model(model, device, compute_type=None, language=None):
        captured.update(model=model, device=device, compute_type=compute_type,
                        language=language)

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
        def __init__(self, model, device=None):
            captured["model"] = model

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
