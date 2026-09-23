"""Freeing GPU memory around local model stages — a fake torch records calls."""

import contextlib
import sys
import types

import pytest

from doblarr import hardware, model_pool
from doblarr.models import DubJob


def recording_torch(devices=2, mps=False):
    calls = []
    state = {"current": 0}
    torch = types.ModuleType("torch")

    @contextlib.contextmanager
    def device(index):
        state["current"] = index
        yield

    torch.cuda = types.SimpleNamespace(
        is_available=lambda: bool(devices),
        device_count=lambda: devices,
        device=device,
        empty_cache=lambda: calls.append(("empty_cache", state["current"])),
        reset_peak_memory_stats=lambda i: calls.append(("reset", i)),
        memory_allocated=lambda i: 100 * (i + 1),
        memory_reserved=lambda i: 200 * (i + 1),
        max_memory_allocated=lambda i: 300 * (i + 1),
    )
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    torch.mps = types.SimpleNamespace(empty_cache=lambda: calls.append(("mps_empty_cache",)))
    torch.calls = calls
    return torch


@pytest.fixture
def job(tmp_path):
    return DubJob(input_file=tmp_path / "film.mkv", source_lang="ja", target_lang="es")


def test_release_empties_every_cuda_device_and_mps(monkeypatch):
    torch = recording_torch(devices=2, mps=True)
    monkeypatch.setitem(sys.modules, "torch", torch)
    model_pool.release_models()
    assert torch.calls == [("empty_cache", 0), ("empty_cache", 1), ("mps_empty_cache",)]


def test_release_without_torch_imports_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    model_pool.release_models()  # no error, nothing to free


def test_stage_releases_and_records_memory(monkeypatch, job):
    torch = recording_torch(devices=1)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with hardware.gpu_stage("separate", job, {"release_after_stage": True}):
        torch.calls.append(("stage",))
    assert torch.calls == [("reset", 0), ("stage",), ("empty_cache", 0)]
    assert job.metrics["gpu_memory"]["separate"] == {
        "cuda:0": {"peak_allocated": 300, "reserved": 200}}


def test_release_can_be_turned_off(monkeypatch, job):
    torch = recording_torch(devices=1)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with hardware.gpu_stage("diarize", job, {"release_after_stage": False}):
        pass
    assert ("empty_cache", 0) not in torch.calls
    assert "diarize" in job.metrics["gpu_memory"]


def test_kept_models_are_not_released(monkeypatch, job):
    torch = recording_torch(devices=1)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with model_pool.model("whisper", object, retain=True):
        pass
    with hardware.gpu_stage("transcribe", job, {}, retain=True):
        pass
    assert "whisper" in model_pool._MODELS
    assert ("empty_cache", 0) not in torch.calls
    model_pool.release_models()


def test_release_happens_even_when_the_stage_fails(monkeypatch, job):
    torch = recording_torch(devices=1)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with pytest.raises(RuntimeError), hardware.gpu_stage("separate", job, {}):
        raise RuntimeError("demucs crashed")
    assert ("empty_cache", 0) in torch.calls


def test_torch_loaded_during_the_stage_is_still_measured(monkeypatch, job):
    monkeypatch.setitem(sys.modules, "torch", None)
    torch = recording_torch(devices=1)
    with hardware.gpu_stage("separate", job, {}):
        monkeypatch.setitem(sys.modules, "torch", torch)  # demucs imports torch
    assert job.metrics["gpu_memory"]["separate"]["cuda:0"]["peak_allocated"] == 300
    assert ("reset", 0) not in torch.calls


def test_without_torch_the_stage_wrapper_does_nothing(monkeypatch, job):
    monkeypatch.setitem(sys.modules, "torch", None)
    with hardware.gpu_stage("separate", job, {}):
        pass
    assert "gpu_memory" not in job.metrics
    assert "torch" in sys.modules and sys.modules["torch"] is None  # never imported


def test_memory_is_logged(monkeypatch, job, caplog):
    monkeypatch.setitem(sys.modules, "torch", recording_torch(devices=1))
    with caplog.at_level("INFO", logger="doblarr.hardware"):
        with hardware.gpu_stage("separate", job, {"log_memory": True}):
            pass
        assert "separate on cuda:0" in caplog.text
        caplog.clear()
        with hardware.gpu_stage("separate", job, {"log_memory": False}):
            pass
        assert "separate on cuda:0" not in caplog.text


def test_the_pipeline_wraps_each_local_model_stage(monkeypatch, tmp_path):
    from doblarr.config import Config
    from doblarr.pipeline import run_job

    monkeypatch.chdir(tmp_path)
    torch = recording_torch(devices=1)
    monkeypatch.setitem(sys.modules, "torch", torch)
    job = DubJob(input_file=tmp_path / "movie.mkv", source_lang="ko", target_lang="es")
    run_job(job, Config.load(tmp_path / "none.yaml"), dry_run=True)
    assert set(job.metrics["gpu_memory"]) == {"separate", "transcribe", "diarize"}
    assert torch.calls.count(("empty_cache", 0)) >= 3
