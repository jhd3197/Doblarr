"""Translation prep pass — a fake translator; no provider, no network."""

import json
from pathlib import Path

import pytest

from doblarr import prepass
from doblarr.budget import RequestBudget
from doblarr.clients.translator import TranslationError
from doblarr.cues import ensure_identity
from doblarr.knowledge import memory
from doblarr.models import DubJob, Segment
from doblarr.stages import translate


class FakeTranslator:
    model = "fake/model"
    endpoint = None

    def __init__(self, terms=(), corrections=(), fail=False):
        self.requests = []
        self.batches = []
        self.terms = list(terms)
        self.corrections = list(corrections)
        self.fail = fail
        self.provider_calls = 0

    def analyze_script(self, cues, source_lang, target_lang, *, want_terms=False,
                       want_corrections=False, title="", summaries=None):
        if self.fail:
            raise TranslationError("provider down")
        self.provider_calls += 1
        self.requests.append({"cues": cues, "summaries": summaries, "terms": want_terms,
                              "corrections": want_corrections})
        if summaries:
            return {"summary": "merged: " + " | ".join(summaries)}
        return {"summary": f"window of {len(cues)} cues",
                "terms": self.terms if want_terms else [],
                "corrections": self.corrections if want_corrections else []}

    def translate_batch(self, segments, source_lang, target_lang, context=None,
                        glossary=None, synopsis=None):
        self.batches.append({"synopsis": synopsis, "glossary": dict(glossary or {})})
        return [f"es:{s['text']}" for s in segments]


def _job(lines=4, text="hola"):
    job = DubJob(input_file=Path("film.mkv"), source_lang="ja", target_lang="es")
    job.segments = [Segment(i, i * 3.0, i * 3.0 + 2.0, f"{text} {i}") for i in range(lines)]
    ensure_identity(job)
    return job


def test_off_makes_no_calls(tmp_path):
    job, fake = _job(), FakeTranslator()
    assert prepass.analyze(job, fake, "off", work_dir=tmp_path) is None
    assert prepass.apply(job, None, {}) is None
    assert fake.requests == [] and "prepass" not in job.metrics


def test_a_long_script_is_read_in_bounded_windows_and_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(prepass, "WINDOW_CHARS", 300)
    job, fake = _job(lines=40, text="x" * 40), FakeTranslator()
    result = prepass.analyze(job, fake, "summary", work_dir=tmp_path)
    reads = [r for r in fake.requests if r["cues"]]
    assert len(reads) > 1
    for request in reads:  # no single request carries more than the cap
        size = sum(len(json.dumps(c, ensure_ascii=False)) for c in request["cues"])
        assert size <= 300
    assert sum(len(r["cues"]) for r in reads) == 40  # every cue read exactly once
    assert any(r["summaries"] for r in fake.requests)  # merged hierarchically
    assert result["state"] == "complete"
    assert len(result["summary"]) <= prepass.SUMMARY_CHARS


def test_the_summary_reaches_the_translation_marked_generated(tmp_path):
    job, fake = _job(), FakeTranslator()
    synopsis = prepass.apply(job, prepass.analyze(job, fake, "summary", work_dir=tmp_path), {})
    assert synopsis == "window of 4 cues"
    assert job.metrics["prepass"]["summary_generated"] is True
    translate.run(job, fake, synopsis=synopsis)
    assert fake.batches and all(b["synopsis"] == synopsis for b in fake.batches)
    # the synopsis is part of each line's context, so a new one retranslates
    assert all(s.memory_context.get("synopsis") for s in job.segments)


def test_without_a_synopsis_the_context_key_is_unchanged():
    job = _job()
    job.translation_options = {"adaptation": "natural"}
    plain = memory.scene_context(job, 0, {})
    job.translation_options = {"adaptation": "natural", "prepass": "off"}
    assert memory.scene_context(job, 0, {}) == plain
    assert "synopsis" not in plain


