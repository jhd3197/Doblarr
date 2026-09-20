"""ArrClient base behavior with a mocked requests.Session — no network."""

import sys
from types import SimpleNamespace

import requests

from doblarr.clients.plex import PlexClient, PlexError
from doblarr.clients.radarr import RadarrClient, RadarrError
from doblarr.clients.translator import ClaudeTranslator
from doblarr.clients.voicebox import VoiceboxClient, VoiceboxError
from doblarr.errors import ArrClientError, ConfigError


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


# -- ClaudeTranslator: anthropic SDK faked in sys.modules — no network --------

class FakeAnthropicAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def fake_anthropic(monkeypatch, replies: list) -> list[dict]:
    """Patch sys.modules['anthropic']; replies are text strings (last repeats)."""
    calls: list[dict] = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            r = replies[min(len(calls) - 1, len(replies) - 1)]
            if isinstance(r, Exception):
                raise r
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])

    class Client:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=Client, APIError=FakeAnthropicAPIError))
    return calls


def test_claude_translates_single_line(monkeypatch):
    calls = fake_anthropic(monkeypatch, ["1. Hola, ¿qué tal?"])
    t = ClaudeTranslator(api_key="k")
    assert t.translate("Hello, how are you?", "en", "es") == "Hola, ¿qué tal?"
    assert len(calls) == 1
    assert calls[0]["model"] == "claude-sonnet-5"
    assert "en" in calls[0]["system"] and "es" in calls[0]["system"]


def test_claude_multiline_batch_preserves_order_and_count(monkeypatch):
    calls = fake_anthropic(monkeypatch, ["1. línea uno\n2. línea dos"])
    t = ClaudeTranslator(api_key="k")
    out = t.translate("line one\nline two", "en", "es", target_chars=40)
    assert out == "línea uno\nlínea dos"
    assert len(calls) == 1                      # one batched call, not two
    assert "40" in calls[0]["system"]           # length budget reaches the prompt


def test_claude_unnumbered_single_line_reply_accepted(monkeypatch):
    fake_anthropic(monkeypatch, ["Hola"])
    assert ClaudeTranslator(api_key="k").translate("Hi", "en", "es") == "Hola"


def test_claude_count_mismatch_retries_then_falls_back_per_line(monkeypatch):
    calls = fake_anthropic(monkeypatch, [
        "1. solo una",            # batch attempt 1: wrong count
        "1. solo una",            # batch attempt 2: still wrong
        "1. uno",                 # per-line fallback
        "1. dos",
    ])
    t = ClaudeTranslator(api_key="k")
    assert t.translate("one\ntwo", "en", "es") == "uno\ndos"
    assert len(calls) == 4


def test_claude_missing_package_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import fails
    try:
        ClaudeTranslator(api_key="k").translate("hi", "en", "es")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "pip install anthropic" in str(exc)


def test_claude_missing_api_key_is_config_error(monkeypatch):
    fake_anthropic(monkeypatch, ["1. hola"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        ClaudeTranslator().translate("hi", "en", "es")
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)


def test_claude_api_error_maps_to_arr_client_error(monkeypatch):
    fake_anthropic(monkeypatch, [FakeAnthropicAPIError("overloaded", status_code=529)])
    try:
        ClaudeTranslator(api_key="k").translate("hi", "en", "es")
        raise AssertionError("expected ArrClientError")
    except ArrClientError as exc:
        assert exc.status == 529
        assert "Claude" in str(exc)
