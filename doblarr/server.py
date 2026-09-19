"""Doblarr web server — serves the UI and the first real API (library scan)."""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import discovery, plex_labels
from .auth import build_api_key_dependency
from .clients.plex import PlexClient, PlexError
from .clients.radarr import RadarrClient, RadarrError
from .clients.sonarr import SonarrClient, SonarrError
from .config import Config
from .errors import ConfigError, DoblarrError, NotFoundError
from .jobs import JobStore, Worker
from .scheduler import Scheduler

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
    app = FastAPI(title="Doblarr", version="0.1.0")

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

    # Job queue + background worker (dry-run until heavy deps + voicebox are ready).
    store = JobStore(config.work_dir / "jobs.json")
    worker = Worker(store, config, dry_run=True)
    worker.start()
    app.state.jobs = store
    app.state.worker = worker

    @api.get("/api/health")
    def health():
        return {"ok": True, "service": "doblarr", "web_dir": str(WEB_DIR)}

    @api.get("/api/config")
    def get_config():
        return config.as_dict(redact_secrets=True)

    @api.post("/api/config")
    def post_config(changes: dict[str, Any]):
        try:
            config.apply_and_save(changes)
        except OSError as exc:
            raise DoblarrError(f"could not write {config.path}: {exc}")
        return {"ok": True, "saved_to": str(config.path),
                "config": config.as_dict(redact_secrets=True)}

    status_state: dict = {"last_scan": None, "counts": None}

    def _scan_library() -> tuple[list, list[str]]:
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
        return items, warnings

    def _scan_and_maybe_label():
        items, _ = _scan_library()
        conn = config.get("connect", {})
        if config.get("filtering", {}).get("auto_label") and conn.get("plex_url") and conn.get("plex_token"):
            try:
                plex_labels.sync_labels(
                    items, PlexClient(conn["plex_url"], conn["plex_token"]), config, apply=True)
            except PlexError as exc:
                log.warning("auto label sync failed: %s", exc)

    scheduler = Scheduler(config, _scan_and_maybe_label)
    scheduler.start()
    app.state.scheduler = scheduler

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
    def library():
        items, warnings = _scan_library()
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
