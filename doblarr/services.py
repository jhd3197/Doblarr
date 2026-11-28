"""Lazy service clients built from Config — one construction site, easy fakes.

Routes and the pipeline ask for `services.plex` etc. instead of building
clients ad hoc; clients are cached per Services instance and `invalidate()`
drops the cache (called after a config save so new keys/URLs take effect).
Tests inject fakes by assigning into `services._cache` or subclassing.
"""

from __future__ import annotations

import logging

from .clients.marcarr import MarcarrClient
from .clients.plex import PlexClient
from .clients.radarr import RadarrClient
from .clients.sonarr import SonarrClient
from .clients.speech import SpeechClient, build_speech_client
from .config import Config
from .errors import ConfigError

log = logging.getLogger("doblarr.services")


class Services:
    def __init__(self, config: Config):
        self.config = config
        self._cache: dict = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def _conn(self, service: str, url_key: str, secret_key: str) -> tuple[str, str]:
        conn = self.config.get("connect", {})
        url, secret = conn.get(url_key), conn.get(secret_key)
        if not url or not secret:
            raise ConfigError(f"{service} is not configured "
                              f"(connect.{url_key} / connect.{secret_key}).")
        return url, secret

    @property
    def radarr(self) -> RadarrClient:
        if "radarr" not in self._cache:
            url, key = self._conn("Radarr", "radarr_url", "radarr_api_key")
            self._cache["radarr"] = RadarrClient(url, key)
        return self._cache["radarr"]

    @property
    def sonarr(self) -> SonarrClient:
        if "sonarr" not in self._cache:
            url, key = self._conn("Sonarr", "sonarr_url", "sonarr_api_key")
            self._cache["sonarr"] = SonarrClient(url, key)
        return self._cache["sonarr"]

    @property
    def plex(self) -> PlexClient:
        if "plex" not in self._cache:
            url, token = self._conn("Plex", "plex_url", "plex_token")
            self._cache["plex"] = PlexClient(url, token)
        return self._cache["plex"]

    @property
    def marcarr(self) -> MarcarrClient:
        if "marcarr" not in self._cache:
            conn = self.config.get("marcarr", {})
            if not conn.get("url"):
                raise ConfigError("Marcarr is not configured (marcarr.url / marcarr.api_key).")
            self._cache["marcarr"] = MarcarrClient(conn["url"], conn.get("api_key") or "")
        return self._cache["marcarr"]

    @property
    def speech(self) -> SpeechClient:
        """The configured speech service (voicebox or VoiceStudio)."""
        if "speech" not in self._cache:
            self._cache["speech"] = build_speech_client(self.config)
        return self._cache["speech"]
