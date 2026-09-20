import json
from pathlib import Path

import pytest

from doblarr.artifacts import matches, media_work, record
from doblarr.clients.voicebox import GenerationFailed, VoiceboxClient, VoiceboxError
from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import synthesize


def test_distinct_speakers_and_voice_revision_invalidate_only_their_lines(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es", source_audio=tmp_path / "source.wav")
    job.segments = [Segment(0, 0, 2, "Hello", speaker="A"),
                    Segment(1, 3, 5, "Goodbye", speaker="B")]
    job.speakers = {key: Speaker(key) for key in ["A", "B"]}
    calls = []

    class Voicebox:
        def synthesize_to_file(self, profile, text, language, dest, **kwargs):
            calls.append((profile, text))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"audio")

    cast = {"A": {"voice": "alice"}, "B": {"voice": "bob"}}
    synthesize.run(job, Voicebox(), tmp_path, cast=cast)
    assert calls == [("alice", "Hello"), ("bob", "Goodbye")]
    cast["B"]["revision"] = "2"
    synthesize.run(job, Voicebox(), tmp_path, cast=cast)
    assert calls[-1] == ("bob", "Goodbye") and len(calls) == 3
    cast["A"]["voice"] = "new-alice"
    synthesize.run(job, Voicebox(), tmp_path, cast=cast)
    assert calls[-1] == ("new-alice", "Hello")


def test_remote_id_survives_network_failure(tmp_path, monkeypatch):
    client = VoiceboxClient("http://test")
    submissions = []
    waits = []
    def generate(*a, **kw):
        submissions.append(1)
        return "remote-id"
    def wait(gid, **kw):
        waits.append(gid)
        if len(waits) == 1:
            raise VoiceboxError("network unavailable")
    def download(gid, dest):
        dest.write_bytes(b"audio")
        return dest
    monkeypatch.setattr(client, "generate", generate)
    monkeypatch.setattr(client, "wait_for", wait)
    monkeypatch.setattr(client, "download_audio", download)
    dest = tmp_path / "line.wav"
    with pytest.raises(VoiceboxError):
        client.synthesize_to_file("voice", "hello", "en", dest)
    assert json.loads(dest.with_suffix(".request.json").read_text())["generation_id"] == "remote-id"
    client.synthesize_to_file("voice", "hello", "en", dest)
    assert submissions == [1] and waits == ["remote-id", "remote-id"]


def test_terminal_failure_clears_pending_request(tmp_path, monkeypatch):
    client = VoiceboxClient("http://test")
    monkeypatch.setattr(client, "generate", lambda *a, **kw: "failed-id")
    def fail(*a, **kw):
        raise GenerationFailed("failed")
    monkeypatch.setattr(client, "wait_for", fail)
    with pytest.raises(GenerationFailed):
        client.synthesize_to_file("voice", "hello", "en", tmp_path / "line.wav")
    assert not (tmp_path / "line.request.json").exists()


def test_artifacts_track_settings_and_sources(tmp_path):
    dest = tmp_path / "mix.wav"
    dest.write_bytes(b"audio")
    request = {"start": 1, "ratio": 4}
    record([dest], request)
    assert matches([dest], request)
    assert not matches([dest], {**request, "start": 2})
    assert not matches([dest], request, force=True)
    dest.write_bytes(b"broken")
    assert not matches([dest], request)
    a = DubJob(Path("a/movie.mkv"), "en", "es")
    b = DubJob(Path("b/movie.mkv"), "en", "es")
    assert media_work(tmp_path, a) != media_work(tmp_path, b)
