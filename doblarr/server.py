"""Doblarr web server — UI, REST API, SSE event stream, and *arr webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import build_api_key_dependency, build_host_check
from .clients.speech import SpeechError
from .config import Config
from .errors import ConfigError, DoblarrError, NotFoundError
from .events import EventBus
from .jobs import JobStore, Worker, import_legacy_json
from .library_service import LibraryService
from .logging_setup import attach_log_stream
from .routes import adaptive as adaptive_routes
from .routes import analysis as analysis_routes
from .routes import audio_languages as audio_language_routes
from .routes import cast as cast_routes
from .routes import characters as character_routes
from .routes import configuration as configuration_routes
from .routes import jobs as job_routes
from .routes import knowledge as knowledge_routes
from .routes import languages as language_routes
from .routes import library as library_routes
from .routes import memory as memory_routes
from .routes import narrative as narrative_routes
from .routes import packs as pack_routes
from .routes import research as research_routes
from .routes import series as series_routes
from .routes import studio as studio_routes
from .routes import templates as template_routes
from .routes import titles as title_routes
from .routes import voice_catalog as catalog_routes
from .routes import watch as watch_routes
from .scheduler import Scheduler
from .services import Services
from .store import Database

log = logging.getLogger("doblarr.server")
# The web UI is the React build (ui/, `npm run build:ui`). An installed
# package carries it as doblarr/web; a checkout serves ui/dist.
# DOBLARR_UI_DIR points at another build (the browser tests use it).
WEB_DIR = Path(__file__).resolve().parent / "web"
if not WEB_DIR.is_dir():
    WEB_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"
if os.environ.get("DOBLARR_UI_DIR"):
    WEB_DIR = Path(os.environ["DOBLARR_UI_DIR"])
SHUTDOWN_TIMEOUT = 5.0  # seconds to wait for worker/scheduler threads
SSE_HEARTBEAT = 15.0    # seconds between `: ping` comments


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()

    # Job queue + background worker; periodic rescan scheduler. Both start with
    # the app lifespan and are stopped (and joined) on shutdown.
    db = Database(config.db_path)
    from .knowledge.packs import ensure_starter_pack

    ensure_starter_pack(db, config.get("knowledge", {}))
    store = JobStore(db)
    store.reset_running()  # jobs a previous process left running resume from checkpoints
    import_legacy_json(store, config.work_dir / "jobs.json")
    bus = EventBus()
    attach_log_stream(bus)  # log records flow onto the SSE stream (topic: log)
    services = Services(config)
    worker = Worker(store, config, dry_run=config["dub"].get("dry_run", True),
                    events=bus, services=services)
    library = LibraryService(config, db, bus, services)
    scheduler = Scheduler(config, library.scan_and_label, events=bus)

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
        library.debouncer.cancel()
        db.close()

    app = FastAPI(title="Doblarr", version="0.1.0", lifespan=lifespan)
    app.middleware("http")(build_host_check(config))
    app.state.jobs = store
    app.state.worker = worker
    app.state.scheduler = scheduler
    app.state.events = bus
    app.state.services = services
    app.state.library = library

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
        """Readiness: worker alive, speech service reachable, an *arr source configured."""
        problems: list[str] = []
        if not worker.is_alive():
            problems.append("the job worker is not running; restart Doblarr")
        conn = config.get("connect", {})
        if not ((conn.get("radarr_url") and conn.get("radarr_api_key"))
                or (conn.get("sonarr_url") and conn.get("sonarr_api_key"))):
            problems.append("no library source configured "
                            "(connect.radarr_* / connect.sonarr_*)")
        try:
            speech = services.speech
            try:
                speech.health(timeout=4)
            except SpeechError as exc:
                problems.append(f"{speech.service}: {exc}")
        except ConfigError as exc:
            problems.append(str(exc))
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

    api.include_router(configuration_routes.build_router(config, library, services))
    api.include_router(library_routes.build_router(config, library, services, worker))
    api.include_router(audio_language_routes.build_router(config, library, services, worker))
    api.include_router(language_routes.build_router())
    api.include_router(knowledge_routes.build_router(config, services, db))
    api.include_router(pack_routes.build_router(config, services, db))
    api.include_router(memory_routes.build_router(db))
    api.include_router(job_routes.build_router(config, store, worker, bus))
    api.include_router(title_routes.build_router(config, db, bus, services))
    api.include_router(series_routes.build_router(config, services, store, bus))
    api.include_router(analysis_routes.build_router(config, store))
    api.include_router(studio_routes.build_router(config, store, worker, bus))
    api.include_router(catalog_routes.build_router(config, services, db))
    api.include_router(character_routes.build_router(config, services, db))
    api.include_router(narrative_routes.build_router(config, store))
    api.include_router(cast_routes.build_router(config, db))
    api.include_router(research_routes.build_router(config, store, bus))
    api.include_router(template_routes.build_router(config, db))
    api.include_router(adaptive_routes.build_router(config, store, bus))
    api.include_router(watch_routes.build_router(config, store))
    app.include_router(api)

    # index.html and the public files keep their names across updates, so they
    # revalidate ("no-cache" still answers unchanged files with a 304 from
    # their ETag); otherwise a browser could run an older page against a newer
    # server. Built assets carry a content hash in their names and never change.
    REVALIDATE = {"Cache-Control": "no-cache"}
    IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

    # SPA fallback (History API routing): any GET that isn't /api/* and doesn't
    # name a real file gets index.html. Registered before the catch-all mount;
    # /api/* misses fall through here and 404 as JSON, not HTML.
    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise NotFoundError(f"no such API route: /{full_path}")
        if full_path:
            candidate = WEB_DIR / full_path
            try:
                if candidate.is_file() and candidate.resolve().is_relative_to(
                        WEB_DIR.resolve()):
                    # Vite names built assets by content hash, so they never change.
                    headers = IMMUTABLE if full_path.startswith("assets/") else REVALIDATE
                    return FileResponse(candidate, headers=headers)  # a real asset
            except OSError:
                pass
            if "." in full_path.rsplit("/", 1)[-1]:
                raise NotFoundError(f"no such file: /{full_path}")
        return FileResponse(WEB_DIR / "index.html", headers=REVALIDATE)

    # Static mount last (HEAD requests, anything the fallback didn't take).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web UI not found at %s — run `npm run build:ui`", WEB_DIR)

    return app
