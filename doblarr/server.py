"""Doblarr web server — UI, REST API, SSE event stream, and *arr webhooks."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import queue
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import discovery, plex_labels
from .auth import build_api_key_dependency
from .cache import TTLCache
from .clients.plex import PlexError
from .clients.radarr import RadarrError
from .clients.sonarr import SonarrError
from .clients.voicebox import VoiceboxError
from .config import Config
from .errors import ConfigError, DoblarrError, ForbiddenError, NotFoundError
from .events import EventBus
from .jobs import JobStore, Worker, import_legacy_json
from .logging_setup import attach_log_stream
from .scheduler import Scheduler
from .services import Services
from .store import Database
from .voices import CATEGORY_LABELS, cast_key
from .webhooks import Debouncer, is_test_event, should_rescan

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
    kind: Literal["full", "tease"] = "full"
    force: bool = False  # re-run every stage, ignoring cached artifacts


class CastEntryIn(BaseModel):
    speaker_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    category: str
    voice: str = ""            # voicebox profile id; "" = unassigned
    previewed: bool = False

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in CATEGORY_LABELS:
            raise ValueError(f"unknown category {v!r}")
        return v


class CastPutIn(BaseModel):
    key: str | None = None       # explicit cast key, or computed from the rest
    path: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    title: str = ""
    cast: list[CastEntryIn]


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()

    # Paths a job's output_file is allowed to resolve under (traversal guard).
    allowed_roots = [Path(os.path.normcase(str(config.output_dir.resolve()))),
                     Path(os.path.normcase(str(config.work_dir.resolve())))]

    def _allowed_path(path_str: str) -> Path | None:
        try:
            p = Path(os.path.normcase(str(Path(path_str).resolve())))
        except OSError:
            return None
        for root in allowed_roots:
            try:
                p.relative_to(root)
                return Path(path_str).resolve()
            except ValueError:
                continue
        return None

    def _ranged_response(path: Path, range_header: str | None):
        """Serve `path`, honoring `Range: bytes=...` for browser video seeking
        (starlette 0.38's FileResponse does not do ranges)."""
        media_type = {".mkv": "video/x-matroska", ".mp4": "video/mp4",
                      ".m4v": "video/mp4"}.get(path.suffix.lower(),
                                               "application/octet-stream")
        size = path.stat().st_size
        base_headers = {"Accept-Ranges": "bytes"}
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
        if not range_header:
            return FileResponse(path, media_type=media_type, headers=base_headers)
        if not m or (not m.group(1) and not m.group(2)):
            return JSONResponse(status_code=416, content={"error": "bad range"},
                                headers={"Content-Range": f"bytes */{size}"})
        start_s, end_s = m.groups()
        if not start_s:      # suffix range: last N bytes
            start = max(0, size - int(end_s))
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            return JSONResponse(status_code=416, content={"error": "range unsatisfiable"},
                                headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1

        def iterfile():
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iterfile(), status_code=206, media_type=media_type,
            headers={**base_headers,
                     "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(length)})

    # Job queue + background worker; periodic rescan scheduler. Both start with
    # the app lifespan and are stopped (and joined) on shutdown.
    db = Database(config.db_path)
    store = JobStore(db)
    import_legacy_json(store, config.work_dir / "jobs.json")
    bus = EventBus()
    attach_log_stream(bus)  # log records flow onto the SSE stream (topic: log)
    services = Services(config)
    worker = Worker(store, config, dry_run=config["dub"].get("dry_run", True),
                    events=bus, services=services)
    scheduler = Scheduler(config, lambda: _scan_and_maybe_label(), events=bus)
    scan_cache = TTLCache(max_size=4)
    status_state: dict = {"last_scan": None, "counts": None}

    # Restore the last scan so /api/status and /api/library survive a restart.
    saved_scan = db.load_scan()
    if saved_scan and saved_scan["last_scan"]:
        status_state.update(last_scan=saved_scan["last_scan"],
                            counts=saved_scan["counts"])
        scan_cache.set("library", (discovery.from_dicts(saved_scan["items"]), []))

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
        rescan_debouncer.cancel()
        db.close()

    app = FastAPI(title="Doblarr", version="0.1.0", lifespan=lifespan)
    app.state.jobs = store
    app.state.worker = worker
    app.state.scheduler = scheduler
    app.state.events = bus
    app.state.services = services

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

    @api.get("/api/config")
    def get_config():
        return config.as_dict(redact_secrets=True)

    @api.post("/api/config")
    def post_config(changes: dict[str, Any]):
        try:
            config.apply_and_save(changes)
        except OSError as exc:
            raise DoblarrError(f"could not write {config.path}: {exc}") from exc
        scan_cache.clear()    # connection/discovery settings may have changed
        services.invalidate()  # rebuild clients with the new keys/URLs
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
        disc = config.get("discovery", {})
        only_foreign = disc.get("only_original_foreign", True)
        undefined = disc.get("treat_undefined_as", "original")
        items: list = []
        warnings: list[str] = []
        try:
            movies = services.radarr.list_movies()
            items += discovery.scan_radarr(movies, targets,
                only_original_foreign=only_foreign, treat_undefined_as=undefined)
        except ConfigError:
            pass  # Radarr not configured
        except RadarrError as exc:
            warnings.append(f"Radarr: {exc}")
        try:
            sc = services.sonarr
            items += discovery.scan_sonarr(sc.list_series(), sc.episode_files, targets,
                only_original_foreign=only_foreign, treat_undefined_as=undefined)
        except ConfigError:
            pass  # Sonarr not configured
        except SonarrError as exc:
            warnings.append(f"Sonarr: {exc}")
        discovery.sort_items(items)
        status_state["last_scan"] = _dt.datetime.now().isoformat(timespec="seconds")
        status_state["counts"] = discovery.summarize(items)
        db.save_scan(status_state["last_scan"], status_state["counts"],
                     discovery.to_dicts(items))
        result = (items, warnings)
        scan_cache.set("library", result)
        return result

    def _scan_and_maybe_label():
        items, _ = _scan_library(force=True)  # a scheduled rescan refreshes the cache
        if config.get("filtering", {}).get("auto_label"):
            try:
                plex_labels.sync_labels(items, services.plex, config, apply=True)
            except ConfigError:
                pass  # Plex not configured
            except PlexError as exc:
                log.warning("auto label sync failed: %s", exc)
        return status_state["counts"]

    def _run_scan_with_events(source: str):
        bus.publish("scan", {"type": "started", "source": source})
        counts = _scan_and_maybe_label()
        bus.publish("scan", {"type": "completed", "counts": counts, "source": source})

    def _webhook_debounce() -> float:
        try:
            return float(config.get("discovery", {}).get("webhook_debounce", 30))
        except (TypeError, ValueError):
            return 30.0

    # A burst of webhooks (batch import) coalesces into one rescan.
    rescan_debouncer = Debouncer(lambda: _run_scan_with_events("webhook"),
                                 delay=_webhook_debounce)

    def _handle_arr_webhook(source: str, body: dict[str, Any]):
        event_type = body.get("eventType", "")
        if is_test_event(body):
            return {"ok": True, "test": True}
        if not should_rescan(body):
            return {"ok": True, "ignored": event_type}
        log.info("%s webhook (%s) — rescan in %.0fs",
                 source, event_type, _webhook_debounce())
        rescan_debouncer.trigger()
        return {"ok": True, "scan": "scheduled"}

    @api.post("/api/webhooks/radarr")
    def webhook_radarr(body: dict[str, Any]):
        return _handle_arr_webhook("radarr", body)

    @api.post("/api/webhooks/sonarr")
    def webhook_sonarr(body: dict[str, Any]):
        return _handle_arr_webhook("sonarr", body)

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
        items, _ = _scan_library()
        # ConfigError (Plex not configured) maps to 400; PlexError to 502.
        return plex_labels.sync_labels(items, services.plex, config, apply=body.apply)

    @api.get("/api/jobs")
    def list_jobs():
        jobs = store.list()
        for j in jobs:
            # computed server-side so the UI never guesses about the filesystem
            out = _allowed_path(j["output_file"]) if j.get("output_file") else None
            j["has_file"] = bool(out and out.exists())
        return {"jobs": jobs, "counts": store.counts(), "paused": worker.paused}

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
            kind=body.kind,
            force=body.force,
        )
        bus.publish("job", {"type": "queued", "job_id": job.id, "title": job.title})
        return {"ok": True, "job": asdict(job)}

    @api.post("/api/jobs/clear-finished")
    def clear_finished():
        return {"ok": True, "removed": store.clear_finished()}

    @api.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        """Remove a job; a RUNNING job is cancelled instead of removed."""
        if worker.cancel(job_id):
            bus.publish("job", {"type": "cancelling", "job_id": job_id})
            return {"ok": True, "cancelled": True}
        if not store.remove(job_id):
            raise NotFoundError(f"no job with id {job_id}")
        return {"ok": True}

    @api.get("/api/jobs/{job_id}/file")
    def job_file(job_id: str, request: Request):
        """Stream a job's output file (HTTP Range support for video seeking)."""
        job = store.get(job_id)
        if job is None or not job.output_file:
            raise NotFoundError(f"no output file for job {job_id}")
        path = _allowed_path(job.output_file)
        if path is None:
            raise ForbiddenError("output path is outside the configured directories")
        if not path.exists():
            raise NotFoundError("output file does not exist on disk")
        return _ranged_response(path, request.headers.get("range"))

    # -- voice casting ------------------------------------------------------
    @api.get("/api/voices")
    def list_voices():
        """Voice list: voicebox profiles when reachable, else config presets."""
        try:
            return {"source": "voicebox", "voices": services.voicebox.list_voices()}
        except VoiceboxError as exc:
            presets = config.get("dub", {}).get("preset_voices") or []
            return {"source": "config",
                    "voices": [{"id": v, "name": v} for v in presets],
                    "warning": str(exc)}

    @api.get("/api/cast")
    def get_cast(key: str | None = None, title: str | None = None,
                 path: str | None = None, tmdb_id: int | None = None,
                 tvdb_id: int | None = None):
        """Cast lookup; the key is computed server-side from whatever is given."""
        if key:
            k = key
        elif path or title or tmdb_id or tvdb_id:
            k = cast_key(title=title, path=path, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
        else:
            raise ConfigError("cast lookup needs key / path / tmdb_id / tvdb_id / title")
        saved = db.load_cast(k)
        return {"key": k, "title": (saved or {}).get("title", ""),
                "cast": (saved or {}).get("cast", [])}

    @api.put("/api/cast")
    def put_cast(body: CastPutIn):
        k = body.key or cast_key(title=body.title, path=body.path,
                                 tmdb_id=body.tmdb_id, tvdb_id=body.tvdb_id)
        db.save_cast(k, body.title, [e.model_dump() for e in body.cast])
        bus.publish("cast", {"type": "updated", "key": k})
        return {"ok": True, "key": k, "saved": len(body.cast)}

    app.include_router(api)

    # Static UI last, so /api/* routes take precedence over the catch-all mount.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
