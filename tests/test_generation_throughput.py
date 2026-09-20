import threading
import time

from doblarr.config import Config
from doblarr.model_pool import model, release_models
from doblarr.models import DubJob, Segment, Speaker
from doblarr.presets import effective_config
from doblarr.stages import synthesize


def test_queue_is_bounded_and_progress_counts_completed_lines(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "en", "es", source_audio=tmp_path / "source.wav")
    job.segments = [Segment(i, i * 3, i * 3 + 2, str(i)) for i in range(9)]
    job.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", voicebox_profile_id="voice")}
    lock = threading.Lock()
    active, peak = 0, 0

    class Voicebox:
        def synthesize_to_file(self, profile, text, language, dest, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.015)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"audio")
            with lock:
                active -= 1

    progress = []
    synthesize.run(
        job, Voicebox(), tmp_path, concurrency=2, progress=lambda done, *a: progress.append(done)
    )
    assert peak == 2
    assert progress == list(range(1, 10))
    assert all(s.audio_clip.exists() for s in job.segments)
    assert job.metrics["tts_generated"] == 9


def test_preview_preset_does_not_mutate_saved_config():
    config = Config.load("nonexistent.yaml").with_overrides({"dub.preset": "preview"})
    preview = effective_config(config)
    assert preview["separate"]["model"] == "htdemucs"
    assert preview["voicebox"]["default_engine"] == "kokoro"
    assert preview["dub"]["voice_mode"] == "preset"
    assert config["separate"]["model"] == "htdemucs_ft"


def test_model_cache_is_bounded_and_explicitly_released():
    release_models()
    loads = []

    def load():
        loads.append(1)
        return object()

    with model("a", load, retain=True) as first:
        pass
    with model("a", load, retain=True) as second:
        assert second is first
    assert len(loads) == 1
    release_models()
    with model("a", load) as third:
        assert third is not first
    assert len(loads) == 2
