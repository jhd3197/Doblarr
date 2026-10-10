"""Atomic file writes and tolerant JSON reads, shared by every module.

A write goes to a temporary file next to the target and then replaces it, so a
crash or a full disk leaves either the old file or the new one, never half of
each. The temporary name is unique per process and thread: two writers of the
same file never write into each other's temporary file.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

# On Windows an indexer or a scanner can hold a brief handle on a file that was
# written a millisecond ago, and `os.replace` then fails with `Access is
# denied` even though nothing is wrong. The replace is retried for a fraction
# of a second rather than treated as a real failure.
REPLACE_ATTEMPTS = 6
REPLACE_BACKOFF = 0.05


def _temp_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.partial")


def write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_for(path)
    try:
        with open(temp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_BACKOFF * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, data: Any, *, indent: int | None = 2, default=None) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent, default=default))


def read_json(path: Path | None) -> dict:
    """The JSON object stored at `path`, or {} when it is missing, unreadable or not an object."""
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
