"""Library discovery, status and integration webhooks."""

import datetime as _dt
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .. import __version__, discovery, plex_labels
from ..clients.plex import PlexError
from ..config import Config
from ..errors import ConfigError, NotFoundError
from ..jobs import Worker
from ..library_service import LibraryService
from ..services import Services


def item_key(row: dict) -> str:
    """A library row's identity in title URLs; matches itemKey() in the web UI."""
    if row.get("tmdb_id"):
        return f"tmdb-{row['tmdb_id']}"
    if row.get("tvdb_id"):
        return f"tvdb-{row['tvdb_id']}"
    return f"t-{row.get('title')}" + (f"-{row['year']}" if row.get("year") else "")


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
            "worker_alive": worker.is_alive(),
            "version": __version__,
        }

    @api.get("/api/plex/thumb")
    def plex_thumb(path: str):
        """A Plex poster fetched with Doblarr's token, which never reaches the browser."""
        if not path.startswith("/library/metadata/") or ".." in path:
            raise HTTPException(422, "Only Plex artwork paths are served")
        try:
            content, media_type = services.plex.image(path)
        except (ConfigError, PlexError) as exc:
            raise HTTPException(502, f"Plex artwork unavailable: {exc}") from exc
        return Response(content=content, media_type=media_type,
                        headers={"Cache-Control": "max-age=86400"})

    def with_known_originals(rows: list[dict]) -> list[dict]:
        """Fill "??" with what a show's or film's files showed (kept, never probed here)."""
        from .. import original_language

        for row in rows:
            if row.get("original") not in ("??", "", None):
                continue
            key = original_language.cache_key(row.get("media_type") or "", row.get("tvdb_id"),
                                              row.get("tmdb_id"))
            lang = original_language.remembered(worker.store.db, key).get("lang") if key else ""
            if lang:
                row["original"] = lang
        return rows

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
            "items": with_known_originals(discovery.to_dicts(items)),
        }

    @api.get("/api/library/item/{key:path}")
    def library_item(key: str):
        """One title by its URL key (tmdb-…, tvdb-…, t-<title>[-<year>]), from the last scan."""
        items, _ = library.scan()
        for row in discovery.to_dicts(items):
            if item_key(row) == key:
                return {"item": with_known_originals([row])[0],
                        "target_languages": config["general"]["target_languages"]}
        raise NotFoundError(f"{key} is not in the current library scan")

    @api.post("/api/plex/labels")
    def plex_sync_labels(body: PlexLabelsIn):
        items, _ = library.scan()
        # ConfigError (Plex not configured) maps to 400; PlexError to 502.
        return plex_labels.sync_labels(items, services.plex, config, apply=body.apply)

    return api
