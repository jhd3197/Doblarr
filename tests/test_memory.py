from pathlib import Path

import pytest

from doblarr.knowledge import memory
from doblarr.knowledge.store import snapshot
from doblarr.models import DubJob, Segment
from doblarr.stages import translate
from doblarr.store import Database


def job_for(text="I'm good.", locale="es-VE"):
    return DubJob(input_file=Path("scene.mkv"), source_lang="en", target_lang="es",
                  target_locale=locale, segments=[Segment(10, 0, 3, text)],
                  translation_options={"reuse_memory": True,
                                       "character_notes": {"SPEAKER_00": "neutral"}})


def approve(db, job):
    entry = memory.MemoryEntry(
        source_lang="en", target_locale=job.target_locale, source_text=job.segments[0].text_src,
        target_text="Estoy bien.", context=memory.scene_context(job, 0, {}), duration=3,
        status="reviewed", reviewer="Test reviewer", meaning_reviewed=True,
        naturalness_reviewed=True, timing_reviewed=True,
    )
    return memory.save(db, entry)


class Translator:
    def __init__(self):
        self.batches = []

    def translate_batch(self, payload, source, target, **kwargs):
        self.batches.append((payload, kwargs))
        return ["Traducido." for _ in payload]


def test_exact_reuse_and_revision_freezing(tmp_path):
    db = Database(tmp_path / "memory.db")
    job = job_for()
    entry = approve(db, job)
    job.knowledge_snapshot = snapshot(db)
    memory.save(db, entry.model_copy(update={"status": "retired"}))
    translator = Translator()
    translate.run(job, translator, memory_db=db)
    assert job.segments[0].text_translated == "Estoy bien."
    assert translator.batches == []
    assert job.segments[0].translation_provenance["revision"] == 1
    fresh = job_for()
    fresh.knowledge_snapshot = snapshot(db)
    translate.run(fresh, translator, memory_db=db)
    assert fresh.segments[0].text_translated == "Traducido."


@pytest.mark.parametrize("change", ["negation", "region", "register", "context", "duration",
                                    "unknown", "glossary", "unreviewed", "direction"])
def test_mismatch_falls_back(tmp_path, change):
    db = Database(tmp_path / "memory.db")
    job = job_for()
    entry = approve(db, job)
    glossary = {}
    if change == "negation":
        job.segments[0].text_src = "I'm not good."
    elif change == "region":
        job.target_locale = "es-MX"
    elif change == "register":
        job.translation_options["character_notes"]["SPEAKER_00"] = "formal"
    elif change == "unknown":
        job.translation_options["character_notes"] = {}
    elif change == "context":
        job.segments.append(Segment(20, 4, 6, "Would you like another drink?"))
    elif change == "duration":
        job.segments[0].end = 1
    elif change == "glossary":
        glossary = {"good": "bueno"}
    elif change == "direction":
        job.translation_options["direction"] = "formal"
    elif change == "unreviewed":
        memory.save(db, entry.model_copy(update={"meaning_reviewed": False}))
    job.knowledge_snapshot = snapshot(db)
    translator = Translator()
    translate.run(job, translator, memory_db=db, glossary=glossary)
    assert job.segments[0].text_translated == "Traducido."
    assert translator.batches


def test_sparse_batches_context_and_nonlatin_complete_lines(tmp_path):
    db = Database(tmp_path / "memory.db")
    job = job_for("元気です。")
    job.source_lang = "ja"
    job.segments += [Segment(77, 4, 7, "まだです。"), Segment(101, 8, 11, "行こう。")]
    for position in (0, 2):
        seg = job.segments[position]
        memory.save(db, memory.MemoryEntry(
            source_lang="ja", target_locale="es-VE", source_text=seg.text_src,
            target_text=f"Línea {seg.index}.", context=memory.scene_context(job, position, {}),
            duration=3, status="reviewed", reviewer="Source reviewer", meaning_reviewed=True,
            naturalness_reviewed=True, timing_reviewed=True,
        ))
    job.knowledge_snapshot = snapshot(db)
    translator = Translator()
    translate.run(job, translator, memory_db=db)
    assert [s.index for s in job.segments] == [10, 77, 101]
    assert [s.text_translated for s in job.segments] == ["Línea 10.", "Traducido.", "Línea 101."]
    assert translator.batches[0][1]["context"][0]["translated"] == "Línea 10."


def test_regional_adaptation_is_explicit_and_resumable():
    job = job_for()
    job.script_is_target = True
    job.segments[0].text_translated = job.segments[0].text_src
    translator = Translator()
    translate.run(job, translator)
    assert not translator.batches
    job.translation_options["adapt_region"] = True
    translate.run(job, translator)
    assert job.segments[0].text_translated == "Traducido."
    translate.run(job, translator)
    assert len(translator.batches) == 1


def test_conflicting_matches_do_not_depend_on_insert_order(tmp_path):
    db = Database(tmp_path / "memory.db")
    job = job_for()
    entry = approve(db, job)
    memory.save(db, entry.model_copy(update={"id": "other", "target_text": "No, gracias."}))
    job.knowledge_snapshot = snapshot(db)
    translate.run(job, Translator(), memory_db=db)
    assert job.segments[0].translation_provenance["reason"] == "conflicting-reviewed-lines"


def test_memory_api_private_and_revisioned(client_factory):
    client = client_factory()
    payload = dict(source_lang="en", target_locale="es-ve", source_text="Hello.",
                   target_text="Hola.", duration=2)
    response = client.post("/api/memory", json=payload)
    assert response.status_code == 200
    entry = response.json()["entry"]
    assert entry["status"] == "proposed" and entry["target_locale"] == "es-VE"
    assert client.get("/api/memory").json()["total"] == 1
    response = client.post(f"/api/memory/{entry['id']}/retire")
    assert response.json()["entry"]["revision"] == 2
    assert client.get("/api/memory").json()["total"] == 1
