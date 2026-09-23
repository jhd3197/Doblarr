"""Read and save application settings."""

from typing import Any

from fastapi import APIRouter

from .. import delivery as export_delivery
from .. import hardware, treatments
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

    @api.get("/api/capabilities")
    def capabilities():
        """What this installation can actually do, asked rather than assumed.

        The treatment presets are probed against this FFmpeg build, so the
        settings page can mark one unavailable instead of offering a choice
        that would come back as `unsupported` after a render.
        """
        return {
            "treatments": {
                "catalogue": treatments.catalogue(),
                "filters": sorted(treatments.available_filters()),
                "note": ("A preset whose filters this FFmpeg build lacks is "
                         "recorded as asked-for and unsupported; the line stays "
                         "dry rather than receiving an approximation."),
            },
            "delivery": export_delivery.describe(
                export_delivery.settings(dict(config.get("delivery", {})))),
            "hardware": hardware.probe(),
        }

    @api.get("/api/hardware")
    def hardware_status(refresh: bool = False):
        """The probe plus live GPU memory; `refresh=1` probes again."""
        report = hardware.refresh() if refresh else hardware.probe()
        return {**report, "memory": hardware.live_memory()}

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
