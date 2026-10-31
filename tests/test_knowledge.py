"""Knowledge engine tests — matching, resolver precedence, freezing, integration."""

import json
import math
import struct
import time
import wave
from pathlib import Path

import pytest

from doblarr.knowledge import (
    Entry,
    KnowledgeSelection,
    PhraseMatcher,
    Realization,
    boundary_policy,
    save_entry,
    save_realization,
    snapshot,
)
from doblarr.knowledge.resolver import line_ref
from doblarr.store import Database


def make_db(tmp_path):
    return Database(tmp_path / "knowledge.db")


def add_rule(
    db,
    phrase="Sora",
    kind="pronunciation",
    locale="es",
    scope="personal",
    status="reviewed",
    scope_ref="",
    coverage=(),
    suppresses="",
    sense="",
    source_form="",
    origin="local",
    register=None,
):
    return save_entry(
        db,
        Entry(
            phrase=phrase,
            kind=kind,
            locale=locale,
            scope=scope,
            status=status,
            scope_ref=scope_ref,
            coverage=coverage,
            suppresses=suppresses,
            sense=sense,
            source_form=source_form,
            origin=origin,
            register=register,
        ),
    )


def add_realization(
    db,
    entry,
    replacement="Zo-ra",
    engine="engA",
    status="reviewed",
    model=None,
    voice=None,
    origin="local",
):
    return save_realization(
        db,
        Realization(
            entry_id=entry.id,
            replacement=replacement,
            engine=engine,
            status=status,
            model=model,
            voice=voice,
            origin=origin,
        ),
    )


def selection(db, locale="es", title_ref="", show_ref="", legacy=None, pinned=None):
    return KnowledgeSelection.load(
        db, snapshot=pinned, locale=locale, title_ref=title_ref, show_ref=show_ref, legacy=legacy
    )


# -- phrase matching ---------------------------------------------------------


def test_matching_prefers_the_longest_phrase_at_each_position():
    matcher = PhraseMatcher({"ginkgo biloba": "TREE", "ginkgo": "G"}, "word")
    assert matcher.apply("ginkgo biloba y ginkgo") == ("TREE y G", ["ginkgo biloba", "ginkgo"])


def test_matching_applies_once_without_cascading():
    matcher = PhraseMatcher({"a": "b", "b": "c"}, "word")
    assert matcher.apply("a b") == ("b c", ["a", "b"])  # "a" -> "b" is not re-substituted


def test_matching_normalizes_unicode_and_preserves_diacritics():
    matcher = PhraseMatcher({"canción": "X"}, "word")
    decomposed = "la canción de ayer"
    spoken, matched = matcher.apply(decomposed)
    assert spoken == "la X de ayer"
    assert matched == ["canción"]
    assert PhraseMatcher({"señor": "Y"}).apply("el señor")[0] == "el Y"


def test_matching_is_case_sensitive_by_policy():
    matcher = PhraseMatcher({"Sora": "X"}, "word")
    assert matcher.apply("sora") == ("sora", [])
    assert matcher.apply("Sora") == ("X", ["Sora"])


def test_matching_respects_word_boundaries():
    matcher = PhraseMatcher({"gato": "X"}, "word")
    assert matcher.apply("gatos") == ("gatos", [])
    assert matcher.apply("el gato.") == ("el X.", ["gato"])


def test_japanese_matching_needs_no_whitespace_boundaries():
    assert boundary_policy("ja") == "none"
    matcher = PhraseMatcher({"武士": "ぶし"}, boundary_policy("ja"))
    assert matcher.apply("武士は黙った") == ("ぶしは黙った", ["武士"])
    assert boundary_policy("es") == "word"


def test_empty_matcher_is_a_noop():
    assert PhraseMatcher({}).apply("text") == ("text", [])


# -- resolver: scope precedence and suppression ------------------------------


def test_scope_precedence_line_episode_show_personal_pack(tmp_path):
    db = make_db(tmp_path)
    for scope, ref, repl in [
        ("pack", "", "PACK"),
        ("personal", "", "PERSONAL"),
        ("show", "grp", "SHOW"),
        ("episode", "title-1", "EPISODE"),
        ("line", line_ref("title-1", 3), "LINE"),
    ]:
        entry = add_rule(db, scope=scope, scope_ref=ref)
        add_realization(db, entry, replacement=repl)
    sel = selection(db, title_ref="title-1", show_ref="grp")
    assert sel.spoken("Sora", engine="engA", line=line_ref("title-1", 3))[0] == "LINE"
    assert sel.spoken("Sora", engine="engA", line=line_ref("title-1", 4))[0] == "EPISODE"
    without_episode = selection(db, title_ref="other", show_ref="grp")
    assert without_episode.spoken("Sora", engine="engA")[0] == "SHOW"
    without_show = selection(db, title_ref="other", show_ref="")
    assert without_show.spoken("Sora", engine="engA")[0] == "PERSONAL"
    db.close()


