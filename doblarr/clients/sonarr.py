"""Minimal Sonarr API client (read-only for now)."""

from __future__ import annotations

from ..errors import ArrClientError
from .base import ArrClient


class SonarrError(ArrClientError, RuntimeError):
    pass


class SonarrClient(ArrClient):
    service = "Sonarr"
    error_cls = SonarrError

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        super().__init__(base_url, timeout=timeout,
                         headers={"X-Api-Key": api_key})
        self.api_key = api_key

    def system_status(self) -> dict:
        return self._get("/api/v3/system/status")

    def list_series(self) -> list[dict]:
        return self._get("/api/v3/series")

    def episode_files(self, series_id: int) -> list[dict]:
        return self._get(f"/api/v3/episodefile?seriesId={series_id}")

    def episodes(self, series_id: int) -> list[dict]:
        return self._get(f"/api/v3/episode?seriesId={series_id}")
