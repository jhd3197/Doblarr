"""*arr webhook helpers — event classification + a debounced rescan trigger.

Radarr and Sonarr send JSON webhooks with an `eventType`: "Test" (from the
settings page's Test button), "Download" (a file was imported — what we care
about), "Grab", deletes, renames, etc. A batch import fires many Downloads in a
row, so rescans are debounced: only one scan runs, `delay` seconds after the
last webhook in the burst.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("doblarr.webhooks")

# eventTypes that mean "a new media file landed — rescan the library".
_RESCAN_EVENTS = {"download"}


def is_test_event(payload: dict) -> bool:
    return payload.get("eventType") == "Test"


def should_rescan(payload: dict) -> bool:
    return str(payload.get("eventType", "")).lower() in _RESCAN_EVENTS


class Debouncer:
    """Coalesce a burst of triggers into one call, `delay` after the last.

    `delay` may be a callable so the value is read live from config.
    """

    def __init__(self, fn, delay: float | Callable[[], float] = 30.0):
        self._fn = fn
        self._delay = delay
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._timer is not None

    def trigger(self) -> None:
        delay = self._delay() if callable(self._delay) else self._delay
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._fn()
        except Exception:  # noqa: BLE001 - a timer thread must never die loudly
            log.exception("debounced rescan failed")

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
