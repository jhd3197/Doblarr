"""API-key authentication for the Doblarr HTTP API.

When `web.api_key` is set, every `/api/*` route (except `/api/health`) requires
the key via the `X-Api-Key` header (or `?api_key=` for convenience). When it is
empty — the self-hosted default — the API stays open and a warning is logged at
startup. The static UI mount is not covered by this dependency.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from .config import Config

log = logging.getLogger("doblarr.auth")

OPEN_PATHS = {"/api/health", "/api/health/ready"}


def build_api_key_dependency(config: Config) -> Callable[[Request], Awaitable[None]]:
    """Return a FastAPI dependency enforcing `web.api_key` on API routes."""
    if not (config.get("web", {}) or {}).get("api_key"):
        log.warning("web.api_key is not set — the HTTP API is UNAUTHENTICATED; "
                    "set web.api_key in config.yaml to require X-Api-Key")

    async def verify_api_key(request: Request) -> None:
        api_key = (config.get("web", {}) or {}).get("api_key") or ""
        if not api_key or request.url.path in OPEN_PATHS:
            return
        supplied = (request.headers.get("x-api-key")
                    or request.query_params.get("api_key") or "")
        if not hmac.compare_digest(supplied, api_key):
            raise HTTPException(status_code=401, detail="missing or invalid API key")

    return verify_api_key
