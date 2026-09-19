"""Doblarr web server — serves the UI and the first real API (library scan)."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import discovery
from .clients.radarr import RadarrClient, RadarrError
from .clients.sonarr import SonarrClient, SonarrError
from .config import Config
from .jobs import JobStore, Worker

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()
    app = FastAPI(title="Doblarr", version="0.1.0")

    # Job queue + background worker (dry-run until heavy deps + voicebox are ready).
    store = JobStore(config.work_dir / "jobs.json")
    worker = Worker(store, config, dry_run=True)
    worker.start()
    app.state.jobs = store
    app.state.worker = worker

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "doblarr", "web_dir": str(WEB_DIR)}

    @app.get("/api/config")
    def get_config():
        return config.as_dict(redact_secrets=True)

    @app.post("/api/config")
    async def post_config(request: Request):
        try:
            changes = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
        if not isinstance(changes, dict):
            return JSONResponse(status_code=400, content={"error": "expected a config object"})
        try:
            config.apply_and_save(changes)
        except OSError as exc:
            return JSONResponse(status_code=500,
                                content={"error": f"could not write {config.path}: {exc}"})
        return {"ok": True, "saved_to": str(config.path),
                "config": config.as_dict(redact_secrets=True)}

    @app.get("/api/library")
    def library():
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
                series = sc.list_series()
                items += discovery.scan_sonarr(series, sc.episode_files, targets,
                    only_original_foreign=only_foreign, treat_undefined_as=undefined)
            except SonarrError as exc:
                warnings.append(f"Sonarr: {exc}")

        if not items and not warnings:
            return JSONResponse(status_code=400,
                                content={"error": "No sources configured "
                                         "(connect.radarr_* / connect.sonarr_*)."})

        discovery.sort_items(items)
        return {
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "target_languages": targets,
            "counts": discovery.summarize(items),
            "warnings": warnings,
            "items": discovery.to_dicts(items),
        }

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": store.list(), "counts": store.counts(), "paused": worker.paused}

    @app.post("/api/queue/pause")
    def pause_queue():
        worker.pause()
        return {"paused": True}

    @app.post("/api/queue/resume")
    def resume_queue():
        worker.resume()
        return {"paused": False}

    @app.post("/api/jobs")
    async def create_job(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
        title = (body or {}).get("title")
        if not title:
            return JSONResponse(status_code=400, content={"error": "title is required"})
        default_target = config["general"]["target_languages"][0]
        job = store.add(
            title=title,
            source=body.get("source", "manual"),
            source_lang=body.get("source_lang", "auto"),
            target_lang=body.get("target_lang", default_target),
            input_file=body.get("path"),
        )
        from dataclasses import asdict
        return {"ok": True, "job": asdict(job)}

    @app.post("/api/jobs/clear-finished")
    def clear_finished():
        return {"ok": True, "removed": store.clear_finished()}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        return {"ok": store.remove(job_id)}

    # Static UI last, so /api/* routes take precedence over the catch-all mount.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
