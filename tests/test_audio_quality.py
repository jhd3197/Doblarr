import math
import shutil
import struct
import wave

import pytest

from doblarr.models import DubJob, Segment
from doblarr.stages import quality


def wav(path, amplitude=0, duration=1):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(
            b"".join(
                struct.pack("<h", round(amplitude * math.sin(i * 0.2)))
                for i in range(round(16000 * duration))
            )
        )
    return path


def test_pcm_checks_silence_and_cache(tmp_path):
    seg = Segment(0, 0, 1, "hello", audio_clip=wav(tmp_path / "silent.wav"))
    issues, stats, cached = quality.check_clip(seg, "en")
    assert "silence" in issues and not cached
    assert stats["duration"] == 1
    assert quality.check_clip(seg, "en")[2]
    wav(seg.audio_clip, amplitude=5000)
    assert "silence" not in quality.check_clip(seg, "en")[0]


def test_retry_only_bad_lines_and_bound_attempts(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [
        Segment(0, 0, 1, "a", audio_clip=wav(tmp_path / "a.wav")),
        Segment(1, 2, 3, "b", audio_clip=wav(tmp_path / "b.wav", 5000)),
    ]
    retried = []

    def regenerate(seg):
        retried.append(seg.index)
        wav(seg.audio_clip, 5000)

    quality.run(job, normalize=False, regenerate=regenerate)
    assert retried == [0]
    assert job.segments[0].revision == 1
    assert not any(s.issues for s in job.segments)


def test_asr_flags_mismatch_without_modifying_script(tmp_path):
    class Voicebox:
        def transcribe(self, *a, **kw):
            return {"text": "completely unrelated words"}

    seg = Segment(0, 0, 1, "hello", audio_clip=wav(tmp_path / "a.wav", 5000))
    assert "text_mismatch" in quality.check_clip(seg, "en", Voicebox(), asr="all")[0]
    assert seg.text_src == "hello"


def test_pronunciation_uses_whole_terms_once():
    assert quality.spoken_form("Ginko met Ginkos.", {"Ginko": "Gheen-ko"}) == "Gheen-ko met Ginkos."
    assert quality.spoken_form("A B", {"A": "B", "B": "C"}) == "B C"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_normalization_keeps_raw_audio_and_full_duration(tmp_path):
    raw = wav(tmp_path / "raw.wav", 4000, 2)
    original = raw.read_bytes()
    job = DubJob(tmp_path / "movie.mkv", "en", "es")
    job.segments = [Segment(0, 0, 2, "hello", audio_clip=raw)]
    quality.run(job)
    normalized = job.segments[0].audio_clip
    assert normalized != raw and raw.read_bytes() == original
    stats = quality.inspect_pcm(normalized)
    assert stats["duration"] == pytest.approx(2, abs=0.02)
    assert stats["peak"] <= 0.81
