"""Reviewed scripts retain their translations and require complete character casting."""

import json

import pytest

from doblarr.models import DubJob, Segment
from scripts.finish_episode import apply_cast, load_reviewed_script


def test_cast_assigns_each_character_and_clears_old_voice_override(tmp_path):
    job = DubJob(tmp_path / "episode.mkv", "en", "es")
    job.segments = [Segment(1, 0, 2, "Hello", voice="old-narrator"),
                    Segment(4, 3, 5, "Hi")]
    cast = {"speakers": {"NARRATOR": {"voice": "voice-a", "segments": [1]},
                          "CHARACTER": {"voice": "voice-b", "segments": [4]}}}
    apply_cast(job, cast)
    assert [s.speaker for s in job.segments] == ["NARRATOR", "CHARACTER"]
    assert [s.voice for s in job.segments] == [None, None]
    assert job.speakers["CHARACTER"].voicebox_profile_id == "voice-b"


@pytest.mark.parametrize("cast", [
    {"speakers": {"A": {"voice": "a", "segments": []}}},
    {"speakers": {"A": {"voice": "a", "segments": [1, 99]}}},
    {"speakers": {"A": {"voice": "a", "segments": [1]},
                  "B": {"voice": "b", "segments": [1]}}},
    {"speakers": {"A": {"segments": [1]}}},
])
def test_incomplete_or_ambiguous_cast_fails(tmp_path, cast):
    job = DubJob(tmp_path / "episode.mkv", "en", "es")
    job.segments = [Segment(1, 0, 2, "Hello")]
    with pytest.raises(ValueError):
        apply_cast(job, cast)


def test_reviewed_script_reuse_checks_video_and_language(tmp_path):
    job = DubJob(tmp_path / "episode.mkv", "en", "es")
    payload = {"identity": {"input": str(job.input_file.resolve()), "target_lang": "es",
                            "source_lang": "en"},
               "segments": [{"index": 8, "start": 1, "end": 2, "text_src": "Hi",
                             "text_translated": "Hola", "audio_clip": "old.wav"}]}
    script = tmp_path / "script.json"
    script.write_text(json.dumps(payload))
    load_reviewed_script(job, script)
    assert job.segments[0].text_translated == "Hola"
    assert job.segments[0].audio_clip is None
    job.target_lang = "fr"
    with pytest.raises(ValueError, match="match"):
        load_reviewed_script(job, script)


def test_reviewed_script_rejects_invalid_timing(tmp_path):
    job = DubJob(tmp_path / "episode.mkv", "en", "es")
    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "identity": {"input": str(job.input_file.resolve()), "target_lang": "es"},
        "segments": [{"index": 0, "start": 2, "end": 1, "text_src": "Hi",
                      "text_translated": "Hola"}]}))
    with pytest.raises(ValueError, match="invalid reviewed"):
        load_reviewed_script(job, script)
