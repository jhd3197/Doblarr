"""Recipe schema v2 + the phase-3 acceptance flow: create, reuse, export, import."""

import pytest

from doblarr.knowledge import KnowledgeSelection
from doblarr.recipes import DubRecipe
from doblarr.voices import cast_key


@pytest.fixture
def client_factory(client_factory):
    """Starter auto-install stays off here so counts and overlays stay exact."""

    def make(data=None):
        return client_factory({"knowledge.auto_install_starter": False, **(data or {})})

    return make

EP1 = "/shows/Mushi-Shi/ep1.mkv"
EP2 = "/shows/Mushi-Shi/ep2.mkv"
MEDIA = {"kind": "episode", "title": "Mushi-Shi", "tvdb_id": 79214, "season": 1, "episode": 1}


def make_correction(
    client,
    *,
    scope,
    scope_ref="",
    phrase="Ginko",
    replacement="Guin-ko",
    locale="es-MX",
    status="reviewed",
):
    entry = client.post(
        "/api/knowledge/entries",
        json={
            "phrase": phrase,
            "kind": "pronunciation",
            "locale": locale,
            "scope": scope,
            "scope_ref": scope_ref,
            "status": status,
        },
    ).json()["entry"]
    client.post(
        "/api/knowledge/realizations",
        json={
            "entry_id": entry["id"],
            "engine": "chatterbox",
            "replacement": replacement,
            "status": status,
        },
    )
    return entry


def resolve(db, locale, title_ref="", show_refs=()):
    return KnowledgeSelection.load(db, locale=locale, title_ref=title_ref, show_refs=show_refs)


def test_correction_reuses_across_episodes_without_locale_contamination(client_factory):
    client = client_factory()
    db = client.app.state.jobs.db
    make_correction(client, scope="episode", scope_ref=cast_key(path=EP1))
    make_correction(
        client, scope="show", scope_ref="series:79214", phrase="Mushi", replacement="Mu-shi"
    )

    first = resolve(db, "es-MX", title_ref=cast_key(path=EP1), show_refs=("series:79214",))
    assert first.spoken("Ginko y Mushi", engine="chatterbox")[0] == "Guin-ko y Mu-shi"
    # another episode of the same show inherits the show rule, not the episode rule
    second = resolve(db, "es-MX", title_ref=cast_key(path=EP2), show_refs=("series:79214",))
    assert second.spoken("Ginko y Mushi", engine="chatterbox")[0] == "Ginko y Mu-shi"
    # never into another locale
    for locale in ("es-VE", "es-ES"):
        other = resolve(db, locale, title_ref=cast_key(path=EP1), show_refs=("series:79214",))
        assert other.spoken("Ginko y Mushi", engine="chatterbox")[0] == "Ginko y Mushi"


