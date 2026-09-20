"""Library discovery, status and integration webhooks."""

import datetime as _dt
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import discovery, plex_labels
from ..config import Config
from ..errors import ConfigError
from ..jobs import Worker
from ..library_service import LibraryService
from ..services import Services


class PlexLabelsIn(BaseModel):
    apply: bool = False


def build_router(config: Config, library: LibraryService, services: Services,
                 worker: Worker) -> APIRouter:
    api = APIRouter()

    @api.post("/api/webhooks/radarr")
    def webhook_radarr(body: dict[str, Any]):
        return library.handle_webhook("radarr", body)

    @api.post("/api/webhooks/sonarr")
    def webhook_sonarr(body: dict[str, Any]):
        return library.handle_webhook("sonarr", body)

    @api.get("/api/status")
    def status():
        return {
            "last_scan": library.state["last_scan"],
            "counts": library.state["counts"],
            "auto_scan": bool(config.get("discovery", {}).get("auto_scan")),
            "auto_label": bool(config.get("filtering", {}).get("auto_label")),
            "rescan_interval": config.get("discovery", {}).get("rescan_interval", "6h"),
            "queue_paused": worker.paused,
        }

    @api.get("/api/library")
    def list_library(refresh: bool = False):
        items, warnings = library.scan(force=refresh)
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
        items, _ = library.scan()
        # ConfigError (Plex not configured) maps to 400; PlexError to 502.
        return plex_labels.sync_labels(items, services.plex, config, apply=body.apply)

    return api
