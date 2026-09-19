"""Webhook tests — *arr event classification, debouncing, endpoints, auth."""

import time

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.config import Config
from doblarr.server import create_app
from doblarr.webhooks import Debouncer, is_test_event, should_rescan

API_KEY = "test-key"
HEADERS = {"X-Api-Key": API_KEY}

RADARR_DOWNLOAD = {
    "eventType": "Download",
    "isUpgrade": False,
    "movie": {"id": 1, "title": "KoreanFilm", "year": 2020},
}


def test_event_classification():
    assert is_test_event({"eventType": "Test"})
    assert should_rescan(RADARR_DOWNLOAD)
    assert should_rescan({"eventType": "download", "series": {"id": 1}})
    assert not should_rescan({"eventType": "Grab"})
    assert not should_rescan({})


def test_debouncer_coalesces_burst():
    calls = {"n": 0}
    d = Debouncer(lambda: calls.__setitem__("n", calls["n"] + 1), delay=0.15)
    d.trigger()
    d.trigger()
    d.trigger()
    assert d.pending
    time.sleep(0.4)
    assert calls["n"] == 1          # 3 triggers -> 1 call
    assert not d.pending
    d.trigger()
    d.cancel()                      # cancelled before the delay elapses
    time.sleep(0.3)
    assert calls["n"] == 1


@pytest.fixture
def webhook_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_movies(self):
        calls["n"] += 1
        return [{"title": "KoreanFilm", "hasFile": True,
                 "originalLanguage": {"name": "Korean"},
                 "movieFile": {"mediaInfo": {"audioLanguages": "kor"}}}]
    monkeypatch.setattr("doblarr.clients.radarr.RadarrClient.list_movies", fake_movies)

    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "web": {"api_key": API_KEY},
        "connect": {"radarr_url": "http://r", "radarr_api_key": "k"},
        "discovery": {"webhook_debounce": 0.1},
    }), encoding="utf-8")
    return TestClient(create_app(Config.load(p))), calls


def test_webhook_requires_auth(webhook_client):
    client, _ = webhook_client
    assert client.post("/api/webhooks/radarr", json=RADARR_DOWNLOAD).status_code == 401


def test_webhook_test_event_does_not_scan(webhook_client):
    client, calls = webhook_client
    r = client.post("/api/webhooks/radarr", headers=HEADERS, json={"eventType": "Test"})
    assert r.status_code == 200 and r.json() == {"ok": True, "test": True}
    time.sleep(0.3)
    assert calls["n"] == 0


def test_webhook_download_triggers_debounced_scan(webhook_client):
    client, calls = webhook_client
    for path in ("/api/webhooks/radarr", "/api/webhooks/sonarr"):
        r = client.post(path, headers=HEADERS, json=RADARR_DOWNLOAD)
        assert r.status_code == 200 and r.json() == {"ok": True, "scan": "scheduled"}
    deadline = time.time() + 3
    while calls["n"] == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert calls["n"] == 1  # burst of 2 webhooks coalesced into one rescan


def test_webhook_ignores_non_download_events(webhook_client):
    client, calls = webhook_client
    r = client.post("/api/webhooks/sonarr", headers=HEADERS,
                    json={"eventType": "Grab", "series": {"id": 1}})
    assert r.json() == {"ok": True, "ignored": "Grab"}
    time.sleep(0.3)
    assert calls["n"] == 0
