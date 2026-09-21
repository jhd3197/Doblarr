"""A bounded cache for optional local ML models, with explicit release."""

import gc
import sys
from collections import OrderedDict
from contextlib import contextmanager

_MODELS: OrderedDict = OrderedDict()


def release_models():
    _MODELS.clear()
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


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
