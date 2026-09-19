"""Server tests — FastAPI TestClient with mocked clients; no network, no *arr."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.clients.voicebox import VoiceboxError
from doblarr.config import SECRET_SENTINEL, Config
from doblarr.server import create_app

API_KEY = "test-key"
HEADERS = {"X-Api-Key": API_KEY}

RADARR_MOVIES = [
    {"title": "KoreanFilm", "hasFile": True, "originalLanguage": {"name": "Korean"},
     "movieFile": {"mediaInfo": {"audioLanguages": "kor"}}},
]


def _config(tmp_path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a temp config with web.api_key set (no lifespan)."""
    monkeypatch.chdir(tmp_path)  # work/, jobs.json and logs land in tmp
    cfg = Config.load(_config(tmp_path, {"web": {"api_key": API_KEY}}))
    return TestClient(create_app(cfg))


def _patch_voicebox_down(monkeypatch):
    def _health(self, timeout=15):
        raise VoiceboxError("voicebox unreachable at http://x: connection refused")
    monkeypatch.setattr("doblarr.clients.voicebox.VoiceboxClient.health", _health)


def test_auth_required_on_api_but_not_health(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"X-Api-Key": "wrong"}).status_code == 401
    assert client.get("/api/jobs", headers=HEADERS).status_code == 200
    assert client.get("/api/jobs", params={"api_key": API_KEY}).status_code == 200
    # health endpoints are exempt
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/ready").status_code != 401
    # the static UI is not behind the key
    assert client.get("/").status_code == 200


def test_auth_open_when_no_key_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(_config(tmp_path, {}))
    assert TestClient(create_app(cfg)).get("/api/jobs").status_code == 200


def test_readiness_503_until_source_and_voicebox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _patch_voicebox_down(monkeypatch)
    cfg = Config.load(_config(tmp_path, {}))
    with TestClient(create_app(cfg)) as c:
        r = c.get("/api/health/ready")
        assert r.status_code == 503
        problems = r.json()["problems"]
        assert any("library source" in p for p in problems)
        assert any("voicebox" in p for p in problems)

    # configure a source + bring voicebox up -> ready
    monkeypatch.setattr("doblarr.clients.voicebox.VoiceboxClient.health",
                        lambda self, timeout=15: {"ok": True})
    cfg = Config.load(_config(tmp_path,
                              {"connect": {"radarr_url": "http://r", "radarr_api_key": "k"}}))
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/health/ready").json() == {"ready": True}


def test_config_get_redacts_secrets(client):
    r = client.post("/api/config", headers=HEADERS,
                    json={"connect": {"radarr_api_key": "SECRET123"}})
    assert r.status_code == 200
    body = client.get("/api/config", headers=HEADERS).json()
    assert body["connect"]["radarr_api_key"] == SECRET_SENTINEL
    assert body["web"]["api_key"] == SECRET_SENTINEL


def test_job_crud_and_404(client):
    assert client.post("/api/jobs", headers=HEADERS, json={}).status_code == 422
    r = client.post("/api/jobs", headers=HEADERS,
                    json={"title": "Godzilla", "source_lang": "ja"})
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["target_lang"] == "en"  # first target language is the default
    assert any(j["id"] == job["id"]
               for j in client.get("/api/jobs", headers=HEADERS).json()["jobs"])
    assert client.delete(f"/api/jobs/{job['id']}", headers=HEADERS).json() == {"ok": True}
    assert client.delete(f"/api/jobs/{job['id']}", headers=HEADERS).status_code == 404


def test_plex_labels_validation_and_not_configured(client):
    r = client.post("/api/plex/labels", headers=HEADERS, json={"apply": "not-a-bool"})
    assert r.status_code == 422
    r = client.post("/api/plex/labels", headers=HEADERS, json={"apply": True})
    assert r.status_code == 400  # Plex not configured
    assert "plex" in r.json()["error"].lower()


def test_library_scan_cached_and_refresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake_movies(self):
        calls["n"] += 1
        return RADARR_MOVIES
    monkeypatch.setattr("doblarr.clients.radarr.RadarrClient.list_movies", fake_movies)

    cfg = Config.load(_config(tmp_path, {
        "web": {"api_key": API_KEY},
        "connect": {"radarr_url": "http://r", "radarr_api_key": "k"},
    }))
    with TestClient(create_app(cfg)) as c:
        r = c.get("/api/library", headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["counts"]["needs_dub"] == 1
        assert r.json()["items"][0]["title"] == "KoreanFilm"
        c.get("/api/library", headers=HEADERS)                      # cached
        assert calls["n"] == 1
        c.get("/api/library", headers=HEADERS, params={"refresh": "true"})
        assert calls["n"] == 2


def test_lifespan_starts_and_stops_threads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(_config(tmp_path, {}))
    app = create_app(cfg)
    worker, scheduler = app.state.worker, app.state.scheduler
    assert not worker.is_alive() and not scheduler.is_alive()
    with TestClient(app):
        assert worker.is_alive() and scheduler.is_alive()
    assert not worker.is_alive() and not scheduler.is_alive()
