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

    def clone_voice(self, name, language, sample, reference_text, description=""):
        profile_id = f"profile-{len(self.samples)}"
        self.samples.append((profile_id, sample, reference_text))
        return profile_id


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
        "SORA": {"clone": True, "voice": "old-preset", "segments": [1]},
        "TOMOE": {"voice": "preset-b", "segments": [2]}}})
    assert job.speakers["SORA"].voicebox_profile_id is None
    assert "voice" not in cast["SORA"]
    assert job.speakers["TOMOE"].voicebox_profile_id == "preset-b"


def test_a_pinned_reference_line_is_the_one_cloned(tmp_path, monkeypatch):
    job = _job(tmp_path, [(10, 0, 8, "Long clean line", "TOMOE"),
                          (95, 20, 26, "The line chosen by ear", "TOMOE")])
    _fake_extract(monkeypatch, {})
    vb = Voicebox()
    cast = {"TOMOE": {"clone": True, "reference_line": 95}}
    synthesize._resolve_profile(job, job.speakers["TOMOE"], vb, tmp_path / "clips",
                                "clone", cast, None)
    assert job.metrics["clone_references"]["TOMOE"]["cue"] == job.segments[1].cue_id


def test_a_pinned_line_that_does_not_exist_is_refused(tmp_path, monkeypatch):
    job = _job(tmp_path, [(10, 0, 8, "Line", "TOMOE")])
    _fake_extract(monkeypatch, {})
    with pytest.raises(ValueError):
        synthesize._resolve_profile(job, job.speakers["TOMOE"], Voicebox(),
                                    tmp_path / "clips", "clone",
                                    {"TOMOE": {"reference_line": 7}}, None)


def test_a_character_with_only_short_lines_has_no_clone_reference(tmp_path, monkeypatch):
    # Voicebox rejects a sample under 2 s; the job must reach the cast's
    # fallback path instead of failing on the upload.
    job = _job(tmp_path, [(1, 0, 1.63, "Heh-heh... That's right.", "MINA")])
    _fake_extract(monkeypatch, {})
    vb = Voicebox()
    with pytest.raises(RuntimeError, match="no clean single-speaker reference"):
        synthesize._resolve_profile(job, job.speakers["MINA"], vb, tmp_path / "clips",
                                    "clone", {}, None)
    assert vb.samples == []


def test_a_reference_refused_as_too_short_falls_through_to_the_next(tmp_path, monkeypatch):
    from doblarr.clients.voicebox import VoiceboxError

    class Refusing(Voicebox):
        def clone_voice(self, name, language, sample, reference_text, description=""):
            if not self.samples:
                self.samples.append(None)
                raise VoiceboxError("voicebox 400: Audio too short (minimum 2.0 seconds)")
            return super().clone_voice(name, language, sample, reference_text)

    job = _job(tmp_path, [(1, 0, 2.02, "Barely long enough", "MINA"),
                          (2, 10, 12.5, "Long enough", "MINA")])
    _fake_extract(monkeypatch, {})
    monkeypatch.setattr(synthesize, "reference_score",
                        lambda seg, job: (1, seg.index == 1))   # try the short one first
    synthesize._resolve_profile(job, job.speakers["MINA"], Refusing(), tmp_path / "clips",
                                "clone", {}, None)
    assert job.metrics["clone_references"]["MINA"]["cue"] == job.segments[1].cue_id


def test_a_rejected_sample_does_not_leave_an_empty_profile(tmp_path):
    from doblarr.clients.voicebox import VoiceboxClient, VoiceboxError

    deleted = []
    client = VoiceboxClient.__new__(VoiceboxClient)
    client.create_profile = lambda name, language, description="": "p-1"
    client.add_sample = lambda *a: (_ for _ in ()).throw(VoiceboxError("too short"))
    client.delete_profile = deleted.append
    sample = _tone(tmp_path / "s.wav", 1.0, 220)
    with pytest.raises(VoiceboxError):
        client.clone_voice("n", "es", sample, "text")
    assert deleted == ["p-1"]


def test_a_low_reference_prefers_the_lowest_clean_reading(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 8, "Eight seconds, high", "TOMOE"),
                          (2, 20, 26, "Six seconds, low", "TOMOE"),
                          (3, 40, 45, "Five seconds, middle", "TOMOE")])
    _fake_extract(monkeypatch, {0: 280, 20: 180, 40: 230})
    vb = Voicebox()
    synthesize._resolve_profile(job, job.speakers["TOMOE"], vb, tmp_path / "clips",
                                "clone", {"TOMOE": {"reference": "low"}}, None)
    assert job.metrics["clone_references"]["TOMOE"]["cue"] == job.segments[1].cue_id


def test_reference_pitch_reads_a_tone(tmp_path):
    pytest.importorskip("numpy")
    assert synthesize.reference_pitch(_tone(tmp_path / "t.wav", 1.0, 200)) == \
        pytest.approx(200, rel=0.05)


