from types import SimpleNamespace


def fake_voicebox(client):
    calls = []
    client.app.state.services._cache["speech"] = SimpleNamespace(
        voice_profiles=lambda: [
            {"id": "saved", "name": "Original", "language": "es", "default_engine": "qwen"}
        ],
        preset_voices=lambda engine: [
            {"voice_id": "voice", "name": "Storyteller", "language": "es", "gender": "male"}
        ],
        register_preset=lambda voice, engine: calls.append(engine) or "registered",
        generate=lambda *a, **kw: calls.append(kw) or "preview-id",
        preset_engines=("kokoro", "qwen_custom_voice"),
        supports_direction=lambda engine: engine in {"qwen", "qwen_custom_voice"},
        generation_status=lambda gid: {"status": "completed", "error": None},
        fetch_audio=lambda gid: b"audio",
    )
    return calls


def test_catalog_combines_presets_without_importing_and_preserves_unknown_age(client_factory):
    client = client_factory()
    calls = fake_voicebox(client)
    rows = client.get("/api/voice-catalog").json()["voices"]
    assert len(rows) == 3 and not calls
    assert all(v["age"] == "unknown" for v in rows)
    key = "preset:kokoro:voice"
    assert (
        client.put(
            "/api/voice-catalog/traits", json={"key": key, "age": "older", "gender": "male"}
        ).status_code
        == 200
    )
    tagged = client.get("/api/voice-catalog").json()["voices"]
    assert next(v for v in tagged if v["key"] == key)["age"] == "older"
    chosen = client.post("/api/voice-catalog/select", json={"key": key}).json()
    assert chosen["profile_id"] == "registered" and chosen["engine"] == "kokoro"
    assert calls == ["kokoro"]


def test_catalog_preview_guards_language_and_scopes_audio(client_factory):
    client = client_factory()
    calls = fake_voicebox(client)
    body = {"key": "preset:kokoro:voice", "text": "Hello", "language": "en"}
    assert client.post("/api/voice-catalog/preview", json=body).status_code == 422
    assert not calls
    body.update(key="preset:qwen_custom_voice:voice", direction="Older, thoughtful voice")
    result = client.post("/api/voice-catalog/preview", json=body)
    assert result.status_code == 200
    assert calls[-1]["engine"] == "qwen_custom_voice"
    assert calls[-1]["instruct"] == body["direction"]
    assert client.get("/api/voice-catalog/preview/preview-id").json()["status"] == "completed"
    assert client.get("/api/voice-catalog/preview/preview-id/audio").content == b"audio"
    assert client.get("/api/voice-catalog/preview/unrelated/audio").status_code == 404


def test_a_voice_has_an_identity_and_says_where_it_is_cast(client_factory):
    client = client_factory()
    fake_voicebox(client)
    key = "profile:saved"
    named = client.put("/api/voice-catalog/traits", json={
        "key": key, "gender": "male", "age": "young", "display_name": "Kaito",
        "character": "KAITO", "show": "tvdb-81234", "show_name": "Harbor Lights"})
    assert named.status_code == 200
    db = client.app.state.jobs.db
    db.save_cast("e02", "S01E02", [{"speaker_id": "KAITO", "label": "Kaito",
                                    "voice": "saved", "pitch_semitones": 2}])
    db.save_cast("e01", "S01E01", [{"speaker_id": "MINA", "voice": "other"}])
    detail = client.get("/api/voice-catalog/voice", params={"key": key}).json()
    assert detail["voice"]["display_name"] == "Kaito"
    assert detail["voice"]["show"] == "tvdb-81234" and detail["voice"]["gender"] == "male"
    assert [u["title_key"] for u in detail["used_in"]] == ["e02"]
    assert detail["used_in"][0]["pitch_semitones"] == 2
    assert client.get("/api/voice-catalog/voice", params={"key": "profile:nope"}).status_code == 404
    coloured = {"key": key, "display_name": "Kaito", "color": "#F08A24"}
    assert client.put("/api/voice-catalog/traits", json=coloured).status_code == 200
    shown = client.get("/api/voice-catalog/voice", params={"key": key}).json()["voice"]
    assert shown["color"] == "#F08A24"
    bad = client.put("/api/voice-catalog/traits", json={**coloured, "color": "orange"})
    assert bad.status_code == 422          # a colour is a hex value, never free text
