"""Teaser jobs — duration-limited pipeline, distinct artifacts, cast wiring."""

import logging
from pathlib import Path

from doblarr.config import Config
from doblarr.jobs import JobStore, Worker
from doblarr.models import DubJob, Segment, Speaker
from doblarr.pipeline import run_job
from doblarr.stages import diarize, extract, transcribe
from doblarr.store import Database
from doblarr.voices import ensure_cast


def _tease_job(tmp_path, kind="tease") -> DubJob:
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    return DubJob(input_file=src, source_lang="ko", target_lang="es", kind=kind)


def test_dry_run_tease_plan_has_marker(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="doblarr.extract"):
        run_job(_tease_job(tmp_path), Config.load("nope.yaml"), dry_run=True)
    extract_logs = [r.getMessage() for r in caplog.records
                    if r.name == "doblarr.extract"]
    assert any("-t 600" in m for m in extract_logs)        # teaser_minutes 10 -> 600s
    assert any(".tease.source.wav" in m for m in extract_logs)


def test_tease_artifacts_distinct_from_full(tmp_path, monkeypatch):
    calls = []
    def render(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"audio")
    monkeypatch.setattr(extract, "run_ffmpeg", render)
    monkeypatch.setattr(extract, "_select_audio_stream", lambda *a: 1)
    job = _tease_job(tmp_path)
    extract.run(job, tmp_path / "work", duration=600)
    assert job.source_audio.name == "movie.tease.source.wav"
    assert "-t" in calls[0] and "600" in calls[0]

    full = _tease_job(tmp_path, kind="full")
    extract.run(full, tmp_path / "work")
    assert full.source_audio.name == "movie.source.wav"
    assert not any("-t" in c for c in calls[1])  # full extract is not cut


def test_transcribe_max_seconds_window(tmp_path):
    sub = tmp_path / "film.srt"
    sub.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nearly line\n\n"
        "2\n00:11:40,000 --> 00:11:43,000\nlate line\n", encoding="utf-8")
    job = _tease_job(tmp_path)
    job.subtitle_file = sub
    transcribe.run(job, tmp_path / "work", max_seconds=600)
    assert [s.text_src for s in job.segments] == ["early line"]
    job2 = _tease_job(tmp_path, kind="full")
    job2.subtitle_file = sub
    transcribe.run(job2, tmp_path / "work")
    assert len(job2.segments) == 2  # no window on a full dub


def test_diarize_falls_back_to_narrator(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    job = _tease_job(tmp_path)
    job.source_audio = tmp_path / "movie.tease.source.wav"
    diarize.run(job, enabled=True, dry_run=False)  # no token, no pyannote: no raise
    assert list(job.speakers) == ["NARRATOR"]


def test_ensure_cast_tease_creates_and_publishes(tmp_path):
    db = Database(tmp_path / "d.db")
    bus_events = []

    class FakeBus:
        def publish(self, topic, payload):
            bus_events.append((topic, payload))
    job = _tease_job(tmp_path)
    job.speakers = {"S0": Speaker("S0"), "S1": Speaker("S1")}
    cast = ensure_cast(job, db, events=FakeBus())
    assert [e["label"] for e in cast] == ["Adult M 1", "Adult F 1"]
    assert db.load_cast(cast_key_for(job)) is not None
    assert bus_events == [("cast", {"type": "updated", "key": cast_key_for(job),
                                    "speakers": 2})]

    # a full dub reuses the saved cast without rewriting it
    full = _tease_job(tmp_path, kind="full")
    full.speakers = {"S0": Speaker("S0"), "S1": Speaker("S1")}
    assert ensure_cast(full, db) == cast
    assert len(bus_events) == 1
    db.close()


def cast_key_for(job):
    from doblarr.voices import cast_key
    return cast_key(path=str(job.input_file))


def test_ensure_cast_full_without_cast_returns_none(tmp_path):
    db = Database(tmp_path / "d.db")
    full = _tease_job(tmp_path, kind="full")
    assert ensure_cast(full, db) is None
    db.close()


def test_synthesize_uses_cast_voice_instead_of_cloning(tmp_path):
    job = _tease_job(tmp_path)
    job.source_audio = tmp_path / "work" / "movie.tease.source.wav"
    job.segments = [Segment(0, 0.0, 1.0, "line", text_translated="línea")]
    job.speakers = {"NARRATOR": Speaker("NARRATOR")}

    class FakeVb:
        def create_profile(self, **kw):
            raise AssertionError("must not clone when the cast assigns a voice")

        def synthesize_to_file(self, profile_id, text, lang, dest, **kw):
            assert profile_id == "preset-1"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("audio", encoding="utf-8")
            return dest

    from doblarr.stages import synthesize
    synthesize.run(job, FakeVb(), tmp_path / "work",
                   cast={"NARRATOR": {"voice": "preset-1"}})
    assert job.speakers["NARRATOR"].voicebox_profile_id == "preset-1"
    assert job.segments[0].audio_clip.name == "line_0000.wav"
    # tease clips live in their own namespace
    assert "clips-tease" in str(job.segments[0].audio_clip)


def test_worker_threads_tease_kind(tmp_path, monkeypatch):
    seen = {}

    def fake_run_job(dj, config, dry_run=False, on_stage=None, cancel_event=None,
                     services=None, force=False, db=None, events=None,
                     on_progress=None):
        seen["kind"] = dj.kind
    monkeypatch.setattr("doblarr.jobs.run_job", fake_run_job)
    store = JobStore(tmp_path / "jobs.db")
    worker = Worker(store, Config.load("nope.yaml"))
    job = store.add(title="Tease Me", source="t", source_lang="ko",
                    target_lang="en", kind="tease")
    worker._process(job)
    assert seen["kind"] == "tease"
    assert store.get(job.id).status == "done"
    store.close()
