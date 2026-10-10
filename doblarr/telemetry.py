"""Small, atomic run reports; no provider credentials or dialogue are recorded."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import JobCancelled
from .fileio import write_json

log = logging.getLogger("doblarr.telemetry")

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
        try:
            write_json(self.path, self.data)
        except OSError as exc:
            # A progress report that could not be written is worth a warning
            # and nothing more. Losing an hour of rendering because the run
            # log could not be saved would be the wrong trade every time.
            log.warning("could not write the run report %s: %s", self.path.name, exc)

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
