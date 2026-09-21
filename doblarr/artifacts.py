"""Configuration-aware checkpoints and source-isolated working directories."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .telemetry import write_json


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def stamp(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        stat = path.stat()
        return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return None


def media_work(root: Path, job) -> Path:
    identity = digest([str(job.input_file.resolve()), job.source_lang])[:16]
    return root / "media" / identity


def receipt_path(output: Path) -> Path:
    return output.with_name(output.name + ".manifest.json")


def matches(outputs: list[Path], request: dict, force=False) -> bool:
    if force or not outputs:
        return False
    saved = read_json(receipt_path(outputs[0]))
    states = [stamp(p) for p in outputs]
    return (all(s and s["size"] > 0 for s in states)
            and saved.get("request") == request and saved.get("outputs") == states)


def record(outputs: list[Path], request: dict) -> None:
    write_json(receipt_path(outputs[0]), {"request": request,
                                         "outputs": [stamp(p) for p in outputs]})
