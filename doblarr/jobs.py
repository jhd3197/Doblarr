"""Dub job queue — a small persistent store plus a background worker.

The worker runs each job through the real pipeline. Until the heavy stages
(Demucs / voicebox / WhisperX) are installed and voicebox is running, it runs in
dry-run mode: jobs still flow queued -> running -> done and record the plan, so
the queue, the Dubs page, and the Overview counts are genuinely live. Flip
`dry_run=False` once the dependencies are in place and nothing else changes.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import DubJob
from .pipeline import run_job

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
    status: str = "queued"     # queued | running | done | failed
    stage: str = ""
    progress: int = 0
    message: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class JobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._jobs = {j["id"]: Job(**j) for j in data}
            except (OSError, ValueError, TypeError) as exc:
                log.warning("could not load jobs from %s: %s", self.path, exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in self._jobs.values()], indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def add(self, **kwargs) -> Job:
        with self._lock:
            job = Job(id=uuid.uuid4().hex[:12], **kwargs)
            self._jobs[job.id] = job
            self._save()
            return job

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = _now()
            self._save()

    def remove(self, job_id: str) -> bool:
        with self._lock:
            existed = self._jobs.pop(job_id, None) is not None
            if existed:
                self._save()
            return existed

    def clear_finished(self) -> int:
        with self._lock:
            ids = [i for i, j in self._jobs.items() if j.status in ("done", "failed")]
            for i in ids:
                del self._jobs[i]
            if ids:
                self._save()
            return len(ids)

    def next_queued(self) -> Job | None:
        with self._lock:
            queued = [j for j in self._jobs.values() if j.status == "queued"]
            queued.sort(key=lambda j: j.created_at)
            return queued[0] if queued else None

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [asdict(j) for j in items]

    def counts(self) -> dict:
        with self._lock:
            c = {"queued": 0, "running": 0, "done": 0, "failed": 0}
            for j in self._jobs.values():
                c[j.status] = c.get(j.status, 0) + 1
            return c


class Worker(threading.Thread):
    daemon = True

    def __init__(self, store: JobStore, config, dry_run: bool = True):
        super().__init__(name="doblarr-worker")
        self.store = store
        self.config = config
        self.dry_run = dry_run
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("worker started (dry_run=%s)", self.dry_run)
        while not self._stop.is_set():
            job = self.store.next_queued()
            if job is None:
                time.sleep(1.0)
                continue
            self._process(job)

    def _process(self, job: Job) -> None:
        def on_stage(name: str, i: int, total: int) -> None:
            self.store.update(job.id, status="running", stage=name,
                              progress=int(i / total * 100))

        self.store.update(job.id, status="running", stage="probe", progress=0)
        try:
            dj = DubJob(
                input_file=Path(job.input_file) if job.input_file else Path(job.title),
                source_lang=job.source_lang,
                target_lang=job.target_lang,
            )
            run_job(dj, self.config, dry_run=self.dry_run, on_stage=on_stage)
            out = str(dj.output_file) if dj.output_file else "(planned)"
            self.store.update(job.id, status="done", stage="mux", progress=100,
                              message=f"{'planned' if self.dry_run else 'dubbed'} -> {out}")
        except Exception as exc:  # noqa: BLE001 - surface any stage failure to the UI
            log.exception("job %s failed", job.id)
            self.store.update(job.id, status="failed", message=str(exc))