def test_suppression_disables_an_inherited_rule(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry)
    add_rule(db, phrase="", scope="show", scope_ref="grp", suppresses=entry.id)
    suppressed = selection(db, show_ref="grp")
    assert suppressed.spoken("Sora", engine="engA") == ("Sora", [])
    unaffected = selection(db, show_ref="other")
    assert unaffected.spoken("Sora", engine="engA")[0] == "Zo-ra"
    db.close()


def test_exact_regional_rule_beats_base_language_rule(tmp_path):
    db = make_db(tmp_path)
    base = add_rule(db, locale="es")
    add_realization(db, base, replacement="BASE")
    regional = add_rule(db, locale="es-MX")
    add_realization(db, regional, replacement="MX")
    assert selection(db, locale="es-MX").spoken("Sora", engine="engA")[0] == "MX"
    assert selection(db, locale="es").spoken("Sora", engine="engA")[0] == "BASE"
    assert selection(db, locale="es-VE").spoken("Sora", engine="engA")[0] == "BASE"
    db.close()


def test_es419_applies_to_es_ve_only_with_declared_coverage(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, locale="es-419")
    add_realization(db, entry)
    assert selection(db, locale="es-VE").spoken("Sora", engine="engA") == ("Sora", [])
    db.execute("DELETE FROM knowledge_entries")
    covered = add_rule(db, locale="es-419", coverage=("es-VE",))
    add_realization(db, covered)
    assert selection(db, locale="es-VE").spoken("Sora", engine="engA")[0] == "Zo-ra"
    assert selection(db, locale="es-MX").spoken("Sora", engine="engA") == ("Sora", [])
    db.close()


def test_mexican_rules_never_leak_into_venezuelan(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, locale="es-MX")
    add_realization(db, entry)
    assert selection(db, locale="es-VE").spoken("Sora", engine="engA") == ("Sora", [])
    db.close()


@pytest.mark.parametrize(
    "status,scope,applies",
    [
        ("proposed", "personal", True),  # personal rules apply immediately
        ("reviewed", "personal", True),
        ("proposed", "pack", False),  # shared content needs recorded review
        ("reviewed", "pack", True),
        ("needs-retest", "personal", False),
        ("retired", "personal", False),
    ],
)
def test_activation_status_by_scope(tmp_path, status, scope, applies):
    db = make_db(tmp_path)
    entry = add_rule(
        db, scope=scope, status=status, origin="installed" if scope == "pack" else "local"
    )
    realization = add_realization(
        db,
        entry,
        origin="installed" if scope == "pack" else "local",
        status="reviewed" if scope == "pack" else status,
    )
    if scope == "pack":
        # direct construction: pack content resolves only via an active release
        # (covered in test_packs.py); here we test the activation rules themselves
        sel = KnowledgeSelection(entries=[entry], realizations=[realization], locale="es")
    else:
        sel = selection(db)
    spoken, _ = sel.spoken("Sora", engine="engA")
    assert (spoken == "Zo-ra") is applies
    db.close()


def test_equally_applicable_conflicts_are_surfaced_and_skipped(tmp_path):
    db = make_db(tmp_path)
    first = add_rule(db)
    add_realization(db, first, replacement="ONE")
    second = add_rule(db)  # same phrase, same scope: never decided by load order
    add_realization(db, second, replacement="TWO")
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA") == ("Sora", [])
    assert len(sel.conflicts) == 1
    assert sel.conflicts[0]["entries"] == sorted([first.id, second.id])
    db.close()


def test_ambiguous_senses_stay_suggestions(tmp_path):
    db = make_db(tmp_path)
    for sense, repl in [("health", "UNO"), ("declining", "DOS")]:
        entry = add_rule(db, sense=sense)
        add_realization(db, entry, replacement=repl)
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA") == ("Sora", [])
    assert sel.conflicts
    db.close()


# -- realizations ------------------------------------------------------------


def test_mixed_engine_cast_gets_per_engine_realizations(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry, replacement="FOR-A", engine="engA")
    add_realization(db, entry, replacement="FOR-B", engine="engB")
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA")[0] == "FOR-A"
    assert sel.spoken("Sora", engine="engB")[0] == "FOR-B"
    assert sel.spoken("Sora", engine="engC") == ("Sora", [])  # untested engine: unknown
    db.close()


