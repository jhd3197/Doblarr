"""One way to load faster-whisper: through the model pool, on the resolved device.

The transcribe stage, the published-dub readers and the studio's reference
transcription all load the same models. Loading them here keeps one copy in
memory (doblarr.model_pool) instead of one per caller, and gives every caller
the same device rules.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from . import hardware
from .model_pool import model as pooled_model

log = logging.getLogger("doblarr.whisper")


def _compute_type(kind: str) -> str:
    return "float16" if kind == "cuda" else "int8"


def _load(name: str, kind: str, index: int):
    from faster_whisper import WhisperModel  # lazy — heavy ML dep

    try:
        return WhisperModel(name, device=kind, device_index=index, compute_type=_compute_type(kind))
    except Exception as exc:  # noqa: BLE001 - a GPU runtime that will not start: use the CPU
        if kind == "cpu":
            raise
        log.warning("whisper: %s on %s:%d failed (%s); using the cpu", name, kind, index, exc)
        return WhisperModel(name, device="cpu", compute_type="int8")


@contextmanager
def loaded(name: str, device: hardware.Device | None = None, keep: bool = False) -> Iterator:
    """A faster-whisper model on `device`; released afterwards unless `keep`."""
    device = device or hardware.Device()
    # CTranslate2 runs on CUDA or the CPU; an Apple GPU falls back to the CPU.
    kind = device.kind if device.kind in ("cuda", "cpu") else "cpu"
    index = device.device_index if kind == "cuda" else 0
    with pooled_model(("faster-whisper", name, kind, index), lambda: _load(name, kind, index),
                      keep) as model:
        yield model
