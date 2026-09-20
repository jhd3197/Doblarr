"""SPA fallback — History API paths serve index.html; API misses stay JSON."""

import pytest


@pytest.fixture
def client(client_factory):
    return client_factory()


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
