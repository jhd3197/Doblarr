"""Periodic library rescan (and optional Plex label sync).

Runs `tick_fn` every `discovery.rescan_interval` while `discovery.auto_scan` is on.
Both flags are read live from config, so toggling them in Settings takes effect
without a restart. Auto-labeling is opt-in — the tick only writes to Plex when
`filtering.auto_label` is enabled (enforced by the caller's tick_fn).
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("doblarr.scheduler")


def parse_interval(value, default: int = 6 * 3600) -> int:
    """'6h' / '30m' / '90s' / '3600' -> seconds (minimum 60)."""
    s = str(value or "").strip().lower()
    try:
        if s.endswith("h"):
            n = float(s[:-1]) * 3600
        elif s.endswith("m"):
            n = float(s[:-1]) * 60
        elif s.endswith("s"):
            n = float(s[:-1])
        else:
            n = float(s)
    except ValueError:
        return default
    return max(60, int(n))


class Scheduler(threading.Thread):
    daemon = True

    def __init__(self, config, tick_fn, events=None):
        super().__init__(name="doblarr-scheduler")
        self.config = config
        self.tick_fn = tick_fn
        self.events = events
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def _interval(self) -> int:
        return parse_interval(self.config.get("discovery", {}).get("rescan_interval", "6h"))

    def _tick(self) -> None:
        if self.events:
            self.events.publish("scan", {"type": "started"})
        result = self.tick_fn()
        if self.events:
            payload: dict = {"type": "completed"}
            if isinstance(result, dict):
                payload["counts"] = result
            self.events.publish("scan", payload)

    def run(self) -> None:
        log.info("scheduler started")
        while not self._stop_evt.is_set():
            if self.config.get("discovery", {}).get("auto_scan"):
                try:
                    self._tick()
                except Exception:  # noqa: BLE001 - never let the loop die
                    log.exception("scheduled scan failed")
            # Interruptible sleep for the current interval.
            self._stop_evt.wait(self._interval())
