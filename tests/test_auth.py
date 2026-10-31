"""API key generation and the Host check that stops DNS rebinding."""

import re

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from doblarr.auth import OPEN_PATHS, ensure_api_key, host_allowed, host_name
from doblarr.config import Config
from doblarr.server import create_app


@pytest.mark.parametrize("header,name", [
    ("example.com:6363", "example.com"), ("NAS", "nas"), ("[::1]:6363", "::1"),
    ("::1", "::1"), ("192.168.1.20:6363", "192.168.1.20"),
])
def test_host_name_drops_port_and_brackets(header, name):
    assert host_name(header) == name


@pytest.mark.parametrize("host", [
    "127.0.0.1:6363", "192.168.1.20", "[::1]:6363", "localhost:6363", "nas:6363",
    "nas.local", "server.lan", "box.home.arpa", "app.internal", "dev.localhost",
])
def test_addresses_and_local_names_are_always_allowed(host):
    assert host_allowed(host, [])


def test_a_public_domain_needs_listing():
    assert not host_allowed("attacker.example:6363", [])
    assert not host_allowed("", [])
    assert host_allowed("dub.example.com", ["dub.example.com"])
    assert host_allowed("dub.example.com:443", [".example.com"])
    assert not host_allowed("example.com.evil.net", [".example.com"])


def _app(tmp_path, web: dict) -> TestClient:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"web": web}), encoding="utf-8")
    return TestClient(create_app(Config.load(path)))


def test_unlisted_domain_is_refused_before_auth(tmp_path):
    client = _app(tmp_path, {"api_key": "k"})
    r = client.get("/api/health", headers={"Host": "rebind.example"})
    assert r.status_code == 400
    assert "web.allowed_hosts" in r.json()["error"]
    assert client.get("/api/health").status_code == 200       # Host: testserver


def test_listed_domain_and_environment_hosts_are_served(tmp_path, monkeypatch):
    client = _app(tmp_path, {"api_key": "k", "allowed_hosts": ["dub.example.com"]})
    assert client.get("/api/health", headers={"Host": "dub.example.com"}).status_code == 200
    monkeypatch.setenv("DOBLARR_ALLOWED_HOSTS", "other.example.org")
    assert client.get("/api/health", headers={"Host": "other.example.org"}).status_code == 200


def test_first_serve_generates_and_saves_a_key(tmp_path):
    path = tmp_path / "config.yaml"
    config = Config.load(path)
    key = ensure_api_key(config)
    assert key and len(key) >= 24
    assert config["web"]["api_key"] == key
    assert Config.load(path)["web"]["api_key"] == key
    assert ensure_api_key(config) is None             # an existing key is kept


def test_no_key_is_generated_when_unauthenticated_is_chosen(tmp_path):
    config = Config.load(tmp_path / "config.yaml")
    config.apply_and_save({"web": {"allow_unauthenticated": True}})
    assert ensure_api_key(config) is None
    assert config["web"]["api_key"] == ""


def _api_routes(routes, prefix=""):
    """(full path, route) for every APIRoute; newer FastAPI wraps included routers."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            inner = prefix + route.include_context.prefix
            yield from _api_routes(route.original_router.routes, inner)


def test_every_api_route_requires_the_key(tmp_path):
    client = _app(tmp_path, {"api_key": "k"})
    checked = 0
    for full, route in _api_routes(client.app.routes):
        if not full.startswith("/api/") or full in OPEN_PATHS:
            continue
        path = re.sub(r"\{[^}]+\}", "x", full)
        method = sorted(route.methods)[0]
        r = client.request(method, path)
        assert r.status_code == 401, f"{method} {full} answered {r.status_code}"
        checked += 1
    assert checked > 150
