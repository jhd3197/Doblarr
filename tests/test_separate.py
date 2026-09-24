"""Separate stage tests — fake demucs module, no torch, no model downloads."""

import sys
import types
from pathlib import Path

from doblarr.models import DubJob
from doblarr.stages import separate


def _job(tmp_path: Path) -> DubJob:
    src = tmp_path / "clip.source.wav"
    src.write_bytes(b"audio")
    inp = tmp_path / "clip.mkv"
    inp.write_bytes(b"video")
    job = DubJob(input_file=inp, source_lang="ko", target_lang="es")
    job.source_audio = src
    return job


def _fake_demucs(monkeypatch):
    pkg = types.ModuleType("demucs")
    sep = types.ModuleType("demucs.separate")

    def main(argv):
        out = Path(argv[argv.index("-o") + 1])
        model = argv[argv.index("-n") + 1]
        track_dir = out / model / Path(argv[-1]).stem
        track_dir.mkdir(parents=True)
        (track_dir / "vocals.wav").write_bytes(b"vocals")
        (track_dir / "no_vocals.wav").write_bytes(b"bed")

    sep.main = main  # type: ignore[attr-defined]
    pkg.separate = sep  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "demucs", pkg)
    monkeypatch.setitem(sys.modules, "demucs.separate", sep)


def test_separate_writes_two_stems(tmp_path, monkeypatch):
    _fake_demucs(monkeypatch)
    job = _job(tmp_path)
    work = tmp_path / "work"
    separate.run(job, work, model="htdemucs_ft")
    assert job.vocals == work / "clip.vocals.wav"
    assert job.background == work / "clip.background.wav"
    assert job.vocals.read_bytes() == b"vocals"
    assert job.background.read_bytes() == b"bed"
    # scratch layout is cleaned up
    assert not (work / "separated" / "htdemucs_ft" / "clip.source").exists()


def test_separate_falls_back_without_demucs(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "demucs", None)
    monkeypatch.setitem(sys.modules, "demucs.separate", None)
    job = _job(tmp_path)
    separate.run(job, tmp_path / "work")
    assert job.vocals is None
    assert job.background == job.source_audio


def test_separate_dry_run_plans_paths(tmp_path):
    job = DubJob(input_file=tmp_path / "clip.mkv", source_lang="ko", target_lang="es")
    separate.run(job, tmp_path / "work", dry_run=True)
    assert job.vocals and job.vocals.name == "clip.vocals.wav"
    assert job.background and job.background.name == "clip.background.wav"


def test_separate_passes_the_chosen_device(tmp_path, monkeypatch):
    _fake_demucs(monkeypatch)
    seen = []
    real = sys.modules["demucs.separate"].main

    def main(argv):
        seen.append(argv)
        real(argv)

    sys.modules["demucs.separate"].main = main
    job = _job(tmp_path)
    separate.run(job, tmp_path / "work", compute={"device": "cpu"})
    argv = seen[0]
    assert argv[argv.index("-d") + 1] == "cpu"
    assert job.metrics["devices"] == {"separate": "cpu"}


def test_separate_cache_survives_a_device_change(tmp_path, monkeypatch):
    _fake_demucs(monkeypatch)
    calls = []
    real = sys.modules["demucs.separate"].main
    sys.modules["demucs.separate"].main = lambda argv: (calls.append(argv), real(argv))
    job = _job(tmp_path)
    separate.run(job, tmp_path / "work", compute={"device": "cpu"})
    again = DubJob(input_file=job.input_file, source_lang="ko", target_lang="es")
    again.source_audio = job.source_audio
    separate.run(again, tmp_path / "work", compute={"device": "auto"})
    assert len(calls) == 1


# -- long files, in windows ---------------------------------------------------

import wave  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from doblarr.errors import JobCancelled  # noqa: E402

RATE = separate.CHUNK_RATE


