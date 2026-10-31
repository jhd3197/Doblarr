"""faster-whisper is loaded through the pool, on a device it can use."""

import sys
import types

from doblarr import hardware, model_pool, whisper


def _fake_faster_whisper(monkeypatch, fail_on=()):
    made = []

    class WhisperModel:
        def __init__(self, name, device="cpu", device_index=0, compute_type="int8"):
            if device in fail_on:
                raise RuntimeError(f"no {device} runtime")
            made.append((name, device, compute_type))

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return made


def test_a_gpu_that_will_not_start_falls_back_to_the_cpu(monkeypatch):
    made = _fake_faster_whisper(monkeypatch, fail_on=("cuda",))
    with whisper.loaded("small", hardware.Device(kind="cuda", index=0)):
        pass
    assert made == [("small", "cpu", "int8")]


def test_an_apple_gpu_runs_whisper_on_the_cpu(monkeypatch):
    made = _fake_faster_whisper(monkeypatch)
    with whisper.loaded("small", hardware.Device(kind="mps")):
        pass
    assert made == [("small", "cpu", "int8")]


def test_a_kept_model_is_loaded_once(monkeypatch):
    made = _fake_faster_whisper(monkeypatch)
    try:
        for _ in range(3):
            with whisper.loaded("small", hardware.Device(kind="cuda", index=0), keep=True):
                pass
    finally:
        model_pool.release_models()
    assert made == [("small", "cuda", "float16")]
