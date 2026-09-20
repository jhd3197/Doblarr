"""Shared exception hierarchy for Doblarr.

Every error the app raises on purpose derives from `DoblarrError`, which carries
an `http_status` so the web layer can map failures to consistent JSON responses
(see `doblarr.server`). Client errors additionally carry the upstream HTTP
`status` when there is one.
"""

from __future__ import annotations


class DoblarrError(Exception):
    """Base error; `http_status` is the API response code for this failure."""

    http_status: int = 500


class ConfigError(DoblarrError):
    """A required service/section is not configured (caller-fixable)."""

    http_status = 400


class NotFoundError(DoblarrError):
    """The requested object does not exist."""

    http_status = 404


class ForbiddenError(DoblarrError):
    """The request targets something outside the allowed roots."""

    http_status = 403


class JobCancelled(DoblarrError):
    """A dub job was cancelled — by the user, or by killing its ffmpeg run."""


class ArrClientError(DoblarrError):
    """An upstream *arr-style service (Radarr/Sonarr/Plex/voicebox) failed."""

    http_status = 502

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # upstream HTTP status, if any
        self.retry_after: str | None = None  # Retry-After header on 429/5xx
