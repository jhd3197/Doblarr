"""Portable recipe contract and import side-effect boundaries."""

import copy

import pytest

from doblarr.recipes import DubRecipe


@pytest.fixture
def client(client_factory):
    return client_factory({})


def export_body():
    return {
        "identity": {"path": "/private/movie.mkv"},
        "media": {"kind": "movie", "title": "Test Film", "tmdb_id": 42},
        "target_language": "es",
    }


def exported(client):
    response = client.post("/api/recipes/export", json=export_body())
    assert response.status_code == 200, response.text
    return response.json()["recipe"]


def test_recipe_export_contains_only_portable_settings(client):
    client.put(
        "/api/plan",
        json={
            "path": "/private/movie.mkv",
            "plan": {
                "paths.output_dir": "/private/output",
                "translate.endpoint": "https://secret.example",
                "dub.line_edits": {"0": {"text": "copyright dialogue"}},
                "dub.narrator_voice": "private-profile",
                "dub.dry_run": False,
                "dub.pronunciations": {"Ginko": "Gheen-ko"},
                "target_lang": "es",
            },
        },
    )
    recipe = exported(client)
    text = str(recipe)
    for excluded in [
        "/private",
        "secret.example",
        "copyright dialogue",
        "private-profile",
        "dub.dry_run",
    ]:
        assert excluded not in text
    assert recipe["settings"]["dub.pronunciations"] == {"Ginko": "Gheen-ko"}
    assert recipe["target_language"] == "es"
    DubRecipe.model_validate(recipe)
    assert client.get("/api/recipes/schema").status_code == 200


def test_preview_is_read_only_and_import_is_explicit(client):
    recipe = exported(client)
    body = {
        "identity": {"path": "/local/movie.mkv"},
        "media": export_body()["media"],
        "recipe": recipe,
    }
    assert client.post("/api/recipes/preview", json=body).status_code == 200
    assert client.get("/api/plan", params=body["identity"]).json()["plan"] == {}
    assert client.post("/api/recipes/import", json=body).status_code == 422
    body["voices"] = {"narrator": "missing"}
    assert client.post("/api/recipes/import", json=body).status_code == 422
    body["voices"] = {"narrator": ""}
    jobs_before = client.get("/api/jobs").json()
    assert client.post("/api/recipes/import", json=body).status_code == 200
    plan = client.get("/api/plan", params=body["identity"]).json()["plan"]
    assert plan["target_lang"] == "es"
    assert "dub.dry_run" not in plan
    assert client.get("/api/jobs").json() == jobs_before


def test_wrong_episode_rejected_and_no_partial_save(client):
    recipe = exported(client)
    recipe["media"] = {"kind": "episode", "title": "Show", "tvdb_id": 9, "season": 1, "episode": 1}
    body = {
        "identity": {"key": "episode:9:2"},
        "media": {**recipe["media"], "episode": 2},
        "recipe": recipe,
        "voices": {"narrator": ""},
    }
    assert client.post("/api/recipes/import", json=body).status_code == 422
    assert client.get("/api/plan", params=body["identity"]).json()["plan"] == {}


@pytest.mark.parametrize(
    "change",
    [
        {"audio": "base64 data"},
        {"schema_version": 99},
        {"mode": "dialogue"},
        {"settings": {"paths.output_dir": "/tmp"}},
        {"settings": {"dub.duration_match": "false"}},
        {"settings": {"translate.glossary": {"name": {"audio": "payload"}}}},
    ],
)
def test_recipe_rejects_unknown_versions_media_and_unsafe_settings(client, change):
    recipe = {**exported(client), **change}
    body = {"identity": {"tmdb_id": 42}, "media": export_body()["media"], "recipe": recipe}
    assert client.post("/api/recipes/preview", json=body).status_code == 422


def test_cast_import_maps_local_voices_and_preserves_dry_run(client):
    recipe = exported(client)
    recipe["characters"] = [
        {
            "speaker_id": "S0",
            "label": "Elder",
            "category": "elderly_m",
            "voice": {
                "name": "Unavailable elder",
                "engine": "qwen",
                "delivery": "Measured and calm",
            },
        }
    ]
    identity = {"tmdb_id": 42}
    client.put(
        "/api/plan",
        json={**identity, "plan": {"dub.dry_run": True, "dub.line_edits": {"0": {"text": "old"}}}},
    )
    body = {
        "identity": identity,
        "media": export_body()["media"],
        "recipe": recipe,
        "voices": {"narrator": "", "character:S0": ""},
    }
    assert client.post("/api/recipes/import", json=body).status_code == 200
    cast = client.get("/api/cast", params=identity).json()["cast"]
    assert cast[0]["label"] == "Elder" and cast[0]["voice"] == ""
    assert cast[0]["engine"] == "qwen"
    plan = client.get("/api/plan", params=identity).json()["plan"]
    assert plan["dub.dry_run"] is True and plan["dub.line_edits"] == {}
    bad = copy.deepcopy(body)
    bad["recipe"]["characters"].append(bad["recipe"]["characters"][0])
    assert client.post("/api/recipes/import", json=bad).status_code == 422


def test_missing_preset_voice_requires_a_cloning_engine(client):
    recipe = exported(client)
    recipe["narrator"] = {"name": "Preset elder", "engine": "kokoro", "delivery": ""}
    body = {
        "identity": {"tmdb_id": 42},
        "media": export_body()["media"],
        "recipe": recipe,
        "voices": {"narrator": ""},
    }
    assert client.post("/api/recipes/import", json=body).status_code == 422
    body["engines"] = {"narrator": "qwen"}
    result = client.post("/api/recipes/import", json=body)
    assert result.status_code == 200
    assert result.json()["plan"]["voicebox.default_engine"] == "qwen"


def test_recipe_write_rolls_back_if_cast_write_fails(tmp_path):
    import sqlite3

    from doblarr.store import Database

    db = Database(tmp_path / "atomic.db")
    db.save_plan("movie", "Film", {"target_lang": "en"})
    db.execute(
        "CREATE TRIGGER fail_cast BEFORE INSERT ON voice_casts "
        "BEGIN SELECT RAISE(ABORT, 'simulated cast failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.save_recipe("movie", "Film", {"target_lang": "es"}, [])
    assert db.load_plan("movie")["plan"] == {"target_lang": "en"}
    db.close()
