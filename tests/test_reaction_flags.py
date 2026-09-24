"""Reaction cues the translator flags — a fake translator; no provider."""

import sys
import types
from pathlib import Path

import pytest

from doblarr.clients.translator import PromptureTranslator
from doblarr.cues import ensure_identity
from doblarr.models import DubJob, Segment
from doblarr.stages import prepare, translate


class FlaggingTranslator:
    """Translates everything; flags the texts in `reactions` as reactions."""

    def __init__(self, reactions: dict[str, str], suggest=()):
        self.reactions = reactions
        self.suggest = set(suggest)
        self.last_flags: list[dict] = []

    def translate_batch(self, segments, source_lang, target_lang, context=None, glossary=None):
        self.last_flags = [
            {"delivery": "reaction", "reaction_kind": self.reactions[s["text"]],
             "suggest_retain": s["text"] in self.suggest}
            if s["text"] in self.reactions else {"delivery": "speech"}
            for s in segments]
        return [f"es:{s['text']}" for s in segments]


def _job(*texts):
    job = DubJob(input_file=Path("film.mkv"), source_lang="ja", target_lang="es")
    job.segments = [Segment(i, i * 3.0, i * 3.0 + 1.5, text, speaker="A")
                    for i, text in enumerate(texts)]
    ensure_identity(job)
    return job


@pytest.mark.parametrize("text, kind", [("えっ", "gasp"), ("¡Ay!", "interjection"),
                                        ("あはははは", "laugh"), ("헉", "gasp"),
                                        ("Oww!", "interjection")])
def test_a_flagged_reaction_without_words_becomes_an_event(text, kind):
    job = _job("本当にそうですか", text)
    translate.run(job, FlaggingTranslator({text: kind}), flag_reactions=True)
    assert [s.text_src for s in job.segments] == ["本当にそうですか"]
    (event,) = job.nonverbal
    assert event.type == kind and event.category == "vocal"
    assert event.text == text and event.speaker == "A"
    assert event.checks["detected_by"] == "translator"
    assert event.decision == "unresolved"  # nothing inserted without a decision
    assert job.metrics["reaction_cues_by_translator"] == 1


@pytest.mark.parametrize("text", ["Hahaha, no way", "¡Ay, no!", "えっ、本当？", "Hoy",
                                  "Нет!", "[laughs]", "Oh 2"])
def test_a_flagged_cue_with_words_stays_speech(text):
    job = _job(text)
    translate.run(job, FlaggingTranslator({text: "laugh"}), flag_reactions=True)
    assert [s.text_src for s in job.segments] == [text]
    assert job.nonverbal == []
    assert job.metrics["reaction_flags_kept_as_speech"] == 1


def test_with_the_setting_off_nothing_changes():
    job = _job("えっ")
    translate.run(job, FlaggingTranslator({"えっ": "gasp"}), flag_reactions=False)
    assert [s.text_src for s in job.segments] == ["えっ"]
    assert job.nonverbal == []


def test_a_suggested_retain_is_shown_and_not_applied():
    job = _job("えっ")
    translate.run(job, FlaggingTranslator({"えっ": "gasp"}, suggest={"えっ"}),
                  flag_reactions=True)
    (event,) = job.nonverbal
    assert event.checks["suggested_decision"] == "retain"
    assert event.decision == "unresolved" and event.coverage == "unresolved"


def test_rules_detected_reactions_record_their_provenance():
    job = _job("Tsk!", "Hola")
    prepare.run(job)
    (event,) = job.nonverbal
    assert event.checks["detected_by"] == "rules"


def test_the_local_check_is_conservative():
    assert prepare.wordless("えっ") and prepare.wordless("ハハハ") and prepare.wordless("ooh")
    for text in ("本当", "yo", "no", "Hoy", "Нет", "ah 3", "(sighs)", ""):
        assert not prepare.wordless(text), text


def _fake_prompture(monkeypatch, reply):
    exceptions = types.ModuleType("prompture.exceptions")
    exceptions.ExtractionError = type("ExtractionError", (Exception,), {})
    module = types.ModuleType("prompture")
    seen = {}

    def ask_for_json(**kwargs):
        seen.update(kwargs)
        return {"json_object": reply, "usage": {}}

    module.ask_for_json = ask_for_json
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "prompture", module)
    monkeypatch.setitem(sys.modules, "prompture.exceptions", exceptions)
    return seen


def test_the_prompture_translator_reports_flags_on_the_same_request(monkeypatch):
    seen = _fake_prompture(monkeypatch, {"translations": [
        {"segment_id": 1, "text": "¿Eh?", "delivery": "reaction", "reaction_kind": "gasp"},
        {"segment_id": 2, "text": "Hola"}]})
    translator = PromptureTranslator("fake/model")
    translator._driver = object()
    translator.flag_reactions = True
    out = translator.translate_batch([{"text": "えっ"}, {"text": "こんにちは"}], "ja", "es")
    assert out == ["¿Eh?", "Hola"]
    assert [f["delivery"] for f in translator.last_flags] == ["reaction", "speech"]
    assert "delivery to reaction" in seen["system_prompt"]
    translator.flag_reactions = False
    translator.translate_batch([{"text": "えっ"}, {"text": "こんにちは"}], "ja", "es")
    assert "delivery to reaction" not in seen["system_prompt"]
