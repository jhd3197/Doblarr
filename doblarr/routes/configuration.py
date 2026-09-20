"""Read and save application settings."""

from typing import Any

from fastapi import APIRouter

from ..config import Config
from ..errors import DoblarrError
from ..library_service import LibraryService
from ..services import Services


def build_router(config: Config, library: LibraryService,
                 services: Services) -> APIRouter:
    api = APIRouter()

    @api.get("/api/config")
    def get_config():
        return config.as_dict(redact_secrets=True)

    @api.post("/api/config")
    def post_config(changes: dict[str, Any]):
        try:
            config.apply_and_save(changes)
        except OSError as exc:
            raise DoblarrError(f"could not write {config.path}: {exc}") from exc
        library.clear()    # connection/discovery settings may have changed
        services.invalidate()  # rebuild clients with the new keys/URLs
        return {"ok": True, "saved_to": str(config.path),
                "config": config.as_dict(redact_secrets=True)}

    return api
