"""Real remux regression: preserve originals and label the appended track."""

import json
import shutil
import subprocess

import pytest

from doblarr.models import DubJob
from doblarr.stages import mux


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_mux_two_original_audio_tracks(tmp_path):
    src = tmp_path / "input.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=1",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=1", "-map", "0:v",
        "-map", "1:a", "-map", "1:a", "-c:v", "libx264", "-c:a", "pcm_s16le",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn", str(src)],
        check=True, capture_output=True)
    dub = tmp_path / "dub.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(dub)],
                   check=True, capture_output=True)
    job = DubJob(src, "ja", "es", dubbed_track=dub)
    mux.run(job, tmp_path / "out")
    data = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_streams",
        "-of", "json", str(job.output_file)]))
    assert [s["tags"]["language"] for s in data["streams"]] == ["eng", "jpn", "spa"]
    assert data["streams"][2]["tags"]["title"] == "Spanish AI"
    assert src.exists()
    assert not list((tmp_path / "out").glob("*.partial.mkv"))


def test_mux_refuses_original_overwrite(tmp_path):
    job = DubJob(tmp_path / "movie.mkv", "ja", "es")
    with pytest.raises(ValueError, match="original"):
        mux.run(job, tmp_path)
