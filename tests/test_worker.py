"""Worker reliability: failures, shutdown, claiming, and the per-job config copy."""

import json
import threading
import time
from types import SimpleNamespace

from doblarr.config import Config
from doblarr.errors import JobCancelled
from doblarr.events import EventBus
from doblarr.jobs import JobStore, Worker


def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _add(store: JobStore, title: str = "Harbor Lights"):
    return store.add(title=title, source="t", source_lang="ja", target_lang="en")


def _events(bus_queue) -> list[dict]:
    return [bus_queue.get_nowait() for _ in range(bus_queue.qsize())]


def test_failed_job_records_progress_then_failure(tmp_path, monkeypatch):
    progress: list[int] = []

    def failing_run_job(dj, config, on_stage=None, on_progress=None, **kw):
        on_stage("separate", 1, 4)
        progress.append(store.get(job.id).progress)
        on_progress("separate", 0.5, "half")
        progress.append(store.get(job.id).progress)
        raise RuntimeError("boom")

    monkeypatch.setattr("doblarr.jobs.run_job", failing_run_job)
    store = JobStore(tmp_path / "jobs.db")
    bus = EventBus()
    q = bus.subscribe()
    job = _add(store)
    Worker(store, Config({"dub": {}}), events=bus)._process(job)

    stored = store.get(job.id)
    assert stored.status == "failed"
    assert stored.message == "boom"
    assert progress == [25, 37]
    types = [e["type"] for e in _events(q) if e["topic"] == "job"]
    assert types == ["started", "stage", "progress", "failed"]


def test_review_ready_published_when_a_failed_run_left_a_report(tmp_path, monkeypatch):
    def run_job(dj, config, **kw):
        dj.report_file = tmp_path / "report.json"
        raise RuntimeError("late failure")

    monkeypatch.setattr("doblarr.jobs.run_job", run_job)
    store = JobStore(tmp_path / "jobs.db")
    bus = EventBus()
    q = bus.subscribe()
    job = _add(store)
    Worker(store, Config({"dub": {}}), events=bus)._process(job)

    assert store.get(job.id).report_file == str(tmp_path / "report.json")
    types = [e["type"] for e in _events(q) if e["topic"] == "job"]
    assert types[-1] == "review_ready"


def test_worker_survives_an_error_outside_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr("doblarr.jobs.WORKER_BACKOFF", 0.01)
    monkeypatch.setattr("doblarr.jobs.run_job", lambda dj, config, **kw: None)
    store = JobStore(tmp_path / "jobs.db")
    real_claim = store.claim_next
    calls = {"n": 0}

    def flaky_claim():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_claim()

    store.claim_next = flaky_claim  # type: ignore[method-assign]
    job = _add(store)
    worker = Worker(store, Config({"dub": {}}))
    worker.start()
    try:
        _wait_for(lambda: store.get(job.id).status == "done")
        assert worker.is_alive()
    finally:
        worker.stop()
        worker.join(timeout=5)


def test_unknown_payload_fields_do_not_stop_the_queue(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    store.db.execute("UPDATE jobs SET payload = ? WHERE id = ?",
                     (json.dumps({"from_a_newer_version": 1}), job.id))
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == "running"


def test_claim_is_atomic_across_stores(tmp_path):
    first = JobStore(tmp_path / "jobs.db")
    second = JobStore(tmp_path / "jobs.db")
    job = _add(first)
    assert first.claim_next().id == job.id
    assert second.claim_next() is None


def test_opening_a_store_leaves_running_jobs_alone(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    store.update(job.id, status="running")
    JobStore(store.db)      # what a studio task does mid-run
    assert store.get(job.id).status == "running"
    assert store.reset_running() == 1
    assert store.get(job.id).status == "queued"


def test_shutdown_requeues_the_running_job(tmp_path, monkeypatch):
    def run_until_cancelled(dj, config, cancel_event=None, **kw):
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise JobCancelled("stopped")

    monkeypatch.setattr("doblarr.jobs.run_job", run_until_cancelled)
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    worker = Worker(store, Config({"dub": {}}))
    worker.start()
    _wait_for(lambda: worker.current_id == job.id)
    worker.stop()
    worker.join(timeout=5)
    assert store.get(job.id).status == "queued"


def test_user_cancel_still_cancels(tmp_path, monkeypatch):
    def run_until_cancelled(dj, config, cancel_event=None, **kw):
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise JobCancelled("cancelled by user")

    monkeypatch.setattr("doblarr.jobs.run_job", run_until_cancelled)
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    worker = Worker(store, Config({"dub": {}}))
    worker.start()
    try:
        _wait_for(lambda: worker.current_id == job.id)
        worker.cancel(job.id)
        _wait_for(lambda: store.get(job.id).status == "cancelled")
    finally:
        worker.stop()
        worker.join(timeout=5)


def test_settings_saved_mid_run_do_not_reach_the_running_job(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    seen: dict = {}

    def run_job(dj, config, **kw):
        started.set()
        release.wait(5)
        seen["engine"] = config["voicebox"]["default_engine"]

    monkeypatch.setattr("doblarr.jobs.run_job", run_job)
    store = JobStore(tmp_path / "jobs.db")
    live = Config({"dub": {}, "voicebox": {"default_engine": "first"}})
    job = _add(store)
    worker = threading.Thread(target=Worker(store, live)._process, args=(job,))
    worker.start()
    assert started.wait(5)
    live._data["voicebox"]["default_engine"] = "second"     # a Settings save
    release.set()
    worker.join(timeout=5)
    assert seen["engine"] == "first"


def test_unexpected_plex_error_does_not_fail_a_finished_dub(tmp_path, monkeypatch):
    monkeypatch.setattr("doblarr.jobs.run_job", lambda dj, config, **kw: None)

    def broken_find(plex, title, year=None, source=""):
        raise ValueError("Expecting value: line 1 column 1")   # an HTML 200 from a proxy

    monkeypatch.setattr("doblarr.jobs.find_item", broken_find)
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    worker = Worker(store, Config({"dub": {"dry_run": False}}),
                    services=SimpleNamespace(plex=object()))
    worker._process(job)
    assert store.get(job.id).status == "done"


def test_a_one_voice_dub_says_so(tmp_path, monkeypatch):
    def run_job(dj, config, **kw):
        dj.metrics["diarize"] = {"method": "narrator", "reason": "no HF_TOKEN"}

    monkeypatch.setattr("doblarr.jobs.run_job", run_job)
    store = JobStore(tmp_path / "jobs.db")
    job = _add(store)
    config = Config({"dub": {"dry_run": False}, "plex": {"auto_refresh": False}})
    Worker(store, config)._process(job)
    stored = store.get(job.id)
    assert stored.status == "done"
    assert stored.message.endswith("every line in one voice: no HF_TOKEN")
