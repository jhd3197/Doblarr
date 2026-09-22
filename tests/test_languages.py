"""Language catalog tests — canonicalization, aliases, resolver, and migration wiring."""

import json
import logging
import time
from pathlib import Path

import pytest
import yaml

from doblarr import discovery
from doblarr.config import Config
from doblarr.languages import (
    base_language,
    catalog,
    display_name,
    get,
    is_supported,
    iso3_for,
    native_name,
    normalize,
    parse,
    resolve_target_locale,
    supported_locales,
)


def test_parse_canonicalizes_case_and_accepts_numeric_regions():
    assert parse("es-mx") == "es-MX"
    assert parse("ES") == "es"
    assert parse("es-419") == "es-419"
    assert parse("zh-hant-tw") == "zh-Hant-TW"
    assert parse("jpn") == "jpn"  # three-letter languages stay whole


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "e",
        "es-",
        "es-MXX",
        "123",
        "es--MX",
        "english",
        "es-MX-VE",
    ],
)
def test_parse_rejects_malformed_tags(bad):
    assert parse(bad) is None


def test_normalize_resolves_codes_and_english_names():
    assert normalize("eng") == "en"
    assert normalize("spa") == "es"
    assert normalize("jpn") == "ja"
    assert normalize("ger") == "de"
    assert normalize("deu") == "de"
    assert normalize("fre") == "fr"
    assert normalize("fra") == "fr"
    assert normalize("Spanish") == "es"
    assert normalize("flemish") == "nl"
    assert normalize("mandarin") == "zh"
    assert normalize("es-mx") == "es-MX"


def test_valid_but_uncatalogued_tags_keep_their_identity():
    assert normalize("fr-CA") == "fr-CA"
    assert get("fr-CA") is None
    assert is_supported("fr-CA") is False
    assert normalize("xx") == "xx"  # never truncated or guessed
    assert normalize("!!!") is None


def test_display_and_media_mappings():
    assert display_name("es-MX") == "Spanish — Mexico"
    assert display_name("es-419") == "Spanish — Latin America and the Caribbean"
    assert display_name("fr-CA") == "FR-CA"  # uncatalogued falls back to the code
    assert native_name("es") == "español"
    assert iso3_for("es-MX") == "spa"  # media tags use the base language
    assert iso3_for("es-VE") == "spa"
    assert iso3_for("de") == "ger"
    assert iso3_for("xx") == "xx"  # raw-code fallback, as before
    assert base_language("es-419") == "es"


def test_catalog_covers_targets_and_discovery_languages():
    ids = {e.id for e in catalog()}
    assert {"en", "es", "es-MX", "es-VE", "es-419", "es-ES", "ja"} <= ids
    assert {"de", "fr", "it", "pt", "ko", "zh", "ru", "hi", "ar", "nl", "sv"} <= ids
    assert [e.id for e in supported_locales("es")] == ["es", "es-MX", "es-VE", "es-419", "es-ES"]


def test_spanish_direction_texts_match_the_legacy_translator_wording():
    assert get("es-419").direction.startswith("Neutral Latin American Spanish for studio dubbing.")
    assert get("es-MX").direction.startswith("Mexican Spanish for studio dubbing.")
    assert get("es-ES").direction == (
        "Spanish from Spain with consistent regional vocabulary and forms of address."
    )
    assert "Venezuelan Spanish" in get("es-VE").direction
    assert get("es").direction == ""


def test_discovery_maps_delegate_to_the_catalog():
    assert discovery.NAME_TO_ISO2["spanish"] == "es"
    assert discovery.NAME_TO_ISO2["flemish"] == "nl"
    assert discovery.ISO3_TO_ISO2["jpn"] == "ja"
    assert discovery.ISO3_TO_ISO2["deu"] == "de"
    assert discovery.lang_name("es") == "Spanish"
    assert discovery.lang_name("xx") == "XX"
    assert discovery.lang_name("es-MX") == "Spanish — Mexico"


def test_mux_lang3_comes_from_the_catalog():
    from doblarr.stages import mux

    assert mux._LANG3["fr"] == "fre"
    assert mux._LANG3["zh"] == "chi"


