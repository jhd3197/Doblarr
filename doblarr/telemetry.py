"""Small, atomic run reports; no provider credentials or dialogue are recorded."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import JobCancelled


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class RunReport:
    def __init__(self, job, work: Path, dry_run: bool = False):
        self.job = job
        self.started = time.perf_counter()
        self.path = work / "reports" / f"{uuid.uuid4().hex}.json"
        self.data = {"version": 1, "started_at": datetime.now(UTC).isoformat(),
                     "input": str(job.input_file), "language": job.target_lang,
                     "locale": getattr(job, "target_locale", "") or job.target_lang,
                     "kind": job.kind, "dry_run": dry_run, "status": "running", "stages": []}
        job.report_file = self.path
        self.flush()

    def flush(self):
        self.data.update(elapsed_seconds=round(time.perf_counter() - self.started, 4),
                         counters=dict(self.job.metrics), segments=len(self.job.segments),
                         speakers=len(self.job.speakers))
        write_json(self.path, self.data)

    @contextmanager
    def stage(self, name):
        started = time.perf_counter()
        row = {"name": name, "status": "running"}
        self.data["stages"].append(row)
        self.flush()
        try:
            yield
        except BaseException as exc:
            row["status"] = "cancelled" if isinstance(exc, JobCancelled) else "failed"
            self.data["status"] = row["status"]
            raise
        else:
            row["status"] = "done"
        finally:
            row["seconds"] = round(time.perf_counter() - started, 4)
            self.flush()

    def finish(self, status="done"):
        self.data["status"] = status
        self.flush()
