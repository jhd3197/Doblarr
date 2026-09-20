"""Voice cast tests — store migration + CRUD, assignment logic, cast/voices API."""

import ntpath
import posixpath
from types import SimpleNamespace

import pytest

from doblarr.jobs import JobStore
from doblarr.store import SCHEMA_VERSION, Database
from doblarr.voices import assign_default_cast, cast_key

API_KEY = "test-key"
HEADERS = {"X-Api-Key": API_KEY}


def test_migration_v2_voice_casts_and_job_kind(tmp_path):
    db = Database(tmp_path / "d.db")
    assert db.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION == 3
    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "voice_casts" in tables
    assert "title_plans" in tables
    job_cols = [r["name"] for r in db.query("PRAGMA table_info(jobs)")]
    assert "kind" in job_cols
    db.close()


def test_plan_crud_round_trip(tmp_path):
    db = Database(tmp_path / "d.db")
    assert db.load_plan("k1") is None
    plan = {"dub.voice_mode": "preset", "dub.dry_run": True}
    db.save_plan("k1", "Film A", plan)
    saved = db.load_plan("k1")
    assert saved["title"] == "Film A" and saved["plan"] == plan
    # upsert overwrites; clearing the plan keeps the row with no overrides
    db.save_plan("k1", "Film A", {})
    assert db.load_plan("k1")["plan"] == {}
    db.close()


def test_cast_crud_round_trip(tmp_path):
    db = Database(tmp_path / "d.db")
    assert db.load_cast("k1") is None
    cast = [{"speaker_id": "SPEAKER_00", "label": "Adult M 1", "category": "adult_m",
             "voice": "v1", "previewed": False}]
    db.save_cast("k1", "Film A", cast)
    saved = db.load_cast("k1")
    assert saved["title"] == "Film A" and saved["cast"] == cast
    # upsert overwrites
    db.save_cast("k1", "Film A", cast + [{"speaker_id": "SPEAKER_01",
             "label": "Adult F 1", "category": "adult_f", "voice": "", "previewed": False}])
    assert len(db.load_cast("k1")["cast"]) == 2
    db.close()


def test_cast_key_priority():
    assert cast_key(title="Film", tmdb_id=42) == "tmdb:42"
    assert cast_key(title="Film", tvdb_id=7) == "tvdb:7"
    assert cast_key(title="  My Film ") == "title:my film"


@pytest.mark.parametrize(("path_module", "path", "expected"), [
    (ntpath, "C:\\Media\\Film.mkv", "c:\\media\\film.mkv"),
    (posixpath, "/Media/Film.mkv", "/Media/Film.mkv"),
])
def test_cast_key_respects_platform_path_case(monkeypatch, path_module, path, expected):
    # Windows folds case; POSIX must preserve distinct, case-sensitive filenames.
    monkeypatch.setattr("doblarr.voices.os", SimpleNamespace(path=path_module))
    assert cast_key(path=path, tmdb_id=42, title="Film") == expected


def test_assign_default_cast_numbering():
    single = assign_default_cast(["SPEAKER_00"])
    assert single == [{"speaker_id": "SPEAKER_00", "label": "Narrator",
                       "category": "narrator", "voice": "", "previewed": False}]
    multi = assign_default_cast(["S0", "S1", "S2", "S3"])
    assert [(e["label"], e["category"]) for e in multi] == [
        ("Adult M 1", "adult_m"), ("Adult F 1", "adult_f"),
        ("Adult M 2", "adult_m"), ("Adult F 2", "adult_f"),
    ]


def test_assign_default_cast_merges_existing():
    existing = [{"speaker_id": "S0", "label": "Adult M 1", "category": "adult_m",
                 "voice": "custom-voice", "previewed": True}]
    merged = assign_default_cast(["S0", "S1"], existing=existing)
    assert merged[0]["voice"] == "custom-voice"      # user choice preserved
    assert merged[1]["label"] == "Adult F 1"          # new speaker numbered fresh
    again = assign_default_cast(["S0", "S1", "S2"], existing=merged)
    assert again[2]["label"] == "Adult M 2"           # numbering continues
    assert len(again) == 3                            # nothing dropped


@pytest.fixture
def cast_client(client_factory):
    return client_factory({"web": {"api_key": API_KEY},
                           "dub": {"preset_voices": ["narrator-deep"]}})


