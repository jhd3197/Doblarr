"""What compute this machine has, asked rather than assumed.

`probe()` reports the torch build, the GPUs it can see and which local ML
libraries are installed. It never raises: anything that goes wrong becomes a
`notes` entry, so a settings page can say *why* a run is CPU-only instead of
showing a fake GPU. When torch cannot see a GPU (absent in the slim Docker
image, or a CPU-only wheel) it falls back to `nvidia-smi`, which can at least
name the card the host has. CTranslate2 is asked separately: faster-whisper
runs on CUDA through it even next to a CPU-only torch.

Every device lists the stages that can actually use it (`usable_by`).
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import threading

log = logging.getLogger("doblarr.hardware")

LIBRARIES = ("demucs", "whisperx", "faster_whisper", "pyannote.audio")
TORCH_STAGES = ("separate", "diarize", "transcribe")

_lock = threading.Lock()
_cached: dict | None = None


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a parent package missing, or a broken stub
        return False


def _torch_probe(report: dict) -> bool:
    """Fill `report` from torch; False when torch is not importable."""
    try:
        import torch
    except ImportError:
        report["notes"].append("PyTorch is not installed, so no local model can use a GPU.")
        return False
    except Exception as exc:  # noqa: BLE001 - a broken install is still an answer
        report["notes"].append(f"PyTorch failed to import: {exc}")
        return False
    info = report["torch"]
    info.update(installed=True, version=str(getattr(torch, "__version__", "")),
                cuda=getattr(getattr(torch, "version", None), "cuda", None))
    try:
        info["cuda_available"] = bool(torch.cuda.is_available())
    except Exception as exc:  # noqa: BLE001
        report["notes"].append(f"CUDA check failed: {exc}")
        info["cuda_available"] = False
    if info["cuda_available"]:
        try:
            count = int(torch.cuda.device_count())
        except Exception as exc:  # noqa: BLE001
            report["notes"].append(f"could not count CUDA devices: {exc}")
            count = 0
        for index in range(count):
            device = {"id": f"cuda:{index}", "kind": "cuda", "index": index,
                      "usable_by": list(TORCH_STAGES)}
            try:
                props = torch.cuda.get_device_properties(index)
                device["name"] = str(props.name)
                device["total_bytes"] = int(props.total_memory)
                device["capability"] = f"{props.major}.{props.minor}"
            except Exception as exc:  # noqa: BLE001
                report["notes"].append(f"cuda:{index}: {exc}")
            try:
                free, total = torch.cuda.mem_get_info(index)
                device["free_bytes"], device["total_bytes"] = int(free), int(total)
            except Exception as exc:  # noqa: BLE001
                report["notes"].append(f"cuda:{index} memory: {exc}")
            report["devices"].append(device)
    elif not info["cuda"]:
        report["notes"].append("This PyTorch build has no CUDA support (a CPU-only wheel).")
    else:
        report["notes"].append("PyTorch was built with CUDA but sees no GPU "
                               "(driver missing or the device is not visible).")
    try:
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if mps is not None and mps.is_available():
            # CTranslate2 has no MPS backend, so transcription stays on CPU.
            report["devices"].append({"id": "mps", "kind": "mps", "index": 0,
                                      "name": "Apple GPU (MPS)",
                                      "usable_by": ["separate", "diarize"]})
    except Exception as exc:  # noqa: BLE001
        report["notes"].append(f"MPS check failed: {exc}")
    return True


def ctranslate2_cuda_devices() -> int:
    """CUDA devices CTranslate2 (faster-whisper's engine) can use; 0 when absent."""
    if not _installed("ctranslate2"):
        return 0
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001 - no CUDA runtime is an answer, not an error
        return 0


def _nvidia_smi(report: dict, ct2_devices: int = 0) -> None:
    """Name the host's NVIDIA cards when torch cannot use them."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return
    try:
        out = subprocess.run(
            [exe, "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        report["notes"].append(f"nvidia-smi failed: {exc}")
        return
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            index, total, free = int(parts[0]), int(float(parts[2])), int(float(parts[3]))
        except ValueError:
            continue
        mib = 1024 * 1024
        report["devices"].append({"id": f"cuda:{index}", "kind": "cuda", "index": index,
                                  "name": parts[1], "total_bytes": total * mib,
                                  "free_bytes": free * mib,
                                  "usable_by": ["transcribe"] if index < ct2_devices else []})
    if report["devices"]:
        report["source"] = "nvidia-smi"
        report["notes"].append(
            "A GPU is present but PyTorch cannot use it, so separation and diarization "
            "run on CPU; install a CUDA build of torch (or use the GPU Docker image)."
            + (" Transcription can still use it through faster-whisper." if ct2_devices else ""))


def _probe() -> dict:
    report: dict = {
        "source": "none",
        "torch": {"installed": False, "version": None, "cuda": None, "cuda_available": False},
        "devices": [],
        "libraries": {name: _installed(name) for name in LIBRARIES},
        "ctranslate2_cuda_devices": 0,
        "notes": [],
    }
    try:
        if _torch_probe(report):
            report["source"] = "torch"
        report["ctranslate2_cuda_devices"] = ctranslate2_cuda_devices()
        if not any(d["kind"] == "cuda" for d in report["devices"]):
            _nvidia_smi(report, report["ctranslate2_cuda_devices"])
    except Exception as exc:  # noqa: BLE001 - the probe never fails a request
        report["notes"].append(f"hardware probe failed: {exc}")
    return report


def probe() -> dict:
    """The machine's compute, probed once per process (see `refresh`)."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = _probe()
        return _cached


def refresh() -> dict:
    global _cached
    with _lock:
        _cached = None
    return probe()


def live_memory() -> list[dict]:
    """Free/total bytes per GPU right now. Not cached; empty without a GPU."""
    report = probe()
    if report["source"] == "nvidia-smi":
        fresh: dict = {"devices": [], "notes": []}
        _nvidia_smi(fresh)
        if not fresh["devices"]:
            return []
        return [{k: d.get(k) for k in ("id", "free_bytes", "total_bytes")}
                for d in fresh["devices"]]
    rows = []
    for device in report["devices"]:
        if device.get("kind") != "cuda":
            continue
        row = {"id": device["id"], "free_bytes": device.get("free_bytes"),
               "total_bytes": device.get("total_bytes")}
        try:
            import torch

            free, total = torch.cuda.mem_get_info(device["index"])
            row.update(free_bytes=int(free), total_bytes=int(total))
        except Exception as exc:  # noqa: BLE001
            log.debug("live memory for %s unavailable: %s", device["id"], exc)
        rows.append(row)
    return rows

