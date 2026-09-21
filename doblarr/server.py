"""Doblarr web server — UI, REST API, SSE event stream, and *arr webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import build_api_key_dependency
from .clients.voicebox import VoiceboxError
from .config import Config
from .errors import DoblarrError, NotFoundError
from .events import EventBus
from .jobs import JobStore, Worker, import_legacy_json
from .library_service import LibraryService
from .logging_setup import attach_log_stream
from .routes import configuration as configuration_routes
from .routes import jobs as job_routes
from .routes import knowledge as knowledge_routes
from .routes import languages as language_routes
from .routes import library as library_routes
from .routes import packs as pack_routes
from .routes import series as series_routes
from .routes import titles as title_routes
from .routes import voice_catalog as catalog_routes
from .scheduler import Scheduler
from .services import Services
from .store import Database

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent / "web"
if not WEB_DIR.is_dir():
    WEB_DIR = Path(__file__).resolve().parent.parent / "web"
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
        """Readiness: voicebox reachable and at least one *arr source configured."""
        problems: list[str] = []
        conn = config.get("connect", {})
        if not ((conn.get("radarr_url") and conn.get("radarr_api_key"))
                or (conn.get("sonarr_url") and conn.get("sonarr_api_key"))):
            problems.append("no library source configured "
                            "(connect.radarr_* / connect.sonarr_*)")
        try:
            services.voicebox.health(timeout=4)
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

    api.include_router(configuration_routes.build_router(config, library, services))
    api.include_router(library_routes.build_router(config, library, services, worker))
    api.include_router(language_routes.build_router())
    api.include_router(knowledge_routes.build_router(config, services, db))
    api.include_router(pack_routes.build_router(config, services, db))
    api.include_router(job_routes.build_router(config, store, worker, bus))
    api.include_router(title_routes.build_router(config, db, bus, services))
    api.include_router(series_routes.build_router(config, services, store, bus))
    api.include_router(catalog_routes.build_router(config, services, db))
    app.include_router(api)

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
                    return FileResponse(candidate)  # real asset (favicon, logo…)
            except OSError:
                pass
            if "." in full_path.rsplit("/", 1)[-1]:
                raise NotFoundError(f"no such file: /{full_path}")
        return FileResponse(WEB_DIR / "index.html")

    # Static mount last (HEAD requests, anything the fallback didn't take).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
