"""Track naming + Plex auto-refresh after a muxed dub."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import requests

from doblarr.clients.plex import PlexClient, PlexError
from doblarr.config import Config
from doblarr.discovery import lang_name
from doblarr.jobs import JobStore, Worker
from doblarr.models import DubJob
from doblarr.stages import mux


def test_track_title_naming(tmp_path):
    assert lang_name("en") == "English" and lang_name("es") == "Spanish"
    assert lang_name("xx") == "XX"  # unknown code falls back to uppercased code

    job = DubJob(input_file=tmp_path / "movie.mkv", source_lang="ko",
                 target_lang="en")
    job.dubbed_track = tmp_path / "dub.wav"
    plan = mux.run.__wrapped__(job, tmp_path / "out", dry_run=True)
    assert "title=English AI" in plan.detail
    assert "language=eng" in plan.detail  # ISO-639-2 metadata tag unchanged

    tease = DubJob(input_file=tmp_path / "movie.mkv", source_lang="ko",
                   target_lang="es", kind="tease")
    tease.dubbed_track = tmp_path / "dub.wav"
    plan = mux.run.__wrapped__(tease, tmp_path / "out", dry_run=True)
    assert "title=Spanish AI (tease)" in plan.detail
    # user's custom template with the old placeholder still works
    plan = mux.run.__wrapped__(job, tmp_path / "out",
                               track_name_template="AI - {language}", dry_run=True)
    assert "title=AI - EN" in plan.detail


def test_plex_refresh_item_request_shape():
    calls = []

    class FakeResp:
        status_code = 200
        ok = True
        text = ""
        headers = {}
        content = b""

    def capture(self, method, url, **kw):
        calls.append((method, url))
        return FakeResp()

    with patch.object(requests.Session, "request", capture):
        PlexClient("http://p", "tok").refresh_item("rk-42")
    assert calls == [("PUT", "http://p/library/metadata/rk-42/refresh")]


def test_plex_refresh_item_post_fallback():
    calls = []

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
            self.ok = code < 400
            self.text = ""
            self.headers = {}
            self.content = b""

    def capture(self, method, url, **kw):
        calls.append((method, url))
        return FakeResp(404 if method == "PUT" else 200)

    with patch.object(requests.Session, "request", capture):
        PlexClient("http://p", "tok").refresh_item("rk-42")  # no raise
    assert calls == [("PUT", "http://p/library/metadata/rk-42/refresh"),
                     ("POST", "http://p/library/metadata/rk-42/refresh")]


def _worker_with(tmp_path, monkeypatch, cfg_data, plex):
    def fake_run_job(dj, config, **kw):
        dj.output_file = tmp_path / "out" / "movie.mkv"
    monkeypatch.setattr("doblarr.jobs.run_job", fake_run_job)
    monkeypatch.setattr("doblarr.jobs.find_item",
                        lambda plex, title, year=None, source="": {"ratingKey": "rk1"})
    store = JobStore(tmp_path / "jobs.db")
    worker = Worker(store, Config(cfg_data), services=SimpleNamespace(plex=plex))
    job = store.add(title="Film", source="Radarr · Films", source_lang="ko",
                    target_lang="en")
    worker._process(job)
    return store, job


def test_plex_auto_refresh_after_real_dub(tmp_path, monkeypatch):
    calls = []

    class FakePlex:
        def refresh_item(self, rating_key):
            calls.append(rating_key)

    store, job = _worker_with(tmp_path, monkeypatch,
                              {"dub": {"dry_run": False}}, FakePlex())
    assert calls == ["rk1"]
    assert store.get(job.id).status == "done"
    store.close()


def test_plex_refresh_failure_does_not_fail_job(tmp_path, monkeypatch, caplog):
    class BrokenPlex:
        def refresh_item(self, rating_key):
            raise PlexError("Plex down", status=502)

    with caplog.at_level(logging.WARNING, logger="doblarr.jobs"):
        store, job = _worker_with(tmp_path, monkeypatch,
                                  {"dub": {"dry_run": False}}, BrokenPlex())
    assert store.get(job.id).status == "done"  # Plex down must not fail the job
    assert any("auto-refresh" in r.getMessage() for r in caplog.records)
    store.close()


def test_plex_auto_refresh_disabled(tmp_path, monkeypatch):
    calls = []

    class FakePlex:
        def refresh_item(self, rating_key):
            calls.append(rating_key)

    store, job = _worker_with(tmp_path, monkeypatch,
                              {"dub": {"dry_run": False},
                               "plex": {"auto_refresh": False}}, FakePlex())
    assert calls == []
    assert store.get(job.id).status == "done"
    store.close()


def test_plex_auto_refresh_skipped_in_dry_run(tmp_path, monkeypatch):
    calls = []

    class FakePlex:
        def refresh_item(self, rating_key):
            calls.append(rating_key)

    store, job = _worker_with(tmp_path, monkeypatch, {}, FakePlex())
    assert calls == []  # dry-run default: plan only, nothing to refresh
    store.close()
