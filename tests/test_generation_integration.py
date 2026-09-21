import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from doblarr.config import Config
from doblarr.models import DubJob, Speaker
from doblarr.pipeline import run_job
from doblarr.stages import diarize, separate

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def test_pipeline_resume_edit_and_audition_with_real_audio(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x64:d=7",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=200:duration=7",
            "-c:v",
            "libx264",
            "-c:a",
            "pcm_s16le",
            "-metadata:s:a:0",
            "language=eng",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    subs = tmp_path / "source.srt"
    subs.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHello.\n\n2\n00:00:04,000 --> 00:00:06,000\nGoodbye.\n",
        encoding="utf-8",
    )

    def assign(job, **kw):
        for i, seg in enumerate(job.segments):
            seg.speaker = f"S{i}"
        job.speakers = {s.speaker: Speaker(s.speaker) for s in job.segments}

    def stems(job, work, **kw):
        job.background = job.source_audio
        job.vocals = job.source_audio

    monkeypatch.setattr(diarize, "run", assign)
    monkeypatch.setattr(separate, "run", stems)
    calls = []

    class Voicebox:
        def synthesize_to_file(self, profile, text, language, dest, **kw):
            calls.append((profile, text))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(dest), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(
                    b"".join(
                        struct.pack("<h", round(5000 * math.sin(i * 0.3))) for i in range(16000)
                    )
                )

    services = SimpleNamespace(voicebox=Voicebox())
    config = Config.load(tmp_path / "config.yaml").with_overrides(
        {
            "paths.work_dir": str(tmp_path / "work"),
            "paths.output_dir": str(tmp_path / "out"),
            "dub.voice_mode": "preset",
            "dub.preset_voices": ["alice", "bob"],
            "translate.provider": "passthrough",
            "dub.max_fit_attempts": 0,
            "dub.dry_run": False,
            "quality.normalize": True,
        }
    )

    def run(cfg=config, kind="full"):
        job = DubJob(source, "en", "es", subtitle_file=subs, kind=kind)
        return run_job(job, cfg, services=services)

    first = run()
    assert calls == [("alice", "Hello."), ("bob", "Goodbye.")]
    assert first.output_file.is_file() and first.review_file.is_file()
    resumed = run()
    assert len(calls) == 2 and resumed.metrics["tts_cache_hits"] == 2
    edited = run(config.with_overrides({"dub.line_edits": {"0": {"text": "Hola."}}}))
    assert calls[-1] == ("alice", "Hola.") and len(calls) == 3
    assert edited.segments[1].text_translated == "Goodbye."
    # A later unedited run must not inherit the reviewed script.
    original = run()
    assert original.segments[0].text_translated == "Hello."
    assert len(calls) == 4
    audition = run(kind="audition")
    assert audition.output_file.suffix == ".wav"
    assert audition.metrics["audition_seconds"] < 7
    assert all(s.source_start is not None for s in audition.segments)
    result = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(first.output_file)]
        )
    )
    assert [s["codec_name"] for s in result["streams"] if s["codec_type"] == "audio"] == [
        "pcm_s16le",
        "aac",
    ]
    report = json.loads(Path(resumed.report_file).read_text())
    assert report["status"] == "done" and report["counters"]["tts_cache_hits"] == 2
