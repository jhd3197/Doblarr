"""Tiny in-memory TTL cache — dict + timestamps, FIFO eviction at max size.

Used for library scan results; intentionally dependency-free and process-local
(nothing persisted to disk).
"""

from __future__ import annotations

import threading
import time


class TTLCache:
    def __init__(self, ttl: float = 300.0, max_size: int = 64):
        self.ttl = ttl
        self.max_size = max_size
        self._items: dict = {}  # key -> (set-time, value), insertion ordered
        self._lock = threading.Lock()

    def get(self, key, ttl: float | None = None):
        """Return the cached value, or None if missing/expired.

        `ttl` overrides the instance default for this lookup, so a caller can
        keep the TTL configurable at read time.
        """
        ttl = self.ttl if ttl is None else ttl
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            ts, value = item
            if time.monotonic() - ts >= ttl:
                del self._items[key]
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            while len(self._items) > self.max_size:
                self._items.pop(next(iter(self._items)))  # evict oldest

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
