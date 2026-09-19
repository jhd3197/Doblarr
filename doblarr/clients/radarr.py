"""Minimal Radarr API client (read-only for now)."""

from __future__ import annotations

import requests


class RadarrError(RuntimeError):
    pass


class RadarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, path: str):
        try:
            r = requests.get(f"{self.base_url}{path}",
                             headers={"X-Api-Key": self.api_key}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RadarrError(f"Radarr unreachable at {self.base_url}: {exc}")
        if r.status_code == 401:
            raise RadarrError("Radarr rejected the API key (401)")
        if not r.ok:
            raise RadarrError(f"Radarr {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    def system_status(self) -> dict:
        return self._get("/api/v3/system/status")

    def list_movies(self) -> list[dict]:
        return self._get("/api/v3/movie")
