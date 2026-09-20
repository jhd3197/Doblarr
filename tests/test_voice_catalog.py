from types import SimpleNamespace


def fake_voicebox(client):
    calls = []
    client.app.state.services._cache["voicebox"] = SimpleNamespace(
        voice_profiles=lambda: [
            {"id": "saved", "name": "Original", "language": "es", "default_engine": "qwen"}
        ],
        preset_voices=lambda engine: [
            {"voice_id": "voice", "name": "Storyteller", "language": "es", "gender": "male"}
        ],
        register_preset=lambda voice, engine: calls.append(engine) or "registered",
        generate=lambda *a, **kw: calls.append(kw) or "preview-id",
        _get=lambda path: {"status": "completed"},
        _request=lambda *a: SimpleNamespace(content=b"audio"),
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
