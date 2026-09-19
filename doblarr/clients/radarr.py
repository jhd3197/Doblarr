"""Minimal Radarr API client (read-only for now)."""

from __future__ import annotations

from ..errors import ArrClientError
from .base import ArrClient


class RadarrError(ArrClientError, RuntimeError):
    pass


class RadarrClient(ArrClient):
    service = "Radarr"
    error_cls = RadarrError

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        super().__init__(base_url, timeout=timeout,
                         headers={"X-Api-Key": api_key})
        self.api_key = api_key

    def system_status(self) -> dict:
        return self._get("/api/v3/system/status")

    def list_movies(self) -> list[dict]:
        return self._get("/api/v3/movie")
