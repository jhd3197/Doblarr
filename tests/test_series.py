from types import SimpleNamespace

from doblarr.voices import cast_key


def setup_series(client, tmp_path):
    show = {
        "id": 10,
        "tvdbId": 79214,
        "title": "Mushi-Shi",
        "path": str(tmp_path),
        "originalLanguage": {"name": "Japanese"},
    }
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    episodes = [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 11, "title": "First"},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 12, "title": "Second"},
        {"id": 3, "seasonNumber": 2, "episodeNumber": 1, "episodeFileId": 0, "title": "Missing"},
        {
            "id": 4,
            "seasonNumber": 1,
            "episodeNumber": 3,
            "episodeFileId": 12,
            "title": "Shared file",
        },
    ]
    files = [
        {"id": 11, "path": str(first), "mediaInfo": {"audioLanguages": "eng/jpn"}},
        {"id": 12, "path": str(second), "mediaInfo": {"audioLanguages": "jpn"}},
    ]
    client.app.state.services._cache["sonarr"] = SimpleNamespace(
        list_series=lambda: [show], episodes=lambda sid: episodes, episode_files=lambda sid: files
    )
    return first, second


def test_episode_status_is_per_language_and_full_output(client_factory, tmp_path):
    client = client_factory()
    first, second = setup_series(client, tmp_path)
    store = client.app.state.jobs
    output = client.app.state.worker.config.output_dir / "dub.mkv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"output")
    store.add(
        source="Sonarr",
        source_lang="ja",
        title="Preview",
        input_file=str(second),
        target_lang="es",
        status="done",
        kind="tease",
        output_file=str(output),
    )
    store.add(
        title="Dry run",
        source="Sonarr",
        source_lang="ja",
        input_file=str(first),
        target_lang="es",
        status="done",
    )
    spanish = client.get("/api/series/79214/episodes?target_lang=es").json()
    assert spanish["total"] == 4 and spanish["downloaded"] == 3 and spanish["dubbed"] == 0
    assert [e["status"] for e in spanish["episodes"]] == [
        "needs-dub",
        "needs-dub",
        "needs-dub",
        "not-downloaded",
    ]
    english = client.get("/api/series/79214/episodes?target_lang=en").json()
    assert english["episodes"][0]["status"] == "audio-present"
    store.add(
        source="Sonarr",
        source_lang="ja",
        title="Full dub",
        input_file=str(second),
        target_lang="es",
        status="done",
        kind="full",
        output_file=str(output),
    )
    assert client.get("/api/series/79214/episodes?target_lang=es").json()["dubbed"] == 2
    output.unlink()
    assert client.get("/api/series/79214/episodes?target_lang=es").json()["dubbed"] == 0


def test_episode_queue_deduplicates_and_inherits_series_narrator(client_factory, tmp_path):
    client = client_factory()
    first, second = setup_series(client, tmp_path)
    store = client.app.state.jobs
    store.db.save_plan(
        cast_key(path=str(tmp_path)),
        "Mushi-Shi",
        {"dub.narrator_voice": "warm-voice", "target_lang": "en", "dub.dry_run": False},
    )
    store.db.save_plan(
        cast_key(path=str(second)), "Second", {"dub.narrator_voice": "episode-voice"}
    )
    request = {"episode_ids": [1, 2, 3, 4], "target_lang": "es"}
    response = client.post("/api/series/79214/queue", json=request)
    assert response.status_code == 200
    assert len(response.json()["queued"]) == 2 and len(response.json()["skipped"]) == 2
    jobs = store.list()
    assert {j["input_file"] for j in jobs} == {str(first), str(second)}
    assert all(j["target_lang"] == "es" and not j["overrides"]["dub.dry_run"] for j in jobs)
    assert {j["overrides"]["dub.narrator_voice"] for j in jobs} == {"warm-voice", "episode-voice"}
    assert not client.post("/api/series/79214/queue", json=request).json()["queued"]
    assert (
        client.post("/api/series/79214/queue", json={**request, "episode_ids": [999]}).status_code
        == 422
    )


def test_folder_job_is_rejected(client_factory, tmp_path):
    client = client_factory()
    response = client.post("/api/jobs", json={"title": "Show", "path": str(tmp_path)})
    assert response.status_code == 422 and "Episodes" in response.json()["error"]