def test_resolver_prefers_explicit_matching_locale():
    assert resolve_target_locale({"dub": {"target_locale": "es-MX"}}, "es") == "es-MX"
    assert resolve_target_locale({"dub": {"target_locale": "es-mx"}}, "es") == "es-MX"
    assert (
        resolve_target_locale(
            {"dub": {"target_locale": "es-VE"}, "translate": {"locale": "es-419"}}, "es"
        )
        == "es-VE"
    )


def test_resolver_never_applies_a_locale_to_another_language(caplog):
    with caplog.at_level(logging.WARNING, logger="doblarr.languages"):
        assert resolve_target_locale({"dub": {"target_locale": "es-MX"}}, "en") == "en"
    assert "es-MX" in caplog.text
    assert resolve_target_locale({"translate": {"locale": "es-419"}}, "en") == "en"


def test_resolver_uses_legacy_translate_locale_only_for_spanish():
    assert resolve_target_locale({"translate": {"locale": "es-419"}}, "es") == "es-419"
    assert resolve_target_locale({"translate": {"locale": "auto"}}, "es") == "es"
    assert resolve_target_locale({}, "es") == "es"


def test_resolver_keeps_a_regional_target_language():
    assert resolve_target_locale({"dub": {"target_locale": "es-VE"}}, "es-MX") == "es-MX"
    assert resolve_target_locale({}, "es-419") == "es-419"