def test_term_candidates_never_enter_the_glossary_and_the_glossary_wins(tmp_path):
    terms = [{"source": "Ginko", "target": "Guinko", "kind": "name", "confidence": 0.9},
             {"source": "Mushi", "target": "Mushi", "kind": "term", "confidence": 0.7},
             {"source": "Tanyuu", "target": "Tanyu", "kind": "name", "confidence": 0.8}]
    job, fake = _job(), FakeTranslator(terms=terms)
    glossary = {"Ginko": "Ginko", "Mushi": "Mushi"}
    prepass.apply(job, prepass.analyze(job, fake, "summary_terms", work_dir=tmp_path), glossary)
    status = {t["source"]: t["status"] for t in job.metrics["prepass"]["terms"]}
    assert status == {"Ginko": "superseded", "Mushi": "confirmed", "Tanyuu": "candidate"}
    assert glossary == {"Ginko": "Ginko", "Mushi": "Mushi"}  # untouched
    translate.run(job, fake, glossary=glossary)
    assert "Tanyuu" not in fake.batches[0]["glossary"]


def test_summary_mode_asks_for_no_terms(tmp_path):
    job, fake = _job(), FakeTranslator(terms=[{"source": "A", "target": "B"}])
    result = prepass.analyze(job, fake, "summary", work_dir=tmp_path)
    assert result["terms"] == [] and fake.requests[0]["terms"] is False


def test_corrections_are_findings_and_never_change_the_text(tmp_path):
    job = _job()
    cue = job.segments[1].cue_id
    fake = FakeTranslator(corrections=[{"cue_id": cue, "heard": "hola 1",
                                        "suggested": "ola 1", "reason": "context"}])
    before = [s.text_src for s in job.segments]
    prepass.apply(job, prepass.analyze(job, fake, "summary", work_dir=tmp_path,
                                       corrections=True), {})
    assert [s.text_src for s in job.segments] == before
    (finding,) = [f for f in job.segments[1].findings if f.code == "source_suspect"]
    assert finding.severity == "info" and finding.evidence["suggested"] == "ola 1"
    assert fake.requests[0]["corrections"] is True


def test_corrections_are_only_asked_for_recognized_speech(tmp_path):
    fake = FakeTranslator(corrections=[{"cue_id": "x", "heard": "a", "suggested": "b"}])
    result = prepass.analyze(_job(), fake, "summary", work_dir=tmp_path, corrections=False)
    assert result["corrections"] == [] and fake.requests[0]["corrections"] is False


def test_a_rerun_uses_the_cache(tmp_path):
    fake = FakeTranslator()
    prepass.analyze(_job(), fake, "summary", work_dir=tmp_path)
    calls = len(fake.requests)
    again = prepass.analyze(_job(), fake, "summary", work_dir=tmp_path)
    assert len(fake.requests) == calls and again["summary"] == "window of 4 cues"
    prepass.analyze(_job(text="adios"), fake, "summary", work_dir=tmp_path)
    assert len(fake.requests) > calls  # a different script is read again


def test_the_shared_budget_bounds_the_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(prepass, "WINDOW_CHARS", 200)
    job, fake = _job(lines=20, text="y" * 40), FakeTranslator()
    result = prepass.analyze(job, fake, "summary", work_dir=tmp_path,
                             budget=RequestBudget(2))
    assert len(fake.requests) == 2 and result["state"] == "budget_exhausted"
    assert not list(tmp_path.glob("prepass/*.json"))  # a partial pass is not cached


def test_a_failing_provider_leaves_translation_as_it_was(tmp_path):
    job = _job()
    assert prepass.analyze(job, FakeTranslator(fail=True), "summary", work_dir=tmp_path) is None
    assert job.metrics["prepass"]["state"] == "failed"


def test_a_provider_without_the_pass_is_reported(tmp_path):
    job = _job()
    assert prepass.analyze(job, object(), "summary", work_dir=tmp_path) is None
    assert job.metrics["prepass"]["state"] == "unsupported"


def test_windows_pack_consecutive_cues():
    cues = [{"id": str(i), "text": "abc"} for i in range(5)]
    assert [len(w) for w in prepass.windows(cues, cap=60)] == [2, 2, 1]
    assert prepass.windows([], cap=60) == []


def test_an_unknown_mode_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="translate.prepass"):
        prepass.analyze(_job(), FakeTranslator(), "everything", work_dir=tmp_path)
