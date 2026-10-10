"""Failure recovery: cancellation, corrupt or missing media, resumes without repeat calls."""

import shutil
import threading
from pathlib import Path

import numpy as np
import pytest

from doblarr import adaptive, envelopes, features, judge, track_alignment
from doblarr.errors import JobCancelled

ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def tone(path, seconds=3.0, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    envelopes.write_wav(path, (0.3 * np.sin(2 * np.pi * 200 * t))[:, None], rate)
    return path


@ffmpeg
def test_a_cancel_stops_line_measurement_between_lines(tmp_path):
    cancel = threading.Event()
    measured = []

    def progress(done, total, note):
        measured.append(done)
        cancel.set()                    # the user cancels after the first line

    with pytest.raises(JobCancelled):
        features.measure_lines(tone(tmp_path / "a.wav"), [(0, 1), (1, 2), (2, 3)],
                               cancel=cancel, progress=progress)
    assert measured == [0]              # the second line was never measured


@ffmpeg
def test_a_cancel_stops_frame_streaming(tmp_path):
    import subprocess

    from doblarr.vision import media

    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:s=64x48:d=3", "-r", "10", str(video)], check=True)
    cancel = threading.Event()
    frames = media.frames(video, 5, 48, cancel=cancel)
    next(frames)
    cancel.set()
    with pytest.raises(JobCancelled):
        next(frames)


@ffmpeg
def test_corrupt_or_missing_audio_is_reported_missing_not_measured(tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"RIFF....not audio")
    assert features.measure_file(broken)["quality"] == "missing"
    rows, _ = features.measure_lines(tmp_path / "absent.wav", [(0, 1)])
    assert rows[0]["quality"] == "missing" and "mean_db" not in rows[0]
    evidence = track_alignment.check_tracks(tone(tmp_path / "r.wav"), [
        {"stream": 2, "path": tmp_path / "gone.wav", "lang": "es", "title": ""}])
    assert evidence[0]["state"] == "uncertain"
    assert track_alignment.usable(evidence) == set()


class CountingClient:
    model = "fake/judge"

    def __init__(self):
        self.calls = 0

    def ask(self, schema, system, payload, max_tokens=800):
        self.calls += 1
        return schema.model_validate({"candidate": payload["candidates"][0]["id"],
                                      "evidence": ["candidates[0].score"]})

    def describe(self):
        return {}


@ffmpeg
def test_a_resumed_run_never_asks_the_judge_twice_for_the_same_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from doblarr.cues import FITTED, Artifact
    from doblarr.models import DubJob, Segment

    take = tone(tmp_path / "take.wav", 2.0, 24000)
    job = DubJob(input_file=tmp_path / "e.mkv", source_lang="ja", target_lang="es")
    seg = Segment(0, 0.0, 2.0, "Wait!", cue_id="c0")
    seg.audio.put_render(Artifact(role=FITTED, path=str(take), fingerprint="fp-take"))
    job.segments = [seg]
    client = CountingClient()
    monkeypatch.setattr(judge, "build", lambda *a, **k: judge.LLMJudge(client))
    config = SimpleNamespace(get=lambda k, d=None: {"mode": "suggest", "judge": "llm:fake"}
                             if k == "adaptive" else d)
    first = adaptive.recommend(job, config, work=tmp_path)
    assert client.calls == 1 and first["judge_cache_hits"] == 0
    second = adaptive.recommend(job, config, work=tmp_path)
    assert client.calls == 1 and second["judge_cache_hits"] == 1     # no repeat request
    assert Path(first["path"]).is_file()
