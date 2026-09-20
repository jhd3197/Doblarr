"""Mix-stage ducking tests — graph shape, ratio parsing, and a real ffmpeg run
proving the bed is attenuated while dialogue is present."""

import shutil
import subprocess
from pathlib import Path

import pytest

from doblarr.models import DubJob, Segment
from doblarr.stages import mix

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def test_parse_ratio():
    assert mix._parse_ratio("12:1") == 12.0
    assert mix._parse_ratio("8:1") == 8.0
    assert mix._parse_ratio("garbage") == mix.DEFAULT_RATIO
    assert mix._parse_ratio("1:1") == mix.DEFAULT_RATIO  # no compression


def test_filter_graph_ducking():
    segs = [Segment(0, 1.0, 2.0, "a"), Segment(1, 2.5, 3.5, "b")]
    graph = mix._filter_graph(segs, win_start=1.0, dur=5.5, ratio=12.0)
    assert "sidechaincompress" in graph
    assert "ratio=12" in graph
    assert "adelay=0|0" in graph and "adelay=1500|1500" in graph
    assert "apad=whole_dur=5.5,asplit=2[dlg][key]" in graph  # key must not truncate
    assert graph.count("amix=inputs=2") == 2   # dialogue bus + final ducked mix


def test_filter_graph_flat_fallback():
    segs = [Segment(0, 0.0, 1.0, "a")]
    graph = mix._filter_graph(segs, win_start=0.0, dur=4.0, ratio=None)
    assert "sidechaincompress" not in graph
    assert "[bed][c0]amix=inputs=2:normalize=0[out]" in graph


def _sine(path: Path, freq: int, dur: float) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
         "-ac", "2", "-ar", "48000", str(path)],
        check=True, capture_output=True)
    return path


def _mean_volume_db(path: Path, start: float, dur: float) -> float:
    """Mean volume of the bed band (<=400 Hz) in a window, via volumedetect."""
    proc = subprocess.run(
        [FFMPEG, "-ss", str(start), "-t", str(dur), "-i", str(path),
         "-af", "lowpass=f=400,volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True)
    for line in proc.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().removesuffix(" dB"))
    raise AssertionError("volumedetect produced no mean_volume")


@needs_ffmpeg
def test_mix_ducks_bed_under_dialogue(tmp_path):
    # high-frequency clip keeps dialogue out of the measured bed band
    bed = _sine(tmp_path / "bed.wav", freq=220, dur=8.0)
    clip = _sine(tmp_path / "clip.wav", freq=4000, dur=1.0)
    src = tmp_path / "movie.mkv"
    src.write_text("fake video", encoding="utf-8")
    job = DubJob(input_file=src, source_lang="ko", target_lang="es")
    job.background = bed
    job.segments = [Segment(0, 2.0, 3.0, "a", audio_clip=clip)]

    mix.run(job, tmp_path / "work", ducking_ratio="12:1")
    out = tmp_path / "work" / "movie.es.dub.wav"
    assert out == job.dubbed_track and out.exists()

    # Original timestamps and the complete eight-second bed are preserved.
    assert mix._duration(out) == pytest.approx(8.0, abs=0.01)
    during = _mean_volume_db(out, 2.2, 0.6)
    after = _mean_volume_db(out, 5.5, 1.0)   # bed alone, past the release tail
    assert during < after - 10               # bed ducked by at least 10 dB
