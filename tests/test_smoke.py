"""Smoke tests — no heavy deps, no network. Verify the skeleton holds together."""

from pathlib import Path

from doblarr import discovery
from doblarr.config import Config
from doblarr.models import DubJob, Segment, Speaker


def test_config_defaults():
    cfg = Config.load("does-not-exist.yaml")
    assert cfg["voicebox"]["base_url"].startswith("http")
    assert cfg.work_dir == Path("./work")
    assert cfg["dub"]["voice_mode"] in {"clone", "preset"}


def test_config_save_and_redact():
    import tempfile

    from doblarr.config import SECRET_SENTINEL
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        cfg = Config.load(p)
        cfg.apply_and_save({"connect": {"radarr_api_key": "SECRET123", "radarr_url": "http://x:7878"}})
        # secret is redacted on read-out
        assert cfg.as_dict(redact_secrets=True)["connect"]["radarr_api_key"] == SECRET_SENTINEL
        # a sentinel value on save must NOT overwrite the real key
        cfg.apply_and_save({"connect": {"radarr_api_key": SECRET_SENTINEL, "radarr_url": "http://y:7878"}})
        assert cfg["connect"]["radarr_api_key"] == "SECRET123"
        assert cfg["connect"]["radarr_url"] == "http://y:7878"
        # persisted to disk
        assert Config.load(p)["connect"]["radarr_api_key"] == "SECRET123"


def test_segment_duration():
    seg = Segment(index=0, start=1.0, end=3.5, text_src="안녕하세요")
    assert seg.duration == 2.5


def test_dubjob_summary():
    job = DubJob(input_file=Path("movie.mkv"), source_lang="ko", target_lang="es")
    job.segments = [Segment(0, 0.0, 1.0, "a"), Segment(1, 1.0, 2.0, "b")]
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00")}
    s = job.summary()
    assert "ko -> es" in s and "2 segments" in s


def test_discovery_movies():
    movies = [
        {"title": "KoreanFilm", "hasFile": True, "originalLanguage": {"name": "Korean"},
         "movieFile": {"mediaInfo": {"audioLanguages": "kor"}}},
        {"title": "GermanDubbed", "hasFile": True, "originalLanguage": {"name": "German"},
         "movieFile": {"mediaInfo": {"audioLanguages": "eng"}}},
        {"title": "EnglishUnd", "hasFile": True, "originalLanguage": {"name": "English"},
         "movieFile": {"mediaInfo": {"audioLanguages": "und"}}},
        {"title": "NoFile", "hasFile": False, "originalLanguage": {"name": "Korean"}},
    ]
    got = {i.title: i.status for i in discovery.scan_radarr(movies, ["en", "es"])}
    assert got == {"KoreanFilm": "needs-dub", "GermanDubbed": "available",
                   "EnglishUnd": "available"}  # NoFile excluded


def test_discovery_shows():
    series = [
        {"id": 1, "title": "EngShow", "originalLanguage": {"name": "English"},
         "statistics": {"episodeFileCount": 2}},
        {"id": 2, "title": "KoShow", "originalLanguage": {"name": "Korean"},
         "statistics": {"episodeFileCount": 2}},
        {"id": 3, "title": "JaPartial", "originalLanguage": {"name": "Japanese"},
         "statistics": {"episodeFileCount": 2}},
    ]
    files = {
        2: [{"mediaInfo": {"audioLanguages": "kor"}}, {"mediaInfo": {"audioLanguages": "kor"}}],
        3: [{"mediaInfo": {"audioLanguages": "jpn/eng"}}, {"mediaInfo": {"audioLanguages": "jpn"}}],
    }
    items = discovery.scan_sonarr(series, lambda sid: files.get(sid, []), ["en", "es"])
    got = {i.title: i.status for i in items}
    assert got == {"EngShow": "available", "KoShow": "needs-dub", "JaPartial": "partial"}


def test_job_store_persist():
    import tempfile

    from doblarr.jobs import JobStore
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "jobs.json"
        s = JobStore(p)
        j = s.add(title="Godzilla", source="Radarr", source_lang="ja", target_lang="en")
        assert s.counts()["queued"] == 1
        assert s.next_queued().id == j.id
        s.update(j.id, status="done", progress=100)
        assert s.counts() == {"queued": 0, "running": 0, "done": 1, "failed": 0}
        # survives reload
        assert JobStore(p).list()[0]["status"] == "done"
        # clear finished
        s.add(title="Still queued", source="Radarr", source_lang="ja", target_lang="en")
        assert s.clear_finished() == 1          # only the done one
        assert s.counts()["queued"] == 1
        assert s.counts()["done"] == 0


def test_plex_label_sync():
    from doblarr.discovery import LibraryItem
    from doblarr.plex_labels import sync_labels

    items = [
        LibraryItem("A", 2020, "ko", "Radarr · Films", "ko", "needs-dub", "needs-dub", True),
        LibraryItem("B", 2021, "en", "Radarr · Films", "en", "available", "available", False),
    ]

    class FakePlex:
        def sections(self):
            return [{"key": "1", "type": "movie", "title": "Movies"},
                    {"key": "2", "type": "show", "title": "TV Shows"}]
        def find(self, sk, tn, title, year):
            return {"ratingKey": "rk-A", "title": title, "year": year, "labels": []} if title == "A" else None
        def items_with_label(self, sk, tn, label):
            return [{"ratingKey": "rk-B", "title": "B", "year": 2021}] if sk == "1" else []
        def add_label(self, *a): pass
        def remove_label(self, *a): pass

    r = sync_labels(items, FakePlex(), Config.load("nope.yaml"), apply=False)
    assert r["matched"] == 1 and r["added"] == 1 and r["removed"] == 1, r


def test_scheduler():
    import time

    from doblarr.scheduler import Scheduler, parse_interval
    assert parse_interval("6h") == 21600
    assert parse_interval("30m") == 1800
    assert parse_interval("90s") == 90
    assert parse_interval("bad") == 6 * 3600

    calls = {"n": 0}

    class Cfg:
        def get(self, k, d=None):
            return {"discovery": {"auto_scan": True, "rescan_interval": "60s"}}.get(k, d)

    s = Scheduler(Cfg(), lambda: calls.__setitem__("n", calls["n"] + 1))
    s.start(); time.sleep(0.4); s.stop(); s.join(timeout=2)
    assert calls["n"] >= 1
