"""Doblarr web server — serves the UI and the first real API (library scan)."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import discovery
from .clients.radarr import RadarrClient, RadarrError
from .config import Config

log = logging.getLogger("doblarr.server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()
    app = FastAPI(title="Doblarr", version="0.1.0")

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "doblarr", "web_dir": str(WEB_DIR)}

    @app.get("/api/library")
    def library():
        targets = config["general"]["target_languages"]
        conn = config.get("connect", {})
        disc = config.get("discovery", {})
        url, key = conn.get("radarr_url"), conn.get("radarr_api_key")
        if not url or not key:
            return JSONResponse(status_code=400,
                                content={"error": "Radarr is not configured "
                                         "(connect.radarr_url / connect.radarr_api_key)."})
        try:
            movies = RadarrClient(url, key).list_movies()
        except RadarrError as exc:
            return JSONResponse(status_code=502, content={"error": str(exc)})

        items = discovery.scan_radarr(
            movies, targets,
            only_original_foreign=disc.get("only_original_foreign", True),
            treat_undefined_as=disc.get("treat_undefined_as", "original"),
        )
        return {
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "target_languages": targets,
            "counts": discovery.summarize(items),
            "items": discovery.to_dicts(items),
        }

    # Static UI last, so /api/* routes take precedence over the catch-all mount.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        log.warning("web dir not found at %s — UI will not be served", WEB_DIR)

    return app
