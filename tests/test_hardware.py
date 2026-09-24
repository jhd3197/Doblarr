"""Hardware probe — fake torch and nvidia-smi, no GPU needed."""

import subprocess
import sys
import types

import pytest

from doblarr import hardware


def fake_torch(devices=(), cuda="12.4", mps=False):
    """A torch stand-in that sees `devices` as (name, total, free) tuples."""
    torch = types.ModuleType("torch")
    torch.__version__ = "2.8.0"
    torch.version = types.SimpleNamespace(cuda=cuda)
    calls = []

    def props(i):
        name, total, _free = devices[i]
        return types.SimpleNamespace(name=name, total_memory=total, major=8, minor=9)

    torch.cuda = types.SimpleNamespace(
        is_available=lambda: bool(devices),
        device_count=lambda: len(devices),
        get_device_properties=props,
        mem_get_info=lambda i: (devices[i][2], devices[i][1]),
        empty_cache=lambda: calls.append("empty_cache"),
    )
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: mps, is_built=lambda: mps))
    torch.calls = calls
    return torch


@pytest.fixture(autouse=True)
def keep_cache(monkeypatch):
    """Probes here replace the cache; hand the session's stand-in back after."""
    monkeypatch.setattr(hardware, "_cached", hardware._cached)
    monkeypatch.setattr(hardware, "ctranslate2_cuda_devices", lambda: 0)


@pytest.fixture
def no_smi(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_probe_lists_each_cuda_device(monkeypatch, no_smi, count):
    gib = 1024 ** 3
    devices = [(f"RTX {i}", 24 * gib, 20 * gib) for i in range(count)]
    monkeypatch.setitem(sys.modules, "torch", fake_torch(devices, cuda="12.4" if count else None))
    report = hardware.refresh()
    assert report["source"] == "torch"
    assert report["torch"]["installed"] and report["torch"]["version"] == "2.8.0"
    assert report["torch"]["cuda_available"] is bool(count)
    assert [d["id"] for d in report["devices"]] == [f"cuda:{i}" for i in range(count)]
    for d in report["devices"]:
        assert d["total_bytes"] == 24 * gib and d["free_bytes"] == 20 * gib
        assert d["capability"] == "8.9"
        assert d["usable_by"] == ["separate", "diarize", "transcribe"]
    if not count:
        assert any("CPU-only" in note for note in report["notes"])


def test_probe_reports_mps(monkeypatch, no_smi):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None, mps=True))
    assert [d["id"] for d in hardware.refresh()["devices"]] == ["mps"]


def test_probe_is_cached_until_refresh(monkeypatch, no_smi):
    monkeypatch.setitem(sys.modules, "torch", fake_torch([("A", 1, 1)]))
    first = hardware.refresh()
    monkeypatch.setitem(sys.modules, "torch", fake_torch())
    assert hardware.probe() is first
    assert hardware.refresh()["devices"] == []


def test_without_torch_falls_back_to_nvidia_smi(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    def run(cmd, **kwargs):
        assert cmd[0] == "/usr/bin/nvidia-smi" and kwargs["timeout"]
        return types.SimpleNamespace(stdout="0, NVIDIA GeForce RTX 4090, 24564, 23000\n")

    monkeypatch.setattr(hardware.subprocess, "run", run)
    report = hardware.refresh()
    assert report["source"] == "nvidia-smi"
    assert report["torch"]["installed"] is False
    (device,) = report["devices"]
    assert device["name"] == "NVIDIA GeForce RTX 4090"
    assert device["total_bytes"] == 24564 * 1024 * 1024 and device["usable_by"] == []
    assert any("PyTorch is not installed" in n for n in report["notes"])


def test_a_cpu_torch_next_to_a_ctranslate2_gpu_can_still_transcribe(monkeypatch):
    """faster-whisper uses CUDA through CTranslate2 even when torch is a CPU wheel."""
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None))
    monkeypatch.setattr(hardware, "ctranslate2_cuda_devices", lambda: 1)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(hardware.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        stdout="0, NVIDIA GeForce RTX 4090, 24564, 23000\n"))
    report = hardware.refresh()
    assert report["torch"]["installed"] and report["source"] == "nvidia-smi"
    assert report["devices"][0]["usable_by"] == ["transcribe"]
    assert any("faster-whisper" in n for n in report["notes"])


def test_without_torch_or_nvidia_smi_is_cpu_only(monkeypatch, no_smi):
    monkeypatch.setitem(sys.modules, "torch", None)
    report = hardware.refresh()
    assert report["source"] == "none" and report["devices"] == []
    assert report["notes"]


def test_a_failing_nvidia_smi_is_a_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "nvidia-smi")

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(hardware.subprocess, "run", run)
    report = hardware.refresh()
    assert report["source"] == "none"
    assert any("nvidia-smi failed" in n for n in report["notes"])


def test_libraries_are_probed_without_importing(monkeypatch, no_smi):
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(hardware, "_installed", lambda name: name == "demucs")
    libs = hardware.refresh()["libraries"]
    assert libs == {"demucs": True, "whisperx": False, "faster_whisper": False,
                    "pyannote.audio": False}


