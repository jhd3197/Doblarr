"""The pipeline puts every translation request behind the holdout guard."""

import json
from types import SimpleNamespace

import pytest

from doblarr.clients.translator import VoiceboxTranslator
from doblarr.pipeline import _holdout_guard
from doblarr.studio.holdout import HoldoutViolation, guard_translator

HELD_OUT = "Kaito never told Mina where the lighthouse key was hidden"


def _holdout_file(tmp_path):
    path = tmp_path / "held-out.json"
    path.write_text(json.dumps({"utterances": [{"utt_id": "u1", "start": 0, "end": 2,
                                                "text": HELD_OUT}]}), encoding="utf-8")
    return path


def test_no_holdout_files_means_no_guard():
    assert _holdout_guard({"holdout_files": []}) is None


def test_held_out_text_never_reaches_the_model(tmp_path):
    guard = _holdout_guard({"holdout_files": [str(_holdout_file(tmp_path))]})
    sent = []
    client = SimpleNamespace(llm_generate=lambda prompt, **kw: sent.append(prompt) or "{}")
    translator = guard_translator(VoiceboxTranslator(client), guard, "translation")
    with pytest.raises(HoldoutViolation):
        translator.translate(HELD_OUT, "en", "es", 80)
    assert sent == []
    assert guard.summary()["blocked"] >= 1


def test_run_job_wraps_the_translator_when_holdout_files_are_set(tmp_path, monkeypatch):
    from doblarr import pipeline
    from doblarr.config import Config
    from doblarr.models import DubJob

    wrapped = []

    def record(translator, guard, where):
        wrapped.append(where)
        raise SystemExit   # stop right after the wiring under test

    monkeypatch.setattr("doblarr.studio.holdout.guard_translator", record)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"not really a video")
    config = Config.load(tmp_path / "c.yaml").with_overrides({
        "paths.work_dir": str(tmp_path / "work"),
        "translate.holdout_files": [str(_holdout_file(tmp_path))]})
    with pytest.raises(SystemExit):
        pipeline.run_job(DubJob(input_file=source, source_lang="ja", target_lang="es"),
                         config, dry_run=False)
    assert wrapped == ["translation"]