def test_unknown_model_version_stays_unknown(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry, replacement="V2", model="model-v2")
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA", model=None) == ("Sora", [])
    assert sel.spoken("Sora", engine="engA", model="model-v2")[0] == "V2"
    assert sel.spoken("Sora", engine="engA", model="model-v1") == ("Sora", [])
    db.close()


def test_specific_realization_beats_deliberately_broad(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry, replacement="BROAD")
    add_realization(db, entry, replacement="V2-SPECIFIC", model="model-v2")
    add_realization(db, entry, replacement="VOICE-SPECIFIC", voice="voice-1")
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA", model="model-v2")[0] == "V2-SPECIFIC"
    assert sel.spoken("Sora", engine="engA", model="model-v1")[0] == "BROAD"
    assert sel.spoken("Sora", engine="engA", voice="voice-1")[0] == "VOICE-SPECIFIC"
    db.close()


def test_conflicting_realizations_contribute_nothing(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry, replacement="ONE")
    add_realization(db, entry, replacement="TWO")  # equally broad, different spelling
    sel = selection(db)
    assert sel.spoken("Sora", engine="engA") == ("Sora", [])
    assert any("realizations" in c["reason"] for c in sel.conflicts)
    db.close()


# -- legacy compatibility ----------------------------------------------------


def test_legacy_pronunciations_layer_below_scoped_entries(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, phrase="Sora", scope="episode", scope_ref="title-1")
    add_realization(db, entry, replacement="SCOPED")
    sel = selection(db, title_ref="title-1", legacy={"Sora": "LEGACY", "Tidekin": "Tai-doh"})
    spoken, applied = sel.spoken("Sora y Tidekin", engine="engA")
    assert spoken == "SCOPED y Tai-doh"
    assert {tuple(a)[0] for a in applied} == {"entry", "legacy"}
    # the legacy map still ranks above installed pack content
    pack = add_rule(db, phrase="Tidekin", scope="pack", origin="installed")
    add_realization(db, pack, replacement="PACK", origin="installed")
    sel = selection(db, legacy={"Tidekin": "Tai-doh"})
    assert sel.spoken("Tidekin", engine="engA")[0] == "Tai-doh"
    db.close()


# -- terminology -------------------------------------------------------------


def test_terminology_is_bounded_to_relevant_segments(tmp_path):
    db = make_db(tmp_path)
    add_rule(db, phrase="Sora", kind="term", source_form="ギンコ", status="reviewed")
    add_rule(db, phrase="tidekin", kind="term", source_form="潮", status="reviewed")
    sel = selection(db)
    assert sel.glossary_terms(["ギンコが歩く"]) == {"ギンコ": "Sora"}
    assert sel.glossary_terms(["unrelated text"]) == {}
    db.close()


def test_two_source_forms_may_share_one_rendering(tmp_path):
    db = make_db(tmp_path)
    for form in ("Cove of the Lantern", "Cove of the Lanterns"):
        add_rule(db, phrase="la Ensenada del Farol", kind="term", source_form=form,
                 status="reviewed")
    sel = selection(db)
    assert sel.glossary_terms(["From the Cove of the Lanterns!"]) == {
        "Cove of the Lantern": "la Ensenada del Farol",
        "Cove of the Lanterns": "la Ensenada del Farol"}
    assert sel.conflicts == []
    db.close()


def test_ambiguous_term_senses_are_not_applied(tmp_path):
    db = make_db(tmp_path)
    add_rule(
        db,
        phrase="Estoy bien",
        kind="term",
        source_form="I'm good",
        sense="health",
        status="reviewed",
    )
    add_rule(
        db,
        phrase="Gracias, no",
        kind="term",
        source_form="I'm good",
        sense="declining",
        status="reviewed",
    )
    sel = selection(db)
    assert sel.glossary_terms(["I'm good, thanks"]) == {}
    assert any("ambiguous" in c["reason"] for c in sel.conflicts)
    db.close()


def test_glossary_terms_carry_a_declared_register(tmp_path):
    db = make_db(tmp_path)
    add_rule(db, phrase="pana", kind="term", source_form="buddy", locale="es-VE",
             status="reviewed", register="colloquial")
    add_rule(db, phrase="Sora", kind="term", source_form="ギンコ", status="reviewed")
    sel = selection(db, locale="es-VE")
    assert sel.glossary_terms(["buddy and ギンコ walk out"]) == {
        "buddy": {"term": "pana", "register": "colloquial"},
        "ギンコ": "Sora",
    }
    db.close()


