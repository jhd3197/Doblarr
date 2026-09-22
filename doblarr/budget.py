"""One request budget shared by every stage that may ask for more audio.

Quality retries, timing repairs and (from Plan 06) extra candidate takes all
spend the same provider quota. Without a shared counter the nested stages
multiply: a per-line quality retry that is itself re-checked, inside a timing
repair loop that regenerates the line again. `RequestBudget` makes that cost
visible and boundable, and makes cancellation a first-class refusal reason so a
cancelled job cannot start one more generation on its way out.

`limit=0` means "unbounded, but counted" — the default, so adopting the budget
cannot change what an existing configuration renders.
"""

from __future__ import annotations

import threading


class RequestBudget:
    """A counted, optionally capped, cancellable allowance of extra requests."""

    def __init__(self, limit: int = 0, cancel: threading.Event | None = None):
        try:
            self.limit = max(0, int(limit))
        except (TypeError, ValueError):
            self.limit = 0
        self.cancel = cancel
        self.spent = 0
        self.refused = 0
        self.by_kind: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.spent >= self.limit

    @property
    def remaining(self) -> int | None:
        """Requests still allowed, or None when the budget is unbounded."""
        return None if not self.limit else max(0, self.limit - self.spent)

    def charge(self, kind: str, count: int = 1) -> bool:
        """Reserve `count` requests for `kind`. False means: do not ask for them."""
        if self.cancelled:
            return False
        with self._lock:
            if self.limit and self.spent + count > self.limit:
                self.refused += count
                return False
            self.spent += count
            self.by_kind[kind] = self.by_kind.get(kind, 0) + count
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "limit": self.limit,
                "spent": self.spent,
                "refused": self.refused,
                "by_kind": dict(self.by_kind),
                "exhausted": bool(self.limit) and self.spent >= self.limit,
            }
