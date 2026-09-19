"""SPA fallback — History API paths serve index.html; API misses stay JSON."""

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.config import Config
from doblarr.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({}), encoding="utf-8")
    return TestClient(create_app(Config.load(p)))


def test_spa_paths_serve_index_html(client):
    for path in ("/library", "/settings/connections", "/dubs", "/nope/unknown"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html")
        assert "<title>Doblarr</title>" in r.text


def test_root_serves_index(client):
    assert client.get("/").status_code == 200


def test_api_miss_is_json_404(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "error" in r.json()


def test_missing_asset_is_plain_404(client):
    r = client.get("/missing.png")
    assert r.status_code == 404
    assert "text/html" not in r.headers["content-type"]


def test_real_assets_still_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "text/html" not in r.headers["content-type"]


def test_api_routes_unaffected(client):
    assert client.get("/api/health").json()["ok"] is True