def test_live_memory_reads_current_free(monkeypatch, no_smi):
    devices = [("A", 100, 60)]
    monkeypatch.setitem(sys.modules, "torch", fake_torch(devices))
    hardware.refresh()
    devices[0] = ("A", 100, 10)
    assert hardware.live_memory() == [{"id": "cuda:0", "free_bytes": 10, "total_bytes": 100}]


def test_capabilities_include_hardware(client_factory):
    client = client_factory()
    body = client.get("/api/capabilities").json()
    assert body["hardware"]["source"] == "none"
    assert "devices" in body["hardware"]


def test_hardware_route_refreshes(client_factory, monkeypatch, no_smi):
    client = client_factory()
    monkeypatch.setitem(sys.modules, "torch", fake_torch([("RTX", 8, 4)]))
    body = client.get("/api/hardware?refresh=1").json()
    assert [d["id"] for d in body["devices"]] == ["cuda:0"]
    assert body["memory"] == [{"id": "cuda:0", "free_bytes": 4, "total_bytes": 8}]


# -- resolve_device -----------------------------------------------------------

@pytest.mark.parametrize("compute, legacy, stage, expected", [
    ({}, None, "separate", "cuda:0"),                                   # auto
    ({"device": "cpu"}, None, "separate", "cpu"),                       # global
    ({"device": "cpu", "separate_device": "cuda:1"}, None, "separate", "cuda:1"),  # override
    ({"device": "cuda:1", "diarize_device": "inherit"}, None, "diarize", "cuda:1"),
    ({"device": "cuda:1"}, "cpu", "transcribe", "cpu"),                 # legacy wins over global
    ({"device": "cuda:1"}, "auto", "transcribe", "cuda:1"),             # legacy auto defers
    ({"transcribe_device": "cuda:1"}, "cpu", "transcribe", "cuda:1"),   # override wins over legacy
    ({"device": "cpu"}, "cuda", "separate", "cpu"),                     # legacy is transcribe-only
    ({"device": "cuda"}, None, "separate", "cuda:0"),
])
def test_resolution_order(monkeypatch, compute, legacy, stage, expected):
    monkeypatch.setitem(sys.modules, "torch", fake_torch([("A", 1, 1), ("B", 1, 1)]))
    device = hardware.resolve_device(stage, compute, legacy)
    assert device.torch == expected


def test_auto_without_a_gpu_is_cpu_and_never_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert hardware.resolve_device("separate", {"device": "auto"}).torch == "cpu"
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None))
    assert hardware.resolve_device("diarize", {}).torch == "cpu"


@pytest.mark.parametrize("torch_module", [None, "cpu-wheel"])
def test_an_explicit_missing_cuda_is_a_clear_failure(monkeypatch, torch_module):
    monkeypatch.setitem(sys.modules, "torch",
                        fake_torch(cuda=None) if torch_module else None)
    with pytest.raises(hardware.DeviceUnavailable) as err:
        hardware.resolve_device("separate", {"device": "cuda"})
    message = str(err.value)
    assert "separate" in message and "cuda" in message and "pytorch.org" in message


def test_an_index_past_the_visible_devices_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch([("A", 1, 1)]))
    with pytest.raises(hardware.DeviceUnavailable, match="only 1 CUDA"):
        hardware.resolve_device("diarize", {"diarize_device": "cuda:3"})


def test_a_nonsense_device_is_a_clear_failure(monkeypatch):
    with pytest.raises(hardware.DeviceUnavailable, match="not a device"):
        hardware.resolve_device("separate", {"device": "gpu"})


def test_mps_for_torch_stages_but_cpu_for_transcription(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None, mps=True))
    assert hardware.resolve_device("separate", {}).torch == "mps"
    auto = hardware.resolve_device("transcribe", {})
    assert auto.torch == "cpu" and "MPS" in auto.reason
    explicit = hardware.resolve_device("transcribe", {"device": "mps"})
    assert explicit.torch == "cpu"


def test_explicit_mps_without_mps_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None))
    with pytest.raises(hardware.DeviceUnavailable, match="MPS"):
        hardware.resolve_device("separate", {"device": "mps"})


def test_transcription_can_use_a_ctranslate2_gpu_next_to_cpu_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(cuda=None))
    monkeypatch.setattr(hardware, "ctranslate2_cuda_devices", lambda: 1)
    device = hardware.resolve_device("transcribe", {"device": "cuda"})
    assert (device.kind, device.device_index) == ("cuda", 0)
    assert hardware.resolve_device("separate", {}).torch == "cpu"  # torch stage: no GPU


def test_device_forms():
    device = hardware.Device("cuda", 1)
    assert (device.torch, device.kind, device.device_index, str(device)) == \
        ("cuda:1", "cuda", 1, "cuda:1")
    assert hardware.Device().torch == "cpu" and hardware.Device().device_index == 0
