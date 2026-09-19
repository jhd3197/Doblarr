"""Doblarr web server — serves the UI and the first real API (library scan)."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import queue
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import discovery, plex_labels
from .auth import build_api_key_dependency
from .cache import TTLCache
from .clients.plex import PlexClient, PlexError
from .clients.radarr import RadarrClient, RadarrError
from .clients.sonarr import SonarrClient, SonarrError
from .clients.voicebox import VoiceboxClient, VoiceboxError
from .config import Config
from .errors import ConfigError, DoblarrError, NotFoundError
from .events import EventBus
from .jobs import JobStore, Worker
from .scheduler import Scheduler

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SHUTDOWN_TIMEOUT = 5.0  # seconds to wait for worker/scheduler threads
SSE_HEARTBEAT = 15.0    # seconds between `: ping` comments


class PlexLabelsIn(BaseModel):
    apply: bool = False


class JobCreateIn(BaseModel):
    title: str = Field(min_length=1)
    source: str = "manual"
    source_lang: str = "auto"
    target_lang: str | None = None
    path: str | None = None


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()

    # Job queue + background worker; periodic rescan scheduler. Both start with
    # the app lifespan and are stopped (and joined) on shutdown.
    store = JobStore(config.work_dir / "jobs.json")
    bus = EventBus()
    worker = Worker(store, config, dry_run=config["dub"].get("dry_run", True),
                    events=bus)
    scheduler = Scheduler(config, lambda: _scan_and_maybe_label(), events=bus)
    scan_cache = TTLCache(max_size=4)
    status_state: dict = {"last_scan": None, "counts": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        scheduler.start()
        yield
        scheduler.stop()
        worker.stop()
        for thread in (scheduler, worker):
            thread.join(timeout=SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                log.warning("%s did not stop within %.0fs", thread.name, SHUTDOWN_TIMEOUT)

    app = FastAPI(title="Doblarr", version="0.1.0", lifespan=lifespan)
    app.state.jobs = store
    app.state.worker = worker
    app.state.scheduler = scheduler
    app.state.events = bus

    @app.exception_handler(DoblarrError)
    async def doblarr_error_handler(request: Request, exc: DoblarrError):
        return JSONResponse(status_code=exc.http_status,
                            content={"error": str(exc)})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": exc.detail})

    # All /api/* routes require web.api_key (when configured); the static UI
    # mount below does not.
    api = APIRouter(dependencies=[Depends(build_api_key_dependency(config))])

    @api.get("/api/health")
    def health():
        """Liveness: the process is up."""
        return {"ok": True, "service": "doblarr", "web_dir": str(WEB_DIR)}

    @api.get("/api/health/ready")
    def ready():
        """Readiness: voicebox reachable and at least one *arr source configured."""
        problems: list[str] = []
        conn = config.get("connect", {})
        if not ((conn.get("radarr_url") and conn.get("radarr_api_key"))
                or (conn.get("sonarr_url") and conn.get("sonarr_api_key"))):
            problems.append("no library source configured "
                            "(connect.radarr_* / connect.sonarr_*)")
        try:
            VoiceboxClient(config["voicebox"]["base_url"]).health(timeout=4)
        except VoiceboxError as exc:
            problems.append(f"voicebox: {exc}")
        if problems:
            return JSONResponse(status_code=503,
                                content={"ready": False, "problems": problems})
        return {"ready": True}

    @api.get("/api/events")
    async def events():
        """Server-sent events: replay recent events, then stream live ones.

        Browsers can't set headers on EventSource, so pass the API key as
        `?api_key=` (the auth dependency accepts it).
        """
        q = bus.subscribe()

        async def stream():
            try:
                while True:
                    try:
                        event = await asyncio.to_thread(q.get, timeout=SSE_HEARTBEAT)
                        yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/api/config")
    def get_config():
        return config.as_dict(redact_secrets=True)

    @api.post("/api/config")
    def post_config(changes: dict[str, Any]):
        try:
            config.apply_and_save(changes)
        except OSError as exc:
            raise DoblarrError(f"could not write {config.path}: {exc}") from exc
        scan_cache.clear()  # connection/discovery settings may have changed
        return {"ok": True, "saved_to": str(config.path),
                "config": config.as_dict(redact_secrets=True)}

    def _scan_ttl() -> float:
        try:
            return float(config.get("discovery", {}).get("cache_ttl", 300))
        except (TypeError, ValueError):
            return 300.0

    def _scan_library(force: bool = False) -> tuple[list, list[str]]:
        if not force:
            cached = scan_cache.get("library", ttl=_scan_ttl())
            if cached is not None:
                return cached
        targets = config["general"]["target_languages"]
        conn = config.get("connect", {})
        disc = config.get("discovery", {})
        only_foreign = disc.get("only_original_foreign", True)
        undefined = disc.get("treat_undefined_as", "original")
        items: list = []
        warnings: list[str] = []
        if conn.get("radarr_url") and conn.get("radarr_api_key"):
            try:
                movies = RadarrClient(conn["radarr_url"], conn["radarr_api_key"]).list_movies()
                items += discovery.scan_radarr(movies, targets,
                    only_original_foreign=only_foreign, treat_undefined_as=undefined)
            except RadarrError as exc:
                warnings.append(f"Radarr: {exc}")
        if conn.get("sonarr_url") and conn.get("sonarr_api_key"):
            try:
                sc = SonarrClient(conn["sonarr_url"], conn["sonarr_api_key"])
                items += discovery.scan_sonarr(sc.list_series(), sc.episode_files, targets,
                    only_original_foreign=only_foreign, treat_undefined_as=undefined)
            except SonarrError as exc:
                warnings.append(f"Sonarr: {exc}")
        discovery.sort_items(items)
        status_state["last_scan"] = _dt.datetime.now().isoformat(timespec="seconds")
        status_state["counts"] = discovery.summarize(items)
        result = (items, warnings)
        scan_cache.set("library", result)
        return result

    def _scan_and_maybe_label():
        items, _ = _scan_library(force=True)  # a scheduled rescan refreshes the cache
        conn = config.get("connect", {})
        if config.get("filtering", {}).get("auto_label") \
                and conn.get("plex_url") and conn.get("plex_token"):
            try:
                plex_labels.sync_labels(
                    items, PlexClient(conn["plex_url"], conn["plex_token"]), config, apply=True)
            except PlexError as exc:
                log.warning("auto label sync failed: %s", exc)
        return status_state["counts"]

    @api.get("/api/status")
    def status():
        return {
            "last_scan": status_state["last_scan"],
            "counts": status_state["counts"],
            "auto_scan": bool(config.get("discovery", {}).get("auto_scan")),
            "auto_label": bool(config.get("filtering", {}).get("auto_label")),
            "rescan_interval": config.get("discovery", {}).get("rescan_interval", "6h"),
            "queue_paused": worker.paused,
        }

    @api.get("/api/library")
    def library(refresh: bool = False):
        items, warnings = _scan_library(force=refresh)
        if not items and not warnings:
            raise ConfigError("No sources configured "
                              "(connect.radarr_* / connect.sonarr_*).")
        return {
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "target_languages": config["general"]["target_languages"],
            "counts": discovery.summarize(items),
            "warnings": warnings,
            "items": discovery.to_dicts(items),
        }

    @api.post("/api/plex/labels")
    def plex_sync_labels(body: PlexLabelsIn):
        conn = config.get("connect", {})
        if not conn.get("plex_url") or not conn.get("plex_token"):
            raise ConfigError("Plex is not configured "
                              "(connect.plex_url / connect.plex_token).")
        items, _ = _scan_library()
        plex = PlexClient(conn["plex_url"], conn["plex_token"])
        return plex_labels.sync_labels(items, plex, config, apply=body.apply)

    @api.get("/api/jobs")
    def list_jobs():
        return {"jobs": store.list(), "counts": store.counts(), "paused": worker.paused}

    @api.post("/api/queue/pause")
    def pause_queue():
        worker.pause()
        return {"paused": True}

    @api.post("/api/queue/resume")
    def resume_queue():
        worker.resume()
        return {"paused": False}

    @api.post("/api/jobs")
    def create_job(body: JobCreateIn):
        default_target = config["general"]["target_languages"][0]
        job = store.add(
            title=body.title,
            source=body.source,
            source_lang=body.source_lang,
            target_lang=body.target_lang or default_target,
            input_file=body.path,
        )
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"ok": True, "job": asdict(job)}

    @api.post("/api/jobs/clear-finished")
    def clear_finished():
        return {"ok": True, "removed": store.clear_finished()}

    @api.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        if not store.remove(job_id):
            raise NotFoundError(f"no job with id {job_id}")
        return {"ok": True}

    app.include_router(api)

    # Static UI last, so /api/* routes take precedence over the catch-all mount.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
