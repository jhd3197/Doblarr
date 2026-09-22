"""SQLite store tests — migrations, legacy import, scan state, payload fields."""

import json

from doblarr.jobs import JobStore, import_legacy_json
from doblarr.store import SCHEMA_VERSION, Database


def test_schema_migrations_run_once(tmp_path):
    db = Database(tmp_path / "d.db")
    version = db.query_one("PRAGMA user_version")[0]
    assert version == SCHEMA_VERSION
    db.close()
    # reopening an up-to-date db applies nothing and keeps the version
    db = Database(tmp_path / "d.db")
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION
    db.close()


def test_migration_applies_to_old_schema(tmp_path, monkeypatch):
    db = Database(tmp_path / "d.db")
    db.close()

    def _v2_add_column(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN retries INTEGER NOT NULL DEFAULT 0")

    import doblarr.store as store_mod
    monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS + [_v2_add_column])
    db = Database(tmp_path / "d.db")  # opens the v1 db, applies v2
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION + 1
    cols = [r["name"] for r in db.query("PRAGMA table_info(jobs)")]
    assert "retries" in cols
    db.close()


def test_legacy_jobs_json_import(tmp_path):
    legacy = tmp_path / "jobs.json"
    legacy.write_text(json.dumps([
        {"id": "old1", "title": "Godzilla", "source": "Radarr", "source_lang": "ja",
         "target_lang": "en", "input_file": None, "status": "done", "stage": "mux",
         "progress": 100, "message": "dubbed", "created_at": "2024-01-01T00:00:00",
         "updated_at": "2024-01-01T01:00:00"},
        {"id": "old2", "title": "Ran", "source": "manual", "source_lang": "ja",
         "target_lang": "es", "input_file": None, "status": "queued", "stage": "",
         "progress": 0, "message": "", "created_at": "2024-01-02T00:00:00",
         "updated_at": "2024-01-02T00:00:00"},
    ]), encoding="utf-8")
    store = JobStore(tmp_path / "jobs.db")
    assert import_legacy_json(store, legacy) == 2
    assert not legacy.exists()
    assert legacy.with_suffix(".json.migrated").exists()  # renamed, not deleted
    old1 = store.get("old1")  # ids and timestamps preserved
    assert old1.status == "done" and old1.created_at == "2024-01-01T00:00:00"
    assert store.next_queued().id == "old2"
    # a second import attempt is a no-op (queue not empty)
    legacy.write_text("[]", encoding="utf-8")
    assert import_legacy_json(store, legacy) == 0
    store.close()


def test_reset_running_requeues_interrupted_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    j = store.add(title="Mid-run", source="t", source_lang="ko", target_lang="en")
    store.update(j.id, status="running", stage="mix")
    store.close()
    store = JobStore(tmp_path / "jobs.db")  # simulates a restart
    assert store.get(j.id).status == "queued"
    store.close()


def test_extra_fields_round_trip_via_payload(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    j = store.add(title="T", source="t", source_lang="ko", target_lang="en",
                  force=True)
    assert j.force is True
    assert store.get(j.id).force is True     # persisted in the payload column
    store.update(j.id, force=False)
    assert store.get(j.id).force is False
    store.close()


def test_saved_generation_metrics_do_not_break_job_listing(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.add(title="Saved dub", source="plex", source_lang="en", target_lang="es")
    store.update(job.id, metrics={"tts_cache_hits": 275, "timing_flags": 13})
    assert store.get(job.id).metrics["tts_cache_hits"] == 275
    assert store.list()[0]["metrics"]["timing_flags"] == 13
    store.close()


def test_scan_state_round_trip(tmp_path):
    db = Database(tmp_path / "d.db")
    assert db.load_scan() is None
    db.save_scan("2024-01-01T00:00:00", {"total": 3, "needs_dub": 1},
                 [{"title": "A", "year": 2020}])
    db.save_scan("2024-01-02T00:00:00", {"total": 4, "needs_dub": 0}, [])
    saved = db.load_scan()
    assert saved["last_scan"] == "2024-01-02T00:00:00"  # second save overwrites
    assert saved["counts"]["total"] == 4
    assert saved["items"] == []
    db.close()