def _write_config(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_config_load_migrates_spanish_legacy_locale(tmp_path):
    path = tmp_path / "config.yaml"
    _write_config(
        path,
        {
            "general": {"target_languages": ["es"]},
            "translate": {"locale": "es-419"},
        },
    )
    cfg = Config.load(path)
    assert cfg["dub"]["target_locale"] == "es-419"
    assert cfg["translate"]["locale"] == "es-419"  # legacy key keeps working
    # re-checked on save: the migration survives a settings round-trip
    cfg.apply_and_save({"dub": {"dry_run": False}})
    assert cfg["dub"]["target_locale"] == "es-419"


def test_config_load_does_not_apply_spanish_locale_to_other_languages(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    _write_config(
        path,
        {
            "general": {"target_languages": ["en", "es"]},
            "translate": {"locale": "es-MX"},
        },
    )
    with caplog.at_level(logging.WARNING, logger="doblarr.config"):
        cfg = Config.load(path)
    assert cfg["dub"]["target_locale"] == ""
    assert "es-MX" in caplog.text


def test_config_load_never_overwrites_an_explicit_target_locale(tmp_path):
    path = tmp_path / "config.yaml"
    _write_config(
        path,
        {
            "general": {"target_languages": ["es"]},
            "translate": {"locale": "es-419"},
            "dub": {"target_locale": "es-VE"},
        },
    )
    assert Config.load(path)["dub"]["target_locale"] == "es-VE"


def test_config_warns_on_a_malformed_target_locale(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    _write_config(path, {"dub": {"target_locale": "not a locale"}})
    with caplog.at_level(logging.WARNING, logger="doblarr.config"):
        cfg = Config.load(path)
    assert "not a locale" in caplog.text
    assert cfg["dub"]["target_locale"] == "not a locale"  # advisory only, never fatal


def test_v4_migration_upgrades_a_v3_database(tmp_path):
    import doblarr.store as store_mod
    from doblarr.store import SCHEMA_VERSION, Database

    v3_path = tmp_path / "old.db"
    original = store_mod.MIGRATIONS
    try:
        store_mod.MIGRATIONS = original[:3]  # build a database at the v3 schema
        db = Database(v3_path)
        db.execute(
            "INSERT INTO jobs (id, title, source, source_lang, target_lang, created_at,"
            " updated_at) VALUES ('j1', 'Film', 'manual', 'ja', 'es', '2024-01-01', '2024-01-01')"
        )
        db.close()
    finally:
        store_mod.MIGRATIONS = original
    db = Database(v3_path)
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    cols = [r["name"] for r in db.query("PRAGMA table_info(jobs)")]
    assert "target_locale" in cols
    assert db.query_one("SELECT target_locale FROM jobs WHERE id = 'j1'")["target_locale"] == ""
    db.close()

    from doblarr.jobs import JobStore

    store = JobStore(v3_path)
    legacy = store.get("j1")
    assert legacy.target_lang == "es" and legacy.target_locale == ""
    store.close()


def test_job_target_locale_round_trips_as_a_column(tmp_path):
    from doblarr.jobs import JobStore

    store = JobStore(tmp_path / "jobs.db")
    job = store.add(
        title="Film", source="t", source_lang="ja", target_lang="es", target_locale="es-MX"
    )
    assert store.get(job.id).target_locale == "es-MX"
    store.update(job.id, target_locale="es-VE")
    assert store.get(job.id).target_locale == "es-VE"
    store.close()


def _captured_run(captured):
    def fake(dj, config, **kwargs):
        captured.append(dj)

    return fake


def test_worker_resolves_target_locale_at_execution_time(tmp_path, monkeypatch):
    from doblarr.events import EventBus
    from doblarr.jobs import JobStore, Worker

    captured = []
    monkeypatch.setattr("doblarr.jobs.run_job", _captured_run(captured))
    store = JobStore(tmp_path / "jobs.db")
    config = Config.load(tmp_path / "config.yaml").with_overrides(
        {
            "paths.work_dir": str(tmp_path / "work"),
            "translate.locale": "es-419",
        }
    )
    worker = Worker(store, config, events=EventBus())
    store.add(title="Legacy", source="t", source_lang="ja", target_lang="es")
    store.add(
        title="Explicit", source="t", source_lang="ja", target_lang="es", target_locale="es-MX"
    )
    worker.start()
    try:
        deadline = time.time() + 5
        while len(captured) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        worker.stop()
        worker.join(timeout=5)
        store.close()
    by_title = {dj.input_file.name: dj for dj in captured}
    assert by_title["Legacy"].target_locale == "es-419"  # legacy row derives from config
    assert by_title["Explicit"].target_locale == "es-MX"  # stored locale wins


def test_pipeline_accepts_numeric_regions_and_rejects_malformed_targets(tmp_path):
    import threading

    from doblarr.models import DubJob
    from doblarr.pipeline import run_job

    config = Config.load(tmp_path / "config.yaml").with_overrides(
        {
            "paths.work_dir": str(tmp_path / "work"),
            "paths.output_dir": str(tmp_path / "output"),
        }
    )
    with pytest.raises(ValueError, match="language code"):
        run_job(DubJob(Path("movie.mkv"), "ja", "123"), config, dry_run=True)

    cancel = threading.Event()
    seen = []

    def on_stage(name, i, total):
        seen.append(name)
        cancel.set()  # validation passed; stop before doing real work

    from doblarr.errors import JobCancelled

    job = DubJob(Path("movie.mkv"), "ja", "es-419")
    with pytest.raises(JobCancelled):
        run_job(job, config, dry_run=True, on_stage=on_stage, cancel_event=cancel)
    assert seen == ["probe"]
    assert job.target_lang == "es"  # engines stay on the base language
    assert job.target_locale == "es-419"


def test_regional_target_gets_its_own_work_namespace(tmp_path):
    import threading

    from doblarr.errors import JobCancelled
    from doblarr.models import DubJob
    from doblarr.pipeline import run_job

    config = Config.load(tmp_path / "config.yaml").with_overrides(
        {
            "paths.work_dir": str(tmp_path / "work"),
            "paths.output_dir": str(tmp_path / "output"),
        }
    )
    cancel = threading.Event()

    def on_stage(name, i, total):
        cancel.set()

    job = DubJob(Path("movie.mkv"), "ja", "es", target_locale="es-MX")
    with pytest.raises(JobCancelled):
        run_job(job, config, dry_run=True, on_stage=on_stage, cancel_event=cancel)
    assert job.artifacts_dir.name == "es-MX"


def test_translator_direction_uses_the_resolved_locale():
    from doblarr.clients.translator import translation_direction

    assert "Venezuelan Spanish" in translation_direction({"locale": "es-VE"}, "es")
    assert "Venezuelan Spanish" not in translation_direction({"locale": "es-VE"}, "fr")
    assert "Mexican Spanish" in translation_direction({"locale": "es-MX"}, "es-MX")
    assert translation_direction({"locale": "auto"}, "es") == translation_direction({}, "es")


def test_review_telemetry_and_versions_record_the_locale(tmp_path):
    from doblarr.models import DubJob, Segment, Speaker
    from doblarr.review import write_review
    from doblarr.telemetry import RunReport
    from doblarr.versions import preserve_version

    job = DubJob(tmp_path / "movie.mkv", "en", "es", target_locale="es-MX")
    job.segments = [Segment(0, 1, 2, "Hello", text_translated="Hola", speaker="GINKO")]
    job.speakers = {"GINKO": Speaker("GINKO")}
    report = RunReport(job, tmp_path)
    assert report.data["language"] == "es"
    assert report.data["locale"] == "es-MX"
    write_review(job, tmp_path)
    snapshot = json.loads(job.review_file.read_text(encoding="utf-8"))
    assert snapshot["language"] == "es"
    assert snapshot["locale"] == "es-MX"

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source video")
    output = tmp_path / "out" / "dub.mkv"
    output.parent.mkdir()
    output.write_bytes(b"rendered dub")
    job.output_file = output
    job.input_file = source
    config = Config.load(tmp_path / "config.yaml")
    manifest = preserve_version(job, config)
    assert manifest["script"]["target_language"] == "es"
    assert manifest["script"]["target_locale"] == "es-MX"


def test_mux_titles_and_tags_use_the_locale(tmp_path, caplog):
    from doblarr.models import DubJob
    from doblarr.stages import mux

    src = tmp_path / "movie.mkv"
    src.write_bytes(b"video")
    job = DubJob(src, "ja", "es", target_locale="es-MX", dubbed_track=tmp_path / "dub.wav")
    with caplog.at_level(logging.INFO, logger="doblarr.mux"):
        assert mux.run(job, tmp_path / "out", dry_run=True) is None  # dry plan is logged
    assert "title=Spanish — Mexico AI" in caplog.text
    assert "language=spa" in caplog.text


def test_series_routes_accept_and_deduplicate_regional_locales(client_factory, tmp_path):
    from tests.test_series import setup_series

    client = client_factory()
    first, second = setup_series(client, tmp_path)
    store = client.app.state.jobs

    assert client.get("/api/series/79214/episodes?target_lang=es-419").status_code == 200
    assert client.get("/api/series/79214/episodes?target_lang=es-VE").status_code == 200
    canonical = client.get("/api/series/79214/episodes?target_lang=es-mx").json()
    assert canonical["target_lang"] == "es-MX"
    assert client.get("/api/series/79214/episodes?target_lang=es-MXX").status_code == 422

    response = client.post(
        "/api/series/79214/queue",
        json={"episode_ids": [1, 2], "target_lang": "es-mx", "missing_only": False},
    )
    assert response.status_code == 200
    assert len(response.json()["queued"]) == 2
    jobs = store.list()
    assert all(j["target_lang"] == "es" and j["target_locale"] == "es-MX" for j in jobs)

    again = client.post(
        "/api/series/79214/queue",
        json={"episode_ids": [1, 2], "target_lang": "es-MX", "missing_only": False},
    )
    assert again.json()["queued"] == []  # same locale, different case: no double-queue

    other = client.post(
        "/api/series/79214/queue",
        json={"episode_ids": [1], "target_lang": "es-VE", "missing_only": False},
    )
    assert len(other.json()["queued"]) == 1  # a different locale is a different dub


def test_manual_job_route_splits_regional_targets(client_factory):
    client = client_factory()
    response = client.post("/api/jobs", json={"title": "Film", "target_lang": "es-ve"})
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["target_lang"] == "es"
    assert job["target_locale"] == "es-VE"


def test_languages_api_serves_the_catalog(client_factory):
    client = client_factory()
    response = client.get("/api/languages")
    assert response.status_code == 200
    entries = {e["id"]: e for e in response.json()["languages"]}
    assert entries["es-MX"]["base"] == "es"
    assert entries["es-MX"]["name"] == "Spanish — Mexico"
    assert entries["es-VE"]["region"] == "VE"
    assert entries["es-419"]["supported"] is True
    assert "direction" not in entries["es-MX"]  # prompt text stays server-side
