"""Keep the work folder from growing without bound.

Each render can leave a full watch copy of the video, a work folder per edit
set and a run report, and nothing else ever deletes them. This trims what can
be made again, newest first, and checks for disk space before a large write.
Saved dub versions are only trimmed when `dub.versions_kept` asks for it:
they are what a person chose to keep.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from .errors import DoblarrError

log = logging.getLogger("doblarr.retention")

DAY = 86400.0


class DiskFull(DoblarrError):
    """There is not enough free space for the next write."""

    http_status = 507


def _newest_first(entries: list[Path]) -> list[Path]:
    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(entries, key=mtime, reverse=True)


def prune(entries: list[Path], keep: int, older_than_days: float = 0.0,
          protected: set[Path] | None = None) -> int:
    """Delete all but the newest `keep` of `entries` that are also old enough.

    `protected` paths are never deleted. Returns how many were removed.
    """
    if keep <= 0:
        return 0
    cutoff = time.time() - older_than_days * DAY
    protected = {p.resolve() for p in protected or set()}
    removed = 0
    for path in _newest_first(entries)[keep:]:
        try:
            if path.resolve() in protected or path.stat().st_mtime > cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("retention: could not remove %s: %s", path, exc)
    if removed:
        log.info("retention: removed %d old item(s) next to %s", removed, entries[0].parent)
    return removed


def tidy(config, referenced: set[Path] | None = None) -> None:
    """Trim watch copies, edit-set work folders and run reports."""
    limits = config.get("retention", {}) or {}
    work = Path(config.work_dir)
    watch = work / "cache" / "watch"
    if watch.is_dir():
        prune([p for p in watch.iterdir() if p.is_dir()], int(limits.get("watch_copies", 10)))
    for effective in work.glob("media/*/*/effective"):
        prune([p for p in effective.iterdir() if p.is_dir()],
              int(limits.get("edit_sets", 5)), older_than_days=14)
    reports = work / "reports"
    if reports.is_dir():
        prune(list(reports.glob("*.json")), int(limits.get("reports", 500)),
              protected=referenced)


def trim_versions(versions_root: Path, keep: int, current: Path | None = None) -> None:
    """Keep the newest `keep` saved versions of one output (0 keeps all)."""
    if keep > 0 and versions_root.is_dir():
        prune([p for p in versions_root.iterdir()
               if p.is_dir() and not p.name.startswith(".")], keep,
              protected={current} if current else None)


def require_space(folder: Path, needed: int) -> None:
    """Raise DiskFull when `folder` has less than `needed` bytes free."""
    folder = Path(folder)
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    free = shutil.disk_usage(folder).free
    if free < needed:
        raise DiskFull(f"only {free / 1e9:.1f} GB free on {folder}, about "
                       f"{needed / 1e9:.1f} GB needed; free some space and resume the job")
