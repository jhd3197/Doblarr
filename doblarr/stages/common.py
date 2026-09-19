"""Shared plumbing for pipeline stages: the uniform dry-run short-circuit.

A stage does its planning (mutating the job — output paths, placeholder clips)
first, then returns `dry("what it would do")` instead of doing the real work.
The `@stage` decorator logs that plan once, uniformly, and returns None, which
matches the pipeline's contract: stages mutate the job in place and their
return value is ignored.
"""

from __future__ import annotations

import functools
import logging


class DryRunPlan:
    """Returned by a stage in dry-run mode; carries what would have run."""

    def __init__(self, detail: str):
        self.detail = detail


def dry(detail: str) -> DryRunPlan:
    return DryRunPlan(detail)


def stage(name: str):
    """Log a stage's `dry(...)` plan as `[dry-run] ...` and swallow the sentinel."""
    log = logging.getLogger(f"doblarr.{name}")

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, DryRunPlan):
                log.info("  [dry-run] %s", result.detail)
                return None
            return result
        return wrapper
    return deco
