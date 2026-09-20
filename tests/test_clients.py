"""ArrClient base behavior with a mocked requests.Session — no network."""


import requests

from doblarr.clients.plex import PlexClient, PlexError
from doblarr.clients.radarr import RadarrClient, RadarrError
from doblarr.clients.voicebox import VoiceboxClient, VoiceboxError


class FakeResp:
    def __init__(self, status: int = 200, json_data=None, headers: dict | None = None):
        self.status_code = status
        self.ok = status < 400
        self.text = "body"
        self.headers = headers or {}
        self.content = b"data"
        self._json = json_data if json_data is not None else {"ok": True}

    def json(self):
        return self._json


def mock_session(monkeypatch, responses: list, fast_sleep: bool = True) -> list[dict]:
    """Patch Session.request to play back `responses` (last one repeats)."""
    if fast_sleep:
        monkeypatch.setattr("time.sleep", lambda *_: None)  # skip retry backoff
    calls: list[dict] = []

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(requests.Session, "request", fake_request)
    return calls


def test_retry_on_429_honoring_retry_after(monkeypatch):
    calls = mock_session(monkeypatch, [
        FakeResp(429, headers={"Retry-After": "0"}),
        FakeResp(429, headers={"Retry-After": "0"}),
        FakeResp(200, json_data=[{"id": 1}]),
    ])
    assert RadarrClient("http://r", "k").list_movies() == [{"id": 1}]
    assert len(calls) == 3


def test_retry_exhausted_on_500_maps_to_error(monkeypatch):
    calls = mock_session(monkeypatch, [FakeResp(500)])
    try:
        RadarrClient("http://r", "k").list_movies()
        raise AssertionError("expected RadarrError")
    except RadarrError as exc:
        assert exc.status == 500
        assert "Radarr 500" in str(exc)
    assert len(calls) == RadarrClient.max_retries + 1


def test_no_retry_on_401(monkeypatch):
    calls = mock_session(monkeypatch, [FakeResp(401)])
    try:
        RadarrClient("http://r", "k").list_movies()
        raise AssertionError("expected RadarrError")
    except RadarrError as exc:
        assert exc.status == 401
        assert "rejected the API key" in str(exc)
    assert len(calls) == 1


def test_connection_error_retries_then_unreachable(monkeypatch):
    calls = mock_session(monkeypatch, [requests.ConnectionError("down")])
    try:
        VoiceboxClient("http://v").health()
        raise AssertionError("expected VoiceboxError")
    except VoiceboxError as exc:
        assert exc.status is None
        assert "unreachable" in str(exc)
    assert len(calls) == VoiceboxClient.max_retries + 1


def test_plex_token_in_header_not_params(monkeypatch):
    calls = mock_session(monkeypatch, [FakeResp(200, json_data={"MediaContainer": {}})])
    plex = PlexClient("http://p", "tok123")
    plex.sections()
    assert plex.session.headers["X-Plex-Token"] == "tok123"
    assert "tok123" not in str(calls[0].get("params"))
    assert "tok123" not in calls[0]["url"]
    plex.add_label("1", 1, "rk", "needs-dub")
    assert "tok123" not in str(calls[1].get("params"))


def test_timeout_plumbing(monkeypatch):
    calls = mock_session(monkeypatch, [FakeResp(200)])
    client = RadarrClient("http://r", "k", timeout=42)
    client.list_movies()
    assert calls[0]["timeout"] == 42
    client._get("/api/v3/system/status", timeout=5)  # per-request override wins
    assert calls[1]["timeout"] == 5


def test_error_subclasses_share_hierarchy(monkeypatch):
    from doblarr.errors import ArrClientError, DoblarrError
    mock_session(monkeypatch, [FakeResp(404)])
    try:
        PlexClient("http://p", "t").sections()
        raise AssertionError("expected PlexError")
    except PlexError as exc:
        assert isinstance(exc, ArrClientError)
        assert isinstance(exc, DoblarrError)
        assert exc.status == 404
        assert exc.http_status == 502  # upstream failure maps to 502 at the API


def test_services_container():
    from doblarr.config import Config
    from doblarr.errors import ConfigError
    from doblarr.services import Services

    empty = Services(Config.load("nope.yaml"))
    try:
        _ = empty.radarr
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "radarr_url" in str(exc)

    cfg = Config({"connect": {"radarr_url": "http://r", "radarr_api_key": "k"},
                  "voicebox": {"base_url": "http://v", "timeout_seconds": 5}})
    svc = Services(cfg)
    first = svc.radarr
    assert svc.radarr is first            # cached
    assert first.base_url == "http://r"
    assert svc.voicebox.base_url == "http://v"
    assert svc.voicebox.timeout == 5
    svc.invalidate()
    assert svc.radarr is not first        # rebuilt after invalidate
