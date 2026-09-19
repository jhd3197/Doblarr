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

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()
    app = FastAPI(title="Doblarr", version="0.1.0")

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

    # Static UI last, so /api/* routes take precedence over the catch-all mount.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
