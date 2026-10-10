"""API-key authentication and Host checking for the Doblarr HTTP API.

Every `/api/*` route (except the health checks) requires `web.api_key`, sent as
the `X-Api-Key` header (or `?api_key=` where a browser cannot set headers).
`doblarr serve` writes a random key into config.yaml on first start; only
`web.allow_unauthenticated: true` leaves the API open.

The Host check stops DNS rebinding: a web page on an attacker's domain that
re-points to this server would otherwise get same-origin access to the API. IP
addresses and names no one can register publicly (single labels, .local, .lan,
.home.arpa, .internal, .localhost) are always accepted; any other domain, such
as a reverse proxy's, must be listed in `web.allowed_hosts`.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Config

log = logging.getLogger("doblarr.auth")

OPEN_PATHS = {"/api/health", "/api/health/ready"}

# Suffixes that only resolve on a private network, so no attacker can own one.
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal")

# Extra allowed hosts, comma separated, for setups that cannot edit the config.
HOSTS_ENV = "DOBLARR_ALLOWED_HOSTS"


def ensure_api_key(config: Config) -> str | None:
    """Generate, save and return a key when none is set; None when nothing changed."""
    web = config.get("web", {}) or {}
    if web.get("api_key") or web.get("allow_unauthenticated"):
        return None
    key = secrets.token_urlsafe(24)
    config.apply_and_save({"web": {"api_key": key}})
    return key


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


def host_name(header: str) -> str:
    """The name in a Host header, without its port or IPv6 brackets."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:header.find("]")] if "]" in header else header[1:]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def host_allowed(host: str, allowed: list[str]) -> bool:
    name = host_name(host).rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name == "localhost" or name.endswith(LOCAL_SUFFIXES):
        return True
    for entry in allowed:
        entry = entry.strip().lower().rstrip(".")
        if name == entry or (entry.startswith(".") and name.endswith(entry)):
            return True
    return False


def build_host_check(config: Config):
    """HTTP middleware refusing requests addressed to an unlisted domain."""

    async def check_host(request: Request, call_next):
        allowed = list((config.get("web", {}) or {}).get("allowed_hosts") or [])
        allowed += [h for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip()]
        host = request.headers.get("host", "")
        if not host_allowed(host, allowed):
            log.warning("refused a request for host %r; add it to web.allowed_hosts", host)
            return JSONResponse(status_code=400, content={
                "error": f"{host_name(host) or 'this host'} is not in web.allowed_hosts; "
                         "add it to config.yaml to reach Doblarr by that name"})
        return await call_next(request)

    return check_host