def export_v2(client, **overrides):
    body = {
        "identity": {"path": EP1},
        "media": MEDIA,
        "target_language": "es-MX",
        "schema_version": 2,
        **overrides,
    }
    response = client.post("/api/recipes/export", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_v2_export_carries_overlay_locale_and_show(client_factory):
    client = client_factory()
    episode_rule = make_correction(client, scope="episode", scope_ref=cast_key(path=EP1))
    make_correction(
        client, scope="show", scope_ref="series:79214", phrase="Mushi", replacement="Mu-shi"
    )
    personal = make_correction(client, scope="personal", phrase="Personal", replacement="Per-sonal")
    data = export_v2(client)
    recipe = data["recipe"]
    assert recipe["schema_version"] == 2
    assert recipe["target_language"] == "es" and recipe["target_locale"] == "es-MX"
    assert recipe["show"]["series_id"] == "series:79214"
    overlay = {e["phrase"]: e for e in recipe["knowledge"]["entries"]}
    assert set(overlay) == {"Ginko", "Mushi"}  # personal rules are never exported by default
    assert overlay["Ginko"]["id"] == episode_rule["id"]
    assert overlay["Ginko"]["realizations"][0]["replacement"] == "Guin-ko"
    assert "scope_ref" not in overlay["Ginko"]  # local identity is not portable
    deps = {d["id"]: d for d in data["personal_dependencies"]}
    assert deps[personal["id"]]["included"] is False
    DubRecipe.model_validate(recipe)

    promoted = export_v2(client, include_personal=[personal["id"]])
    phrases = {e["phrase"] for e in promoted["recipe"]["knowledge"]["entries"]}
    assert "Personal" in phrases
    assert {d["id"]: d for d in promoted["personal_dependencies"]}[personal["id"]]["included"]
    assert (
        client.post(
            "/api/recipes/export",
            json={
                "identity": {"path": EP1},
                "media": MEDIA,
                "schema_version": 2,
                "include_personal": ["unknown-id"],
            },
        ).status_code
        == 422
    )


def test_v2_import_reproduces_the_pinned_selection(client_factory):
    source = client_factory()
    make_correction(source, scope="episode", scope_ref=cast_key(path=EP1))
    make_correction(
        source, scope="show", scope_ref="series:79214", phrase="Mushi", replacement="Mu-shi"
    )
    recipe = export_v2(source)["recipe"]

    fresh = client_factory()
    body = {
        "identity": {"path": "/other/ep1.mkv"},
        "media": MEDIA,
        "recipe": recipe,
        "target_locale": "es-MX",
        "voices": {"narrator": ""},
    }
    preview = fresh.post("/api/recipes/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert {e["state"] for e in preview.json()["knowledge"]["entries"]} == {"will-import"}
    result = fresh.post("/api/recipes/import", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["knowledge_applied"] == 2

    db = fresh.app.state.jobs.db
    first = resolve(
        db, "es-MX", title_ref=cast_key(path="/other/ep1.mkv"), show_refs=("series:79214",)
    )
    assert first.spoken("Ginko y Mushi", engine="chatterbox")[0] == "Guin-ko y Mu-shi"
    # the importing machine's pins match the exported pins exactly
    pinned = recipe["knowledge"]["entries"]
    assert first.snapshot["entries"] == {e["id"]: e["revision"] for e in pinned}
    # re-importing the same recipe is a no-op, not a conflict
    again = fresh.post("/api/recipes/import", json=body)
    assert again.json()["knowledge_applied"] == 2 and again.json()["knowledge_skipped"] == []
    # and the overlay never leaks into another locale on the importing machine
    other = resolve(
        db, "es-VE", title_ref=cast_key(path="/other/ep1.mkv"), show_refs=("series:79214",)
    )
    assert other.spoken("Ginko y Mushi", engine="chatterbox")[0] == "Ginko y Mushi"


def test_v2_import_rejects_a_different_target_locale(client_factory):
    source = client_factory()
    make_correction(source, scope="episode", scope_ref=cast_key(path=EP1))
    recipe = export_v2(source)["recipe"]
    fresh = client_factory()
    body = {
        "identity": {"path": "/other/ep1.mkv"},
        "media": MEDIA,
        "recipe": recipe,
        "target_locale": "es-VE",
        "voices": {"narrator": ""},
    }
    assert fresh.post("/api/recipes/preview", json=body).status_code == 422
    assert fresh.post("/api/recipes/import", json=body).status_code == 422


def test_v1_export_reports_v2_loss_and_v1_import_still_works(client_factory):
    client = client_factory()
    make_correction(client, scope="episode", scope_ref=cast_key(path=EP1))
    client.put("/api/plan", json={"path": EP1, "plan": {"target_lang": "es-MX"}})
    response = client.post(
        "/api/recipes/export",
        json={"identity": {"path": EP1}, "media": MEDIA, "target_language": "es-MX"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["recipe"]["schema_version"] == 1
    assert data["recipe"]["target_language"] == "es"
    assert any("knowledge overlay" in note for note in data["loss"])
    assert any("es-MX" in note for note in data["loss"])
    DubRecipe.model_validate(data["recipe"])  # a v1 reader accepts the file

    body = {
        "identity": {"path": "/other/ep1.mkv"},
        "media": MEDIA,
        "recipe": data["recipe"],
        "voices": {"narrator": ""},
    }
    assert client.post("/api/recipes/import", json=body).status_code == 200


def test_v1_documents_cannot_carry_v2_fields():
    base = {
        "format": "doblarr-recipe",
        "schema_version": 1,
        "mode": "recipe-only",
        "media": MEDIA,
        "target_language": "es",
        "narrator": {"engine": "chatterbox"},
        "settings": {},
    }
    with pytest.raises(ValueError, match="v1"):
        DubRecipe.model_validate({**base, "target_locale": "es-MX"})
    with pytest.raises(ValueError, match="v1"):
        DubRecipe.model_validate({**base, "knowledge": {"entries": [], "pack_dependencies": {}}})
    with pytest.raises(ValueError, match="base"):
        DubRecipe.model_validate(
            {**base, "schema_version": 2, "target_language": "es", "target_locale": "fr-CA"}
        )


def test_preview_reports_missing_packs_and_engine_mismatches(client_factory):
    source = client_factory()
    entry = make_correction(source, scope="episode", scope_ref=cast_key(path=EP1))
    source.post(
        "/api/knowledge/realizations",
        json={"entry_id": entry["id"], "engine": "kokoro", "replacement": "Ghin-ko"},
    )
    recipe = export_v2(source)["recipe"]
    recipe["knowledge"]["pack_dependencies"] = {"community-es-mx": 3}
    fresh = client_factory()
    body = {
        "identity": {"path": "/other/ep1.mkv"},
        "media": MEDIA,
        "recipe": recipe,
        "target_locale": "es-MX",
        "voices": {"narrator": ""},
    }
    preview = fresh.post("/api/recipes/preview", json=body)
    assert preview.status_code == 200, preview.text
    knowledge = preview.json()["knowledge"]
    assert any("community-es-mx" in note for note in knowledge["missing_packs"])
    assert any("kokoro" in note for note in knowledge["engine_mismatches"])