@pytest.mark.parametrize("total, chunk, overlap, expected", [
    (300, 100, 10, [(0, 100, 110), (100, 200, 210), (200, 300, 300)]),   # exact multiple
    (305, 100, 10, [(0, 100, 110), (100, 200, 210), (200, 305, 305)]),   # short tail folded
    (325, 100, 10, [(0, 100, 110), (100, 200, 210), (200, 300, 310), (300, 325, 325)]),
    (80, 100, 10, [(0, 80, 80)]),                                         # shorter than one
    (0, 100, 10, []),
])
def test_chunk_plan_edges(total, chunk, overlap, expected):
    plan = separate.chunk_plan(total, chunk, overlap)
    assert plan == expected
    if plan:  # owned windows tile the track exactly
        assert plan[0][0] == 0 and plan[-1][1] == total
        assert all(a[1] == b[0] for a, b in zip(plan, plan[1:], strict=False))


def _signal(frames):
    t = np.arange(frames)
    left = 12000 * np.sin(2 * np.pi * 220 * t / RATE)
    right = 8000 * np.sin(2 * np.pi * 330 * t / RATE + 1)
    return np.stack([left, right], axis=1).round().astype("<i2")


def _write_wav(path, data):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(data.tobytes())


def _read_wav(path):
    with wave.open(str(path), "rb") as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), "<i2").reshape(-1, 2)


def _long_job(tmp_path, seconds):
    job = _job(tmp_path)
    _write_wav(job.source_audio, _signal(int(seconds * RATE)))
    return job