# -- storage and freezing ----------------------------------------------------


def test_revisions_accumulate_and_latest_wins(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, phrase="old")
    revised = save_entry(db, Entry(**{**entry.__dict__, "phrase": "new"}))
    assert revised.revision == 2
    from doblarr.knowledge import latest_entries

    latest = latest_entries(db)
    assert len(latest) == 1 and latest[0].phrase == "new" and latest[0].revision == 2
    db.close()


def test_frozen_snapshot_is_immune_to_later_edits(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db)
    realization = add_realization(db, entry, replacement="BEFORE")
    pinned = snapshot(db)
    save_entry(db, Entry(**{**entry.__dict__, "phrase": "Sora"}))
    save_realization(db, Realization(**{**realization.__dict__, "replacement": "AFTER"}))
    frozen = selection(db, pinned=pinned)
    assert frozen.spoken("Sora", engine="engA")[0] == "BEFORE"
    current = selection(db)
    assert current.spoken("Sora", engine="engA")[0] == "AFTER"
    db.close()


def test_missing_frozen_revision_is_surfaced_and_skipped(tmp_path, caplog):
    db = make_db(tmp_path)
    entry = add_rule(db)
    add_realization(db, entry)
    pinned = snapshot(db)
    db.execute("DELETE FROM knowledge_entries")
    import logging

    with caplog.at_level(logging.WARNING, logger="doblarr.knowledge"):
        sel = selection(db, pinned=pinned)
    assert entry.id in caplog.text
    assert sel.spoken("Sora", engine="engA") == ("Sora", [])
    db.close()


def test_v5_knowledge_tables_and_indexes_exist(tmp_path):
    db = make_db(tmp_path)
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knowledge_entries", "knowledge_realizations"} <= tables
    indexes = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_knowledge_entries_lookup" in indexes
    db.close()


def test_register_round_trips_through_storage(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, phrase="pana", kind="term", source_form="buddy",
                     locale="es-VE", register="colloquial")
    from doblarr.knowledge import latest_entries

    assert latest_entries(db)[0].register == "colloquial"
    revised = save_entry(db, Entry(**{**entry.__dict__, "register": None}))
    assert revised.revision == 2 and latest_entries(db)[0].register is None
    with pytest.raises(ValueError, match="register"):
        add_rule(db, phrase="x", register="street")
    db.close()


# -- synthesis / quality integration -----------------------------------------