def test_cast_api_round_trip(cast_client):
    c = cast_client
    r = c.get("/api/cast", headers=HEADERS, params={"key": "title:film a"})
    assert r.json() == {"key": "title:film a", "title": "", "cast": []}
    body = {"key": "title:film a", "title": "Film A", "cast": [
        {"speaker_id": "SPEAKER_00", "label": "Adult M 1", "category": "adult_m",
         "voice": "v1", "previewed": False}]}
    assert c.put("/api/cast", headers=HEADERS, json=body).json()["saved"] == 1
    got = c.get("/api/cast", headers=HEADERS, params={"key": "title:film a"}).json()
    assert got["cast"][0]["label"] == "Adult M 1"
    # raw identifiers are accepted too — the server computes the key
    by_title = c.get("/api/cast", headers=HEADERS, params={"title": "Film A"}).json()
    assert by_title["key"] == "title:film a" and by_title["cast"]
    assert c.get("/api/cast", headers=HEADERS).status_code == 400


def test_plan_api_round_trip(cast_client):
    c = cast_client
    r = c.get("/api/plan", headers=HEADERS, params={"key": "title:film a"})
    assert r.json() == {"key": "title:film a", "title": "", "plan": {}}
    body = {"key": "title:film a", "title": "Film A",
            "plan": {"dub.voice_mode": "preset", "target_lang": "es"}}
    assert c.put("/api/plan", headers=HEADERS, json=body).json()["overrides"] == 2
    got = c.get("/api/plan", headers=HEADERS, params={"key": "title:film a"}).json()
    assert got["plan"]["dub.voice_mode"] == "preset"
    # raw identifiers are accepted too — the server computes the key
    by_title = c.get("/api/plan", headers=HEADERS, params={"title": "Film A"}).json()
    assert by_title["key"] == "title:film a" and by_title["plan"]
    assert c.get("/api/plan", headers=HEADERS).status_code == 400


def test_job_overrides_ride_the_payload(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    j = store.add(title="T", source="t", source_lang="ko", target_lang="en",
                  overrides={"dub": {"voice_mode": "preset"}})
    assert store.get(j.id).overrides == {"dub": {"voice_mode": "preset"}}
    assert store.list()[0]["overrides"] == {"dub": {"voice_mode": "preset"}}
    j2 = store.add(title="F", source="t", source_lang="ko", target_lang="en")
    assert store.get(j2.id).overrides is None
    store.close()


def test_cast_api_computes_key_from_path(cast_client):
    c = cast_client
    put = c.put("/api/cast", headers=HEADERS, json={
        "path": "C:/Media/Film.mkv", "title": "Film", "cast": [
            {"speaker_id": "S0", "label": "Narrator", "category": "narrator"}]})
    key = put.json()["key"]
    assert key == cast_key(path="C:/Media/Film.mkv")
    got = c.get("/api/cast", headers=HEADERS, params={"path": "C:/Media/Film.mkv"})
    assert got.json()["cast"][0]["label"] == "Narrator"


def test_cast_api_validation(cast_client):
    bad = {"key": "k", "cast": [{"speaker_id": "S0", "label": "X",
                                 "category": "robot", "voice": ""}]}
    assert cast_client.put("/api/cast", headers=HEADERS, json=bad).status_code == 422
    assert cast_client.get("/api/cast", params={"key": "k"}).status_code == 401


def test_voices_endpoint_falls_back_to_config(cast_client):
    # voicebox is not running in tests -> config preset fallback
    r = cast_client.get("/api/voices", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "config"
    assert body["voices"] == [{"id": "narrator-deep", "name": "narrator-deep"}]
    assert "warning" in body


def test_voices_endpoint_from_voicebox(cast_client, monkeypatch):
    def fake(self):
        return [{"id": "p1", "name": "Deep Narrator"}]
    monkeypatch.setattr("doblarr.clients.voicebox.VoiceboxClient.list_voices", fake)
    body = cast_client.get("/api/voices", headers=HEADERS).json()
    assert body["source"] == "voicebox" and body["voices"][0]["name"] == "Deep Narrator"


def test_job_kind_column_round_trip(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    j = store.add(title="T", source="t", source_lang="ko", target_lang="en",
                  kind="tease")
    assert store.get(j.id).kind == "tease"
    assert store.list()[0]["kind"] == "tease"
    # existing rows default to full
    j2 = store.add(title="F", source="t", source_lang="ko", target_lang="en")
    assert store.get(j2.id).kind == "full"
    store.close()
