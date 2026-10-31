from types import SimpleNamespace

from doblarr.voices import cast_key


def setup_series(client, tmp_path):
    show = {
        "id": 10,
        "tvdbId": 81234,
        "title": "Quiet Valley",
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
    spanish = client.get("/api/series/81234/episodes?target_lang=es").json()
    assert spanish["total"] == 4 and spanish["downloaded"] == 3 and spanish["dubbed"] == 0
    assert [e["status"] for e in spanish["episodes"]] == [
        "needs-dub",
        "needs-dub",
        "needs-dub",
        "not-downloaded",
    ]
    english = client.get("/api/series/81234/episodes?target_lang=en").json()
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
    assert client.get("/api/series/81234/episodes?target_lang=es").json()["dubbed"] == 2
    output.unlink()
    assert client.get("/api/series/81234/episodes?target_lang=es").json()["dubbed"] == 0


def test_episode_queue_deduplicates_and_inherits_series_narrator(client_factory, tmp_path):
    client = client_factory()
    first, second = setup_series(client, tmp_path)
    store = client.app.state.jobs
    store.db.save_plan(
        cast_key(path=str(tmp_path)),
        "Quiet Valley",
        {"dub.narrator_voice": "warm-voice", "target_lang": "en", "dub.dry_run": False},
    )
    store.db.save_plan(
        cast_key(path=str(second)), "Second", {"dub.narrator_voice": "episode-voice"}
    )
    request = {"episode_ids": [1, 2, 3, 4], "target_lang": "es"}
    response = client.post("/api/series/81234/queue", json=request)
    assert response.status_code == 200
    assert len(response.json()["queued"]) == 2 and len(response.json()["skipped"]) == 2
    jobs = store.list()
    assert {j["input_file"] for j in jobs} == {str(first), str(second)}
    assert all(j["target_lang"] == "es" and not j["overrides"]["dub.dry_run"] for j in jobs)
    assert {j["overrides"]["dub.narrator_voice"] for j in jobs} == {"warm-voice", "episode-voice"}
    assert not client.post("/api/series/81234/queue", json=request).json()["queued"]
    assert (
        client.post("/api/series/81234/queue", json={**request, "episode_ids": [999]}).status_code
        == 422
    )


def test_folder_job_is_rejected(client_factory, tmp_path):
    client = client_factory()
    response = client.post("/api/jobs", json={"title": "Show", "path": str(tmp_path)})
    assert response.status_code == 422 and "Episodes" in response.json()["error"]


def _script(path, rows):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"segments": [
        {"speaker": spk, "start": a, "end": b, "text_src": text,
         "cue": {"measurement": {"relative_db": db}}}
        for spk, a, b, db, text in rows]}), encoding="utf-8")


def test_show_voices_share_talk_across_episodes_and_link_named_voices(client_factory, tmp_path):
    client = client_factory()
    first, second = setup_series(client, tmp_path)
    work = client.app.state.worker.config.work_dir
    # Two runs of episode 1 (an older unmeasured one is skipped) and one of episode 2,
    # which ran from a copy of the file elsewhere: matched by the file name.
    _script(work / "media" / "a" / "es-419" / "first.script.json", [
        ("KAITO", 0, 6, 5.0, "¡Sí!"), ("MINA", 7, 9, 0.0, "Kaito.")])
    _script(work / "media" / "b" / "es-MX" / "first.script.json", [
        ("KAITO", 0, 6, None, "old run")])
    _script(work / "media" / "c" / "es-419" / "second.script.json", [
        ("KAITO", 0, 2, -4.0, "...ya."), ("TAMAKI", 3, 5, 0.5, "Hola.")])
    client.app.state.jobs.db.save_cast(cast_key(path=str(tmp_path / "copy" / "second.mkv")),
                                       "copy", [{"speaker_id": "TAMAKI", "voice": "vb-t"}])
    client.app.state.services._cache["speech"] = SimpleNamespace(
        voice_profiles=lambda: [{"id": "vb-n", "name": "x"}], preset_engines=(),
        preset_voices=lambda e: [])
    assert client.put("/api/voice-catalog/traits", json={
        "key": "profile:vb-n", "display_name": "Kaito", "character": "KAITO",
        "show": "tvdb-81234"}).status_code == 200
    data = client.get("/api/series/81234/voices").json()
    rows = {r["speaker"]: r for r in data["speakers"]}
    assert [r["speaker"] for r in data["speakers"]] == ["KAITO", "MINA", "TAMAKI"]
    assert rows["KAITO"]["seconds"] == 8 and rows["KAITO"]["share"] == 0.6667
    assert rows["KAITO"]["episodes"] == [1, 2]
    assert rows["KAITO"]["bands"] == {"quiet": 0.25, "calm": 0.0, "intense": 0.75,
                                       "unmeasured": 0.0}
    assert [h["text"] for h in rows["KAITO"]["highlights"]] == ["¡Sí!"]
    assert rows["KAITO"]["voice"]["name"] == "Kaito"
    assert rows["TAMAKI"]["voice"]["key"] == "profile:vb-t"
    assert rows["MINA"]["voice"] is None
    assert [e["label"] for e in data["analysed"]] == ["S01E01", "S01E02"]   # E03 shares a file


def test_analysing_an_episode_again_runs_the_way_its_first_analysis_did(client_factory, tmp_path):
    import json

    client = client_factory()
    first, _second = setup_series(client, tmp_path)
    work = client.app.state.worker.config.work_dir
    folder = work / "media" / "abc" / "es-419"
    folder.mkdir(parents=True)
    (folder / "first.script.json").write_text(json.dumps({"identity": {
        "input": str(first), "source_lang": "ja", "target_lang": "es"}, "segments": []}),
        encoding="utf-8")
    # The page's dub goes to English; the analysis was made under es-419.
    queued = client.post("/api/series/81234/queue", json={
        "episode_ids": [1], "target_lang": "en", "kind": "analyze", "missing_only": False}).json()
    job = client.app.state.jobs.get(queued["queued"][0]["job_id"])
    assert (job.kind, job.source_lang, job.target_lang, job.target_locale) == (
        "analyze", "ja", "es", "es-419")


def test_single_episode_matches_the_series_row(client_factory, tmp_path):
    client = client_factory()
    setup_series(client, tmp_path)
    listed = client.get("/api/series/81234/episodes?target_lang=es").json()
    one = client.get("/api/series/81234/episodes/2?target_lang=es").json()
    assert one["episode"] == next(e for e in listed["episodes"] if e["id"] == 2)
    assert one["show"] == {"title": "Quiet Valley", "source": "Sonarr · Shows",
                           "path": str(tmp_path), "original": "ja", "tvdb_id": 81234}
    assert one["target_lang"] == "es"
    assert client.get("/api/series/81234/episodes/99?target_lang=es").status_code == 404