def test_a_voiceless_cast_entry_keeps_its_clone_settings():
    from doblarr.voices import merge_cast

    own = [{"speaker_id": "KAITO", "voice": "", "reference": "low"},
           {"speaker_id": "SHIORI", "voice": "", "fallback_voice": "preset-f"},
           {"speaker_id": "SORA", "voice": ""}]
    inherited = [{"speaker_id": "SORA", "voice": "series-sora"}]
    merged = {e["speaker_id"]: e for e in merge_cast(own, inherited)}
    assert merged["KAITO"]["reference"] == "low"
    assert merged["SHIORI"]["fallback_voice"] == "preset-f"
    assert merged["SORA"]["voice"] == "series-sora"   # a blank entry does not unset it
    assert merge_cast(None, []) == []


def test_a_low_reference_works_when_no_line_reaches_four_seconds(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 3.9, "Loud and high", "KAITO"),
                          (2, 10, 13.0, "Quiet and low", "KAITO"),
                          (3, 20, 21.2, "Too short to clone", "KAITO")])
    _fake_extract(monkeypatch, {0: 310, 10: 190, 20: 150})
    synthesize._resolve_profile(job, job.speakers["KAITO"], Voicebox(), tmp_path / "clips",
                                "clone", {"KAITO": {"reference": "low"}}, None)
    assert job.metrics["clone_references"]["KAITO"]["cue"] == job.segments[1].cue_id


def test_a_high_reference_prefers_the_brightest_clean_reading(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 4.3, "Deep and low", "TAMAKI"),
                          (2, 20, 24.5, "Bright", "TAMAKI"),
                          (3, 40, 45, "Middle", "TAMAKI")])
    _fake_extract(monkeypatch, {0: 180, 20: 280, 40: 222})
    synthesize._resolve_profile(job, job.speakers["TAMAKI"], Voicebox(), tmp_path / "clips",
                                "clone", {"TAMAKI": {"reference": "high"}}, None)
    assert job.metrics["clone_references"]["TAMAKI"]["cue"] == job.segments[1].cue_id


def test_one_long_line_is_not_the_only_choice_for_a_pitch_reference(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 4.3, "The only long line, deep", "TAMAKI"),
                          (2, 20, 22.9, "Shorter and bright", "TAMAKI"),
                          (3, 40, 42.4, "Shorter, middle", "TAMAKI")])
    _fake_extract(monkeypatch, {0: 222, 20: 279, 40: 235})
    synthesize._resolve_profile(job, job.speakers["TAMAKI"], Voicebox(), tmp_path / "clips",
                                "clone", {"TAMAKI": {"reference": "high"}}, None)
    assert job.metrics["clone_references"]["TAMAKI"]["cue"] == job.segments[1].cue_id


def test_a_pitch_shift_raises_the_take_and_keeps_its_length(tmp_path):
    pytest.importorskip("numpy")
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    take = _tone(tmp_path / "take.wav", 2.0, 200)
    synthesize.shift_pitch(take, 4.0)
    assert synthesize.reference_pitch(take) == pytest.approx(200 * 2 ** (4 / 12), rel=0.05)
    with wave.open(str(take), "rb") as audio:
        assert audio.getnframes() / audio.getframerate() == pytest.approx(2.0, abs=0.05)


def test_no_pitch_setting_leaves_the_take_untouched(tmp_path):
    take = _tone(tmp_path / "take.wav", 1.0, 200)
    before = take.read_bytes()
    synthesize.shift_pitch(take, 0.0)
    assert take.read_bytes() == before
    cast = {"TAMAKI": {"pitch_semitones": "2", "formant_semitones": 4}}
    assert synthesize._voice_shift(cast, "TAMAKI") == (2.0, 4.0)
    assert synthesize._voice_shift({}, "TAMAKI") == (0.0, 0.0)


def test_a_formant_shift_keeps_the_pitch_it_was_asked_for(tmp_path):
    pytest.importorskip("numpy")
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required")
    take = _tone(tmp_path / "take.wav", 2.0, 200)
    before = take.read_bytes()
    synthesize.shift_pitch(take, 1.0, formant=4.0)
    assert take.read_bytes() != before
    assert synthesize.reference_pitch(take) == pytest.approx(200 * 2 ** (1 / 12), rel=0.05)


def _glide(path, seconds, low_hz, high_hz):
    """A tone whose pitch sweeps between two values: a lively reading."""
    frames, phase = [], 0.0
    for i in range(int(RATE * seconds)):
        t = i / RATE
        hz = low_hz + (high_hz - low_hz) * (0.5 + 0.5 * math.sin(2 * math.pi * 0.8 * t))
        phase += 2 * math.pi * hz / RATE
        frames.append(struct.pack("<h", int(0.3 * 32000 * math.sin(phase))))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def test_a_lively_reference_prefers_the_most_expressive_reading(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    job = _job(tmp_path, [(1, 0, 4.5, "Flat and quiet", "KAITO"),
                          (2, 20, 24.5, "Excited", "KAITO")])

    def extract(source, start, end, dest, cancel=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        return (_glide(dest, 2.0, 200, 340) if start == 20 else _tone(dest, 2.0, 220))

    monkeypatch.setattr(synthesize, "_extract_ref", extract)
    synthesize._resolve_profile(job, job.speakers["KAITO"], Voicebox(), tmp_path / "clips",
                                "clone", {"KAITO": {"reference": "lively"}}, None)
    assert job.metrics["clone_references"]["KAITO"]["cue"] == job.segments[1].cue_id