def wav(path: Path, amplitude: int = 2000, duration: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(
            b"".join(
                struct.pack("<h", round(amplitude * math.sin(i * 0.2)))
                for i in range(round(16000 * duration))
            )
        )
    return path


class FakeVoicebox:
    def __init__(self):
        self.calls = []

    def synthesize_to_file(self, profile, text, language, dest, **kwargs):
        self.calls.append(text)
        wav(Path(dest))


def synth_job(tmp_path, texts):
    from doblarr.models import DubJob, Segment, Speaker

    src = tmp_path / "movie.mkv"
    src.write_bytes(b"video")
    job = DubJob(src, "ja", "es", target_locale="es-MX")
    job.source_audio = wav(tmp_path / "source.wav")
    job.speakers = {"S0": Speaker("S0", voicebox_profile_id="voice-0")}
    job.segments = [
        Segment(i, i * 2.0, i * 2.0 + 1.0, src_text, speaker="S0", text_translated=text)
        for i, (src_text, text) in enumerate(texts)
    ]
    return job


def test_synthesis_resolves_knowledge_and_records_applied_rules(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, locale="es-MX")
    add_realization(db, entry, replacement="Zo-ra")
    sel = selection(db, locale="es-MX")
    job = synth_job(tmp_path, [("x", "Sora habla"), ("y", "otra línea")])
    from doblarr.stages import synthesize

    vb = FakeVoicebox()
    synthesize.run(job, vb, tmp_path / "work", knowledge=sel, engine="engA")
    assert vb.calls == ["Zo-ra habla", "otra línea"]
    assert job.segments[0].tts_text == "Zo-ra habla"
    assert job.segments[0].applied_rules[0]["entry"] == entry.id
    assert job.segments[1].applied_rules == []
    receipt = json.loads(job.segments[0].audio_clip.with_suffix(".json").read_text())
    assert receipt["rules"][0]["entry"] == entry.id

    # quality checks the exact resolved spoken form, not the written text
    from doblarr.stages import quality

    quality.check_clip(job.segments[0], "es")
    receipt = json.loads(job.segments[0].audio_clip.with_suffix(".quality.json").read_text())
    assert receipt["request"]["text"] == "Zo-ra habla"
    db.close()


def test_unrelated_rule_edits_never_regenerate_clips(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, locale="es")
    realization = add_realization(db, entry, replacement="Zo-ra")
    job = synth_job(tmp_path, [("x", "Sora habla"), ("y", "otra línea")])
    from doblarr.stages import synthesize

    vb = FakeVoicebox()
    synthesize.run(job, vb, tmp_path / "work", knowledge=selection(db), engine="engA")
    assert len(vb.calls) == 2

    # an unrelated new rule leaves every clip untouched
    other = add_rule(db, phrase="Ausente", locale="es")
    add_realization(db, other, replacement="Ah-oo")
    synthesize.run(job, vb, tmp_path / "work", knowledge=selection(db), engine="engA")
    assert len(vb.calls) == 2

    # editing the rule that affected line 0 re-renders only line 0
    save_realization(db, Realization(**{**realization.__dict__, "replacement": "Ghin-ko"}))
    synthesize.run(job, vb, tmp_path / "work", knowledge=selection(db), engine="engA")
    assert vb.calls[-1] == "Ghin-ko habla"
    assert len(vb.calls) == 3
    db.close()


def test_mixed_engine_cast_synthesizes_per_speaker_engine(tmp_path):
    db = make_db(tmp_path)
    entry = add_rule(db, locale="es")
    add_realization(db, entry, replacement="FOR-A", engine="engA")
    add_realization(db, entry, replacement="FOR-B", engine="engB")
    from doblarr.models import Speaker

    job = synth_job(tmp_path, [("x", "Sora"), ("y", "Sora otra vez")])
    job.speakers["S1"] = Speaker("S1", voicebox_profile_id="voice-1")
    job.segments[1].speaker = "S1"
    cast = {
        "S0": {"voice": "voice-0", "engine": "engA"},
        "S1": {"voice": "voice-1", "engine": "engB"},
    }
    from doblarr.stages import synthesize

    vb = FakeVoicebox()
    synthesize.run(job, vb, tmp_path / "work", cast=cast, knowledge=selection(db))
    assert vb.calls == ["FOR-A", "FOR-B otra vez"]
    db.close()


# -- queue-time freezing and worker upgrades ----------------------------------


def test_queue_freezes_and_legacy_jobs_snapshot_on_first_run(client_factory, monkeypatch):
    captured = []

    def fake_run_job(dj, config, **kwargs):
        captured.append(dj)

    monkeypatch.setattr("doblarr.jobs.run_job", fake_run_job)
    client = client_factory()
    db = client.app.state.jobs.db
    entry = add_rule(db)
    add_realization(db, entry)

    response = client.post("/api/jobs", json={"title": "Film", "target_lang": "es"})
    queued = response.json()["job"]
    assert queued["knowledge_snapshot"]["entries"][entry.id] == 1

    # a later edit must not reach the frozen job
    save_entry(db, Entry(**{**entry.__dict__, "phrase": "Otro"}))
    # a legacy row without a snapshot gets one pinned at its first upgraded run
    legacy = client.app.state.jobs.add(title="Old", source="t", source_lang="ja", target_lang="es")
    client.app.state.jobs.update(legacy.id, knowledge_snapshot=None)
    worker = client.app.state.worker
    worker.start()
    try:
        deadline = time.time() + 5
        while len(captured) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        worker.stop()
        worker.join(timeout=5)
    by_title = {dj.input_file.name: dj for dj in captured}
    assert by_title["Film"].knowledge_snapshot["entries"][entry.id] == 1
    upgraded = by_title["Old"]
    assert upgraded.knowledge_snapshot["entries"][entry.id] == 2
    stored = client.app.state.jobs.get(legacy.id)
    assert stored.knowledge_snapshot == upgraded.knowledge_snapshot


def test_review_rerender_inherits_the_original_snapshot(client_factory, tmp_path):
    from tests.test_review import review_job

    client = client_factory()
    queued, _, _ = review_job(client, tmp_path)
    client.app.state.jobs.update(queued.id, knowledge_snapshot={"version": 1, "entries": {"e": 2}})
    response = client.post(
        f"/api/jobs/{queued.id}/review",
        json={"edits": [{"index": 0, "text": "buenas", "regenerate": True}]},
    )
    assert response.status_code == 200
    assert response.json()["job"]["knowledge_snapshot"] == {"version": 1, "entries": {"e": 2}}
