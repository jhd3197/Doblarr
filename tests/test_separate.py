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
