"""A bounded cache for optional local ML models, with explicit release."""

import gc
import logging
import sys
from collections import OrderedDict
from contextlib import contextmanager, suppress

log = logging.getLogger("doblarr.model_pool")

_MODELS: OrderedDict = OrderedDict()


def empty_device_caches() -> None:
    """Hand torch's cached GPU blocks back to the driver, on every device.

    `torch.cuda.empty_cache()` only empties the current device, so a stage
    that ran on cuda:1 would otherwise keep its memory. Never imports torch:
    if no stage loaded it, there is nothing to free.
    """
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                with torch.cuda.device(index):
                    torch.cuda.empty_cache()
    except Exception as exc:  # noqa: BLE001 - freeing memory must not fail a job
        log.debug("CUDA cache release failed: %s", exc)
    with suppress(Exception):
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if mps is not None and mps.is_available():
            torch.mps.empty_cache()


def release_models():
    _MODELS.clear()
    gc.collect()
    empty_device_caches()


@contextmanager
def model(key, load, retain=False):
    if not retain:
        release_models()
    if key not in _MODELS:
        # Keep at most two models; callers opt in only with sufficient memory.
        if len(_MODELS) >= 2:
            _MODELS.popitem(last=False)
            gc.collect()
        _MODELS[key] = load()
    _MODELS.move_to_end(key)
    try:
        yield _MODELS[key]
    finally:
        if not retain:
            release_models()
