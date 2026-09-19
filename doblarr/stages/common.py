"""Shared plumbing for pipeline stages.

Two uniform short-circuits, both expressed as sentinels the `@stage` decorator
logs and swallows (the pipeline ignores stage return values; stages mutate the
job in place):

- `dry("what it would do")` — dry-run: the stage planned (job paths set) but
  skips the real work.
- `cached(artifacts, job.input_file, force)` — checkpoint/resume: if every
  declared artifact exists and is at least as fresh as the job's input file,
  the stage's work is already done and it returns the skip signal. `force`
  bypasses. Stages with in-memory outputs (transcribe/diarize/translate) don't
  declare artifacts and never skip.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable
from pathlib import Path


class DryRunPlan:
    """Returned by a stage in dry-run mode; carries what would have run."""

    def __init__(self, detail: str):
        self.detail = detail


class CachedPlan:
    """Returned by a stage whose declared artifacts are fresh enough to skip."""

    def __init__(self, artifacts: list[Path]):
        self.artifacts = artifacts


Plan = DryRunPlan | CachedPlan


def dry(detail: str) -> DryRunPlan:
    return DryRunPlan(detail)


def cached(artifacts: Path | Iterable[Path], input_file: Path,
           force: bool = False) -> CachedPlan | None:
    """A CachedPlan if all artifacts exist and are fresh vs the input, else None."""
    if force:
        return None
    paths = [Path(a) for a in ([artifacts] if isinstance(artifacts, Path) else artifacts)]
    if not paths:
        return None
    try:
        input_mtime = input_file.stat().st_mtime
    except OSError:
        return None  # can't verify freshness without the input file
    for p in paths:
        try:
            if p.stat().st_mtime < input_mtime:
                return None  # stale artifact — redo the stage
        except OSError:
            return None      # missing artifact — do the work
    return CachedPlan(paths)


def stage(name: str):
    """Log a stage's `dry(...)` / `cached(...)` sentinel and swallow it."""
    log = logging.getLogger(f"doblarr.{name}")

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, DryRunPlan):
                log.info("  [dry-run] %s", result.detail)
                return None
            if isinstance(result, CachedPlan):
                names = ", ".join(p.name for p in result.artifacts[:3])
                log.info("  skipping (cached): %s", names)
                return None
            return result
        return wrapper
    return deco