def _windowed_demucs(monkeypatch, calls, fail_at=None):
    """Cut with the wave module; 'separate' each window into a copy and a half."""
    def cut(source, start, stop, dest, cancel=None):
        _write_wav(dest, _read_wav(source)[start:stop])

    def main(argv):
        piece = Path(argv[-1])
        if fail_at is not None and len(calls) == fail_at:
            raise RuntimeError("out of memory")
        calls.append(piece.stem)
        track = Path(argv[argv.index("-o") + 1]) / argv[argv.index("-n") + 1] / piece.stem
        track.mkdir(parents=True)
        data = _read_wav(piece)
        _write_wav(track / "vocals.wav", data)
        _write_wav(track / "no_vocals.wav", (data // 2).astype("<i2"))

    _fake_demucs(monkeypatch)
    sys.modules["demucs.separate"].main = main
    monkeypatch.setattr(separate, "_cut", cut)


def test_a_long_file_is_separated_in_windows_and_stitched_seamlessly(tmp_path, monkeypatch):
    calls = []
    _windowed_demucs(monkeypatch, calls)
    job = _long_job(tmp_path, 2.5)
    separate.run(job, tmp_path / "work", chunk_seconds=1.0, overlap_seconds=0.1)
    assert calls == ["0000", "0001", "0002"]  # the 0.5 s tail is longer than the overlap
    source = _read_wav(job.source_audio)
    vocals, bed = _read_wav(job.vocals), _read_wav(job.background)
    assert len(vocals) == len(bed) == len(source)
    # identical content on both sides of a join crossfades into itself: no seam
    assert np.abs(vocals.astype(int) - source.astype(int)).max() <= 1
    assert np.abs(bed.astype(int) - (source // 2).astype(int)).max() <= 1
    assert not (tmp_path / "work" / "separated").exists()  # windows cleaned up


def test_the_crossfade_blends_across_the_join(tmp_path):
    parts = [tmp_path / "a.wav", tmp_path / "b.wav"]
    _write_wav(parts[0], np.full((150, 2), 1000, "<i2"))   # owns 0-100, context 100-150
    _write_wav(parts[1], np.full((100, 2), 3000, "<i2"))   # owns 100-200
    separate.stitch([(0, 100, 150), (100, 200, 200)], parts, tmp_path / "out.wav")
    out = _read_wav(tmp_path / "out.wav")[:, 0]
    assert len(out) == 200
    assert out[99] == 1000 and out[100] == 1000 and out[150] == 3000
    assert np.all(np.diff(out[100:150].astype(int)) >= 0)  # a ramp, not a step
    assert not (tmp_path / "out.partial.wav").exists()


def test_a_failed_run_resumes_at_the_next_window(tmp_path, monkeypatch):
    calls = []
    _windowed_demucs(monkeypatch, calls, fail_at=1)
    job = _long_job(tmp_path, 3.0)
    with pytest.raises(RuntimeError, match="out of memory"):
        separate.run(job, tmp_path / "work", chunk_seconds=1.0, overlap_seconds=0.1)
    assert calls == ["0000"]
    _windowed_demucs(monkeypatch, calls)
    again = DubJob(input_file=job.input_file, source_lang="ko", target_lang="es")
    again.source_audio = job.source_audio
    separate.run(again, tmp_path / "work", chunk_seconds=1.0, overlap_seconds=0.1)
    assert calls == ["0000", "0001", "0002"]  # window 0 was not separated twice
    assert len(_read_wav(again.vocals)) == 3 * RATE


def test_cancel_stops_between_windows(tmp_path, monkeypatch):
    import threading

    calls = []
    _windowed_demucs(monkeypatch, calls)
    cancel = threading.Event()
    real = sys.modules["demucs.separate"].main

    def main(argv):
        real(argv)
        cancel.set()

    sys.modules["demucs.separate"].main = main
    job = _long_job(tmp_path, 3.0)
    with pytest.raises(JobCancelled):
        separate.run(job, tmp_path / "work", chunk_seconds=1.0, overlap_seconds=0.1,
                     cancel=cancel)
    assert calls == ["0000"]


def test_the_cache_key_includes_the_window_settings(tmp_path, monkeypatch):
    import json

    calls = []
    _windowed_demucs(monkeypatch, calls)
    job = _long_job(tmp_path, 2.5)
    separate.run(job, tmp_path / "work", chunk_seconds=1.0, overlap_seconds=0.1)
    receipt = json.loads(Path(str(job.vocals) + ".manifest.json").read_text())
    assert receipt["request"]["chunk_seconds"] == 1.0
    assert receipt["request"]["overlap_seconds"] == 0.1
    again = DubJob(input_file=job.input_file, source_lang="ko", target_lang="es")
    again.source_audio = job.source_audio
    separate.run(again, tmp_path / "work", chunk_seconds=1.2, overlap_seconds=0.1)
    assert len(calls) == 5  # new windows (1.2 s, tail folded in): separated again


def test_stems_separated_whole_survive_turning_windows_on(tmp_path, monkeypatch):
    calls = []
    _windowed_demucs(monkeypatch, calls)
    job = _long_job(tmp_path, 2.5)
    separate.run(job, tmp_path / "work", chunk_seconds=0)
    assert len(calls) == 1
    again = DubJob(input_file=job.input_file, source_lang="ko", target_lang="es")
    again.source_audio = job.source_audio
    separate.run(again, tmp_path / "work", chunk_seconds=1.0)
    assert len(calls) == 1


def test_a_short_file_is_separated_in_one_piece(tmp_path, monkeypatch):
    calls = []
    _windowed_demucs(monkeypatch, calls)
    job = _long_job(tmp_path, 0.5)
    separate.run(job, tmp_path / "work", chunk_seconds=1.0)
    assert calls == ["clip.source"]


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg required")
def test_the_cut_is_frame_exact_at_the_model_rate(tmp_path):
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as out:  # 48 kHz, like the extract stage writes
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(np.zeros((48000 * 2, 2), "<i2").tobytes())
    separate._cut(source, 1000, 45100, tmp_path / "piece.wav")
    with wave.open(str(tmp_path / "piece.wav"), "rb") as piece:
        assert piece.getframerate() == RATE and piece.getnframes() == 44100
