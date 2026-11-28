"""The Marcarr client wrapper against a stand-in `marcarr` package — no network,
and no real marcarr install."""

import sys
import types

import pytest

from doblarr.clients.marcarr import MarcarrClient, MarcarrError
from doblarr.config import SECRET_SENTINEL, Config, check_overrides
from doblarr.errors import ConfigError, InvalidSettings
from doblarr.services import Services


class APIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Upstream:
    """Stands in for marcarr.client.MarcarrClient; records how it was built."""

    built: list[dict] = []
    fail: APIError | None = None

    def __init__(self, base_url, api_key=None, *, timeout=30):
        self.built.append({"base_url": base_url, "api_key": api_key, "timeout": timeout})

    def status(self):
        if self.fail:
            raise self.fail
        return {"version": "0.2.0"}

    def shots(self, vid, start=None, end=None):
        return [(vid, start, end)]


@pytest.fixture
def marcarr(monkeypatch):
    client = types.ModuleType("marcarr.client")
    client.MarcarrClient = Upstream
    client.APIError = APIError
    package = types.ModuleType("marcarr")
    package.client = client
    monkeypatch.setitem(sys.modules, "marcarr", package)
    monkeypatch.setitem(sys.modules, "marcarr.client", client)
    monkeypatch.setattr(Upstream, "built", [])
    monkeypatch.setattr(Upstream, "fail", None)
    return Upstream


def test_missing_extra_names_the_install_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "marcarr", None)   # makes the import fail
    with pytest.raises(ConfigError, match=r'pip install "doblarr\[marcarr\]"'):
        MarcarrClient("http://m:7470", "mk_harbor")


def test_api_key_is_always_sent(marcarr):
    client = MarcarrClient("http://m:7470", "mk_harbor")
    assert marcarr.built == [{"base_url": "http://m:7470", "api_key": "mk_harbor", "timeout": 30}]
    assert client.status() == {"version": "0.2.0"}
    assert client.shots("v1", end=9.0) == [("v1", None, 9.0)]   # the rest passes through


@pytest.mark.parametrize("key", ["", None])
def test_a_url_without_a_key_is_refused(marcarr, key):
    with pytest.raises(ConfigError, match="marcarr.api_key"):
        MarcarrClient("http://m:7470", key)
    assert marcarr.built == []


def test_upstream_errors_become_marcarr_errors(marcarr):
    marcarr.fail = APIError("Marcarr returned HTTP 401: bad key", 401)
    with pytest.raises(MarcarrError, match="401") as caught:
        MarcarrClient("http://m:7470", "mk_harbor").status()
    assert caught.value.status == 401 and caught.value.http_status == 502


def test_services_builds_it_from_config(marcarr, tmp_path):
    cfg = Config.load(tmp_path / "config.yaml")
    with pytest.raises(ConfigError, match="not configured"):
        _ = Services(cfg).marcarr
    cfg.apply_and_save({"marcarr": {"url": "http://m:7470"}})
    with pytest.raises(ConfigError, match="marcarr.api_key"):
        _ = Services(cfg).marcarr
    cfg.apply_and_save({"marcarr": {"api_key": "mk_harbor"}})
    assert Services(cfg).marcarr.status() == {"version": "0.2.0"}
    assert marcarr.built[-1]["api_key"] == "mk_harbor"


def test_api_key_is_a_secret(tmp_path, monkeypatch):
    cfg = Config.load(tmp_path / "config.yaml")
    assert cfg["marcarr"] == {"url": "", "api_key": ""}
    cfg.apply_and_save({"marcarr": {"url": "http://m:7470", "api_key": "mk_harbor"}})
    assert cfg.as_dict(redact_secrets=True)["marcarr"]["api_key"] == SECRET_SENTINEL
    # A redacted round-trip keeps the key; a moved URL drops it.
    cfg.apply_and_save({"marcarr": {"url": "http://m:7470", "api_key": SECRET_SENTINEL}})
    assert cfg["marcarr"]["api_key"] == "mk_harbor"
    cfg.apply_and_save({"marcarr": {"url": "http://elsewhere:7470", "api_key": SECRET_SENTINEL}})
    assert cfg["marcarr"]["api_key"] == ""
    with pytest.raises(InvalidSettings):
        check_overrides({"marcarr.url": "http://elsewhere:7470"})

    monkeypatch.setenv("DOBLARR_MARCARR_API_KEY", "mk_env")
    assert Config.load(tmp_path / "config.yaml")["marcarr"]["api_key"] == "mk_env"


def test_config_route_redacts_the_key(client_factory):
    c = client_factory({"web": {"api_key": "k"}})
    headers = {"X-Api-Key": "k"}
    r = c.post("/api/config", headers=headers,
               json={"marcarr": {"url": "http://m:7470", "api_key": "mk_harbor"}})
    assert r.status_code == 200
    assert "mk_harbor" not in r.text
    body = c.get("/api/config", headers=headers).json()
    assert body["marcarr"] == {"url": "http://m:7470", "api_key": SECRET_SENTINEL}
