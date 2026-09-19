"""Job cancellation tests — pipeline boundary, worker path, ffmpeg kill, route."""

import shutil
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from doblarr.config import Config
from doblarr.errors import JobCancelled
from doblarr.events import EventBus
from doblarr.jobs import JobStore, Worker
from doblarr.models import DubJob
from doblarr.pipeline import run_job
from doblarr.server import create_app


def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def _fake_run_job(dj, config, dry_run=False, on_stage=None, cancel_event=None,
                  services=None):
    """Stand-in pipeline: runs until cancelled, then raises JobCancelled."""
    for _ in range(200):
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("cancelled mid-run")
        time.sleep(0.05)
    raise AssertionError("cancel never reached the pipeline")


def test_pipeline_cancel_between_stages():
    cancel = threading.Event()
    seen: list[str] = []

    def on_stage(name, i, total):
        seen.append(name)
        if len(seen) == 2:
            cancel.set()  # cancel once the 2nd stage has started

    job = DubJob(input_file=Path("movie.mkv"), source_lang="ko", target_lang="es")
    with pytest.raises(JobCancelled):
        run_job(job, Config.load("nope.yaml"), dry_run=True,
                on_stage=on_stage, cancel_event=cancel)
    assert len(seen) == 2  # the 3rd stage never started


def test_worker_marks_job_cancelled_not_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("doblarr.jobs.run_job", _fake_run_job)
    store = JobStore(tmp_path / "jobs.db")
    bus = EventBus()
    worker = Worker(store, Config.load("nope.yaml"), events=bus)
    job = store.add(title="Godzilla", source="t", source_lang="ja", target_lang="en")
    worker.start()
    try:
        _wait_for(lambda: worker.current_id == job.id)
        assert worker.cancel(job.id)
        _wait_for(lambda: store.list()[0]["status"] == "cancelled")
    finally:
        worker.stop()
        worker.join(timeout=5)
    stored = store.list()[0]
    assert stored["status"] == "cancelled"
    assert "cancelled" in stored["message"]
    # clear-finished treats cancelled jobs as finished
    assert store.clear_finished() == 1
    # the bus saw started + cancelled
    q = bus.subscribe()
    types = [q.get_nowait()["type"] for _ in range(q.qsize())]
    assert "started" in types and "cancelled" in types


def test_delete_running_job_cancels_via_route(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("doblarr.jobs.run_job", _fake_run_job)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({}), encoding="utf-8")
    app = create_app(Config.load(p))
    with TestClient(app) as c:  # lifespan starts the worker
        jid = c.post("/api/jobs", json={"title": "Godzilla"}).json()["job"]["id"]
        _wait_for(lambda: app.state.worker.current_id == jid)
        r = c.delete(f"/api/jobs/{jid}")
        assert r.json() == {"ok": True, "cancelled": True}
        _wait_for(lambda: app.state.jobs.list()[0]["status"] == "cancelled")
        # a queued (not running) job is still removed outright
        jid2 = c.post("/api/jobs", json={"title": "Queued One"}).json()["job"]["id"]
        assert c.delete(f"/api/jobs/{jid2}").json() == {"ok": True}
        assert all(j["id"] != jid2 for j in app.state.jobs.list())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_killed_on_cancel(tmp_path):
    from doblarr.ffmpeg import run_ffmpeg
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    start = time.time()
    with pytest.raises(JobCancelled):
        run_ffmpeg(["-y", "-re", "-f", "lavfi", "-i", "anullsrc", "-t", "60",
                    "-ac", "1", "-ar", "8000", str(tmp_path / "out.wav")],
                   cancel=cancel)
    assert time.time() - start < 10  # the 60s render was actually stopped
