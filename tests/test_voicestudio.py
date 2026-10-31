"""VoiceStudio adapter and backend selection — mocked HTTP, no network."""

import threading
import time

import pytest

from doblarr.clients.speech import SpeechError, Unsupported, build_speech_client
from doblarr.clients.translator import build_translator
from doblarr.clients.voicebox import VoiceboxClient
from doblarr.clients.voicestudio import VoiceStudioClient
from doblarr.config import Config
from doblarr.errors import ConfigError, JobCancelled
from tests.test_clients import FakeResp, mock_session


def _config(backend, **voicestudio):
    overrides = {"speech.backend": backend, "voicestudio.base_url": "http://vs:3900"}
    overrides.update({f"voicestudio.{k}": v for k, v in voicestudio.items()})
    return Config.load("missing.yaml").with_overrides(overrides)


def test_backend_selects_the_adapter():
    assert isinstance(build_speech_client(Config.load("missing.yaml")), VoiceboxClient)
    studio = build_speech_client(_config("voicestudio", api_key="secret",
                                         directable_engines=["indextts2"]))
    assert isinstance(studio, VoiceStudioClient)
    assert studio.base_url == "http://vs:3900"
    assert studio.session.headers["Authorization"] == "Bearer secret"
    assert studio.supports_direction("indextts2")
    assert not studio.supports_direction("omnivoice")
    with pytest.raises(ConfigError):
        build_speech_client(_config("elevenlabs"))


def test_fork_keeps_credentials_and_capabilities():
    studio = VoiceStudioClient("http://vs", api_key="k", directable_engines=["x"])
    twin = studio.fork()
    assert twin.session is not studio.session
    assert twin.session.headers["Authorization"] == "Bearer k"
    assert twin.supports_direction("x")


def test_clone_uploads_reference_with_profile(monkeypatch, tmp_path):
    sample = tmp_path / "ref.wav"
    sample.write_bytes(b"RIFF")
    calls = mock_session(monkeypatch, [FakeResp(200, json_data={"id": "p-1"})])
    pid = VoiceStudioClient("http://vs").clone_voice("Tomoe", "es", sample, "hola")
    assert pid == "p-1"
    call = calls[0]
    assert (call["method"], call["url"]) == ("POST", "http://vs/profiles")
    assert call["data"] == {"name": "Tomoe", "ref_text": "hola", "language": "es",
                            "kind": "clone"}
    assert "ref_audio" in call["files"]


def test_synthesize_writes_wav_from_openai_route(monkeypatch, tmp_path):
    calls = mock_session(monkeypatch, [FakeResp(200)])
    dest = tmp_path / "clips" / "line.wav"
    VoiceStudioClient("http://vs").synthesize_to_file("p-1", "Hola", "es", dest, seed=7,
                                                      model_size="large")
    assert dest.read_bytes() == b"data"
    payload = calls[0]["json"]
    assert calls[0]["url"] == "http://vs/v1/audio/speech"
    assert payload == {"model": "tts-1", "input": "Hola", "voice": "p-1", "language": "es",
                       "response_format": "wav", "seed": 7}


def test_direction_is_refused_for_undeclared_engines(monkeypatch, tmp_path):
    calls = mock_session(monkeypatch, [FakeResp(200)])
    client = VoiceStudioClient("http://vs")
    with pytest.raises(SpeechError):
        client.synthesize_to_file("p", "Hola", "es", tmp_path / "a.wav",
                                  engine="omnivoice", instruct="whisper it")
    assert not calls


def test_cancel_stops_waiting(monkeypatch, tmp_path):
    release = threading.Event()

    def slow(self, method, url, **kwargs):
        release.wait(5)
        return FakeResp(200)

    monkeypatch.setattr("requests.Session.request", slow)
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        VoiceStudioClient("http://vs").synthesize_to_file(
            "p", "Hola", "es", tmp_path / "a.wav", cancel_event=cancel)
    assert time.monotonic() - started < 2
    release.set()
    assert not (tmp_path / "a.wav").exists()


def test_preview_generation_is_polled_locally(monkeypatch):
    mock_session(monkeypatch, [FakeResp(200)])
    client = VoiceStudioClient("http://vs")
    gid = client.generate("p-1", "Hola", "es")
    deadline = time.monotonic() + 5
    while client.generation_status(gid)["status"] == "generating":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert client.generation_status(gid) == {"status": "completed", "error": None}
    assert client.fetch_audio(gid) == b"data"
    with pytest.raises(SpeechError) as exc:
        client.generation_status("unknown")
    assert exc.value.status == 404


def test_profiles_and_cloning_capability(monkeypatch):
    profiles = FakeResp(200, json_data=[{"id": "p", "name": "A", "kind": "clone"}])
    mock_session(monkeypatch, [
        profiles, profiles,
        FakeResp(200, json_data={"backends": [
            {"id": "omnivoice", "supports_cloning": True},
            {"id": "kittentts", "supports_cloning": False}]}),
    ])
    client = VoiceStudioClient("http://vs")
    assert client.voice_profiles()[0]["voice_type"] == "cloned"
    assert client.list_voices() == [{"id": "p", "name": "A"}]
    assert client.supports_cloning("omnivoice") is True
    assert client.supports_cloning("kittentts") is False
    assert client.supports_cloning("nothing") is None


def test_transcribe_uses_openai_route(monkeypatch, tmp_path):
    clip = tmp_path / "c.wav"
    clip.write_bytes(b"RIFF")
    calls = mock_session(monkeypatch, [FakeResp(200, json_data={"text": "hola"})])
    assert VoiceStudioClient("http://vs").transcribe(clip, language="es") == {"text": "hola"}
    assert calls[0]["url"] == "http://vs/v1/audio/transcriptions"
    assert calls[0]["data"] == {"response_format": "json", "language": "es"}


def test_no_language_model_on_voicestudio():
    client = VoiceStudioClient("http://vs")
    with pytest.raises(Unsupported):
        client.llm_generate("translate this")
    with pytest.raises(ConfigError):
        build_translator("voicebox", "", voicebox_client=client)
