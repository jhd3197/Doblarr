"""Dub job queue — a persistent store (SQLite, see doblarr.store) plus a worker.

The worker runs each job through the real pipeline. Until the heavy stages
(Demucs / voicebox / WhisperX) are installed and voicebox is running, it runs in
dry-run mode: jobs still flow queued -> running -> done and record the plan, so
the queue, the Dubs page, and the Overview counts are genuinely live. Flip
`dry_run=False` once the dependencies are in place and nothing else changes.

Jobs left in "running" when the process died are re-queued at startup (the
pipeline's stage checkpointing skips already-produced artifacts on the rerun).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import JobCancelled
from .models import DubJob
from .pipeline import run_job
from .store import Database

log = logging.getLogger("doblarr.jobs")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    title: str
    source: str
    source_lang: str
    target_lang: str
    input_file: str | None = None
    status: str = "queued"     # queued | running | done | failed | cancelled
    stage: str = ""
    progress: int = 0
    message: str = ""
    kind: str = "full"           # full | tease (a dubbed first-minutes preview)
    force: bool = False        # re-run every stage, ignoring cached artifacts
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


# Job fields with a real column; anything else rides in the payload JSON.
_COLS = ("id", "title", "source", "source_lang", "target_lang", "input_file",
         "status", "stage", "progress", "message", "kind", "created_at",
         "updated_at")


class JobStore:
    """SQLite-backed queue; same interface the worker and routes already use."""

    def __init__(self, db: Database | str | Path):
        self.db = db if isinstance(db, Database) else Database(db)
        self.path = self.db.path
        self.reset_running()  # crashed mid-run jobs resume (checkpointed stages)

    def _to_job(self, row) -> Job:
        fields = {c: row[c] for c in _COLS}
        fields.update(json.loads(row["payload"] or "{}"))
        return Job(**fields)

    def reset_running(self) -> int:
        """Re-queue jobs stuck in 'running' from a previous process."""
        rows = self.db.query("SELECT id FROM jobs WHERE status = 'running'")
        for row in rows:
            self.db.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                (_now(), row["id"]))
        if rows:
            log.info("re-queued %d job(s) left running by a previous run", len(rows))
        return len(rows)

    def add(self, **kwargs) -> Job:
        job = Job(id=kwargs.pop("id", uuid.uuid4().hex[:12]), **kwargs)
        extra = {k: v for k, v in asdict(job).items() if k not in _COLS}
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(_COLS)}, payload) "
            f"VALUES ({', '.join('?' for _ in _COLS)}, ?)",
            (*[getattr(job, c) for c in _COLS], json.dumps(extra)))
        return job

    def update(self, job_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        cols = {k: v for k, v in fields.items() if k in _COLS}
        extra = {k: v for k, v in fields.items() if k not in _COLS}
        if cols:
            sets = ", ".join(f"{k} = ?" for k in cols)
            self.db.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                            (*cols.values(), job_id))
        if extra:
            row = self.db.query_one("SELECT payload FROM jobs WHERE id = ?",
                                    (job_id,))
            if row is not None:
                payload = json.loads(row["payload"] or "{}")
                payload.update(extra)
                self.db.execute("UPDATE jobs SET payload = ? WHERE id = ?",
                                (json.dumps(payload), job_id))

    def remove(self, job_id: str) -> bool:
        existed = self.db.query_one("SELECT id FROM jobs WHERE id = ?",
                                    (job_id,)) is not None
        if existed:
            self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return existed

    def clear_finished(self) -> int:
        rows = self.db.query(
            "SELECT id FROM jobs WHERE status IN ('done', 'failed', 'cancelled')")
        for row in rows:
            self.db.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
        return len(rows)

    def next_queued(self) -> Job | None:
        row = self.db.query_one(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1")
        return self._to_job(row) if row else None

    def get(self, job_id: str) -> Job | None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._to_job(row) if row else None

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM jobs ORDER BY created_at DESC")
        return [asdict(self._to_job(r)) for r in rows]

    def counts(self) -> dict:
        c = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        for row in self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            c[row["status"]] = row["n"]
        return c

    def close(self) -> None:
        self.db.close()


def import_legacy_json(store: JobStore, json_path: str | Path) -> int:
    """One-shot import of a pre-SQLite jobs.json; renames it when done."""
    json_path = Path(json_path)
    if not json_path.exists() or store.list():  # only import into an empty queue
        return 0
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("could not import legacy %s: %s", json_path, exc)
        return 0
    imported = 0
    for j in data:
        try:
            store.add(**j)
            imported += 1
        except TypeError as exc:
            log.warning("skipping legacy job %s: %s", j.get("id"), exc)
    json_path.rename(json_path.with_suffix(".json.migrated"))
    log.info("imported %d job(s) from %s (renamed to .migrated)",
             imported, json_path.name)
    return imported


class Worker(threading.Thread):
    daemon = True

    def __init__(self, store: JobStore, config, dry_run: bool = True, events=None,
                 services=None):
        super().__init__(name="doblarr-worker")
        self.store = store
        self.config = config
        self.dry_run = dry_run
        self.events = events
        self.services = services
        self._stop_evt = threading.Event()
        self._pause = threading.Event()
        self._current_id: str | None = None
        self._cancel_evt: threading.Event | None = None

    def stop(self) -> None:
        self._stop_evt.set()
        if self._cancel_evt is not None:
            self._cancel_evt.set()  # unblock a running job so shutdown joins quickly

    def cancel(self, job_id: str) -> bool:
        """Ask the currently running job to stop. True if it was the running one."""
        if self._current_id == job_id and self._cancel_evt is not None:
            self._cancel_evt.set()
            return True
        return False

    @property
    def current_id(self) -> str | None:
        return self._current_id

    def _publish(self, job: Job, type_: str, **extra) -> None:
        if self.events:
            self.events.publish("job", {"type": type_, "job_id": job.id,
                                        "title": job.title, **extra})

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def run(self) -> None:
        log.info("worker started (dry_run=%s)", self.dry_run)
        while not self._stop_evt.is_set():
            if self._pause.is_set():
                self._stop_evt.wait(0.5)
                continue
            job = self.store.next_queued()
            if job is None:
                self._stop_evt.wait(1.0)
                continue
            self._process(job)

    def _process(self, job: Job) -> None:
        def on_stage(name: str, i: int, total: int) -> None:
            self.store.update(job.id, status="running", stage=name,
                              progress=int(i / total * 100))
            self._publish(job, "stage", stage=name, progress=int(i / total * 100))

        # Read live so toggling dub.dry_run in Settings applies without a restart.
        dry_run = self.config.get("dub", {}).get("dry_run", self.dry_run)
        cancel_evt = threading.Event()
        self._current_id = job.id
        self._cancel_evt = cancel_evt
        self.store.update(job.id, status="running", stage="probe", progress=0)
        self._publish(job, "started")
        try:
            dj = DubJob(
                input_file=Path(job.input_file) if job.input_file else Path(job.title),
                source_lang=job.source_lang,
                target_lang=job.target_lang,
            )
            run_job(dj, self.config, dry_run=dry_run, on_stage=on_stage,
                    cancel_event=cancel_evt, services=self.services,
                    force=job.force)
            out = str(dj.output_file) if dj.output_file else "(planned)"
            message = f"{'planned' if dry_run else 'dubbed'} -> {out}"
            self.store.update(job.id, status="done", stage="mux", progress=100,
                              message=message)
            self._publish(job, "done", progress=100, message=message)
        except JobCancelled as exc:
            log.info("job %s cancelled", job.id)
            self.store.update(job.id, status="cancelled", message=str(exc))
            self._publish(job, "cancelled", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any stage failure to the UI
            log.exception("job %s failed", job.id)
            self.store.update(job.id, status="failed", message=str(exc))
            self._publish(job, "failed", message=str(exc))
        finally:
            self._current_id = None
            self._cancel_evt = None
