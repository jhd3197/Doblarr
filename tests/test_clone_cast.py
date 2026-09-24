"""Cloning from the original actor: cast entries, chosen references, fallbacks."""

import math
import struct
import wave

import pytest

from doblarr.models import DubJob, Segment, Speaker
from doblarr.stages import synthesize
from scripts.finish_episode import apply_cast

RATE = 16000


def _tone(path, seconds, hz):
    frames = b"".join(struct.pack("<h", int(0.3 * 32000 * math.sin(2 * math.pi * hz * i / RATE)))
                      for i in range(int(RATE * seconds)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(frames)
    return path


class Voicebox:
    def __init__(self):
        self.samples = []

    def list_voices(self):
        return []

    def transcribe(self, path, language=None):
        return {"text": "una referencia limpia"}

    def create_profile(self, name, language, description=""):
        return f"profile-{len(self.samples)}"

    def add_sample(self, profile_id, sample, reference_text):
        self.samples.append((profile_id, sample, reference_text))


def _job(tmp_path, lines):
    job = DubJob(tmp_path / "episode.mkv", "ja", "es")
    job.script_lang = "en"
    job.segments = [Segment(i, a, b, t, speaker=spk) for i, a, b, t, spk in lines]
    job.speakers = {s.speaker: Speaker(s.speaker) for s in job.segments}
    job.vocals = tmp_path / "vocals.wav"
    job.vocals.write_bytes(b"stub")
    return job


def _fake_extract(monkeypatch, pitches):
    """Cut a reference as a tone whose pitch depends on the line it came from."""
    def extract(source, start, end, dest, cancel=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        return _tone(dest, min(2.0, end - start), pitches.get(start, 220))
    monkeypatch.setattr(synthesize, "_extract_ref", extract)


def test_a_clone_entry_needs_no_preset_and_ignores_a_stale_one(tmp_path):
    job = DubJob(tmp_path / "episode.mkv", "en", "es")
    job.segments = [Segment(1, 0, 2, "Hello"), Segment(2, 3, 5, "Hi")]
    cast = apply_cast(job, {"speakers": {
        "GINKO": {"clone": True, "voice": "old-preset", "segments": [1]},
        "SHINRA": {"voice": "preset-b", "segments": [2]}}})
    assert job.speakers["GINKO"].voicebox_profile_id is None
    assert "voice" not in cast["GINKO"]
    assert job.speakers["SHINRA"].voicebox_profile_id == "preset-b"


def test_a_pinned_reference_line_is_the_one_cloned(tmp_path, monkeypatch):
    job = _job(tmp_path, [(10, 0, 8, "Long clean line", "SHINRA"),
                          (95, 20, 26, "The line chosen by ear", "SHINRA")])
    _fake_extract(monkeypatch, {})
    vb = Voicebox()
    cast = {"SHINRA": {"clone": True, "reference_line": 95}}
    synthesize._resolve_profile(job, job.speakers["SHINRA"], vb, tmp_path / "clips",
                                "clone", cast, None)
    assert job.metrics["clone_references"]["SHINRA"]["cue"] == job.segments[1].cue_id


def test_a_pinned_line_that_does_not_exist_is_refused(tmp_path, monkeypatch):
    job = _job(tmp_path, [(10, 0, 8, "Line", "SHINRA")])
    _fake_extract(monkeypatch, {})
    with pytest.raises(ValueError):
        synthesize._resolve_profile(job, job.speakers["SHINRA"], Voicebox(),
                                    tmp_path / "clips", "clone",
                                    {"SHINRA": {"reference_line": 7}}, None)


def test_a_low_reference_prefers_the_lowest_clean_reading(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 8, "Eight seconds, high", "SHINRA"),
                          (2, 20, 26, "Six seconds, low", "SHINRA"),
                          (3, 40, 45, "Five seconds, middle", "SHINRA")])
    _fake_extract(monkeypatch, {0: 280, 20: 180, 40: 230})
    vb = Voicebox()
    synthesize._resolve_profile(job, job.speakers["SHINRA"], vb, tmp_path / "clips",
                                "clone", {"SHINRA": {"reference": "low"}}, None)
    assert job.metrics["clone_references"]["SHINRA"]["cue"] == job.segments[1].cue_id


def test_reference_pitch_reads_a_tone(tmp_path):
    pytest.importorskip("numpy")
    assert synthesize.reference_pitch(_tone(tmp_path / "t.wav", 1.0, 200)) == \
        pytest.approx(200, rel=0.05)
