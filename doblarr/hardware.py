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
from collections.abc import Mapping
from dataclasses import dataclass

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


# -- choosing a device --------------------------------------------------------

CUDA_INSTALL_HINT = (
    "install a CUDA build of PyTorch from pytorch.org (for example "
    "`pip install torch --index-url https://download.pytorch.org/whl/cu128`), "
    "or set the device back to auto")

DEVICE_KINDS = ("cpu", "cuda", "mps")


class DeviceUnavailable(RuntimeError):
    """An explicitly chosen device this machine cannot provide."""


@dataclass(frozen=True)
class Device:
    """Where a stage runs, in both the forms the libraries want.

    torch and pyannote take `torch` ("cuda:1"); CTranslate2 (faster-whisper,
    whisperx's ASR model) takes `kind` plus `index`.
    """

    kind: str = "cpu"
    index: int | None = None
    configured: str = "auto"
    reason: str = ""

    @property
    def torch(self) -> str:
        return f"{self.kind}:{self.index}" if self.kind == "cuda" and self.index is not None \
            else self.kind

    @property
    def device_index(self) -> int:
        return self.index or 0

    def __str__(self) -> str:
        return self.torch


def parse_device(value: str) -> tuple[str, int | None]:
    """"cuda:1" -> ("cuda", 1). Raises ValueError for anything unknown."""
    text = str(value or "").strip().lower()
    kind, _, index = text.partition(":")
    if kind not in DEVICE_KINDS or (index and not index.isdigit()):
        raise ValueError(f"{value!r} is not a device; use auto, cpu, cuda, cuda:N or mps")
    if index and kind != "cuda":
        if kind == "mps" and index == "0":
            return kind, None
        raise ValueError(f"{value!r} is not a device; only CUDA devices take an index")
    return kind, int(index) if index else None


def _torch_module():
    try:
        import torch
    except Exception:  # noqa: BLE001 - absent or broken both mean "no torch"
        return None
    return torch


def _torch_cuda_count(torch) -> int:
    try:
        return int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
    except Exception:  # noqa: BLE001
        return 0


def _mps_available(torch) -> bool:
    try:
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        return bool(mps is not None and mps.is_available())
    except Exception:  # noqa: BLE001
        return False


def _cuda_count(stage: str) -> tuple[int, str]:
    """CUDA devices this stage's engine can use, and what was asked."""
    torch = _torch_module()
    count = _torch_cuda_count(torch) if torch is not None else 0
    if stage == "transcribe" and not count:
        # whisper's ASR model runs on CTranslate2, which needs no CUDA torch.
        ct2 = ctranslate2_cuda_devices()
        if ct2:
            return ct2, "CTranslate2"
    if torch is None:
        return 0, "PyTorch is not installed"
    return count, (f"torch {getattr(torch, '__version__', '?')}, "
                   f"torch.version.cuda={getattr(getattr(torch, 'version', None), 'cuda', None)}")


def configured_device(stage: str, compute: Mapping | None = None,
                      legacy: str | None = None) -> str:
    """The device setting that applies to `stage`, before it is resolved."""
    compute = compute or {}
    override = str(compute.get(f"{stage}_device") or "inherit").strip().lower()
    if override != "inherit":
        return override
    if stage == "transcribe" and legacy and str(legacy).strip().lower() != "auto":
        return str(legacy).strip().lower()  # transcribe.device predates compute.*
    return str(compute.get("device") or "auto").strip().lower()


def resolve_device(stage: str, compute: Mapping | None = None,
                   legacy: str | None = None) -> Device:
    """Where `stage` runs: per-stage override, legacy transcribe.device, then
    compute.device.

    `auto` never fails: first CUDA device, else MPS, else CPU. An explicit
    GPU that is not there raises DeviceUnavailable naming what was found; a
    silent CPU fallback would turn a two-minute stage into an hour unnoticed.
    """
    configured = configured_device(stage, compute, legacy)
    if configured == "auto":
        count, _found = _cuda_count(stage)
        if count:
            return Device("cuda", 0, configured)
        torch = _torch_module()
        if torch is not None and _mps_available(torch):
            if stage == "transcribe":
                return Device("cpu", None, configured,
                              "whisper has no MPS backend (CTranslate2); using the CPU")
            return Device("mps", None, configured)
        return Device("cpu", None, configured)
    try:
        kind, index = parse_device(configured)
    except ValueError as exc:
        raise DeviceUnavailable(f"{stage}: {exc}") from None
    if kind == "cpu":
        return Device("cpu", None, configured)
    if kind == "mps":
        if stage == "transcribe":
            return Device("cpu", None, configured,
                          "whisper has no MPS backend (CTranslate2); using the CPU")
        torch = _torch_module()
        if torch is None or not _mps_available(torch):
            raise DeviceUnavailable(
                f"{stage}: device {configured} is configured, but MPS is not available "
                "on this machine; set it to auto or cpu")
        return Device("mps", None, configured)
    count, found = _cuda_count(stage)
    if not count:
        raise DeviceUnavailable(
            f"{stage}: device {configured} is configured, but CUDA is not available "
            f"({found}); {CUDA_INSTALL_HINT}")
    if index is not None and index >= count:
        raise DeviceUnavailable(
            f"{stage}: device {configured} is configured, but only {count} CUDA "
            f"device(s) are visible ({found})")
    return Device("cuda", index if index is not None else 0, configured)
