"""Small, atomic run reports; no provider credentials or dialogue are recorded."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import JobCancelled

log = logging.getLogger("doblarr.telemetry")

# On Windows an indexer or a scanner can hold a brief handle on a file that was
# written a millisecond ago, and `os.replace` then fails with `Access is
# denied` even though nothing is wrong. The write is retried for a fraction of
# a second rather than treated as a real failure: the alternative is a whole
# render dying because a virus scanner looked at a progress report.
REPLACE_ATTEMPTS = 6
REPLACE_BACKOFF = 0.05


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_BACKOFF * (attempt + 1))


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
