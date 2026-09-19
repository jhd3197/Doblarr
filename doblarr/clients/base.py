"""Shared HTTP base for the *arr-style service clients.

Wraps a `requests.Session` with a base URL, default timeout, simple retry with
exponential backoff on transient failures (connection errors, timeouts, 429 —
honoring `Retry-After` — and 5xx), and uniform error mapping into the
`doblarr.errors` hierarchy. Per-request `timeout=` overrides are supported for
long-running calls (e.g. voicebox generation).
"""

from __future__ import annotations

import logging
import time

import requests

from ..errors import ArrClientError

log = logging.getLogger("doblarr.clients.base")


class ArrClient:
    """Session-backed HTTP client with retries and uniform errors."""

    service = "service"              # display name used in error messages
    auth_label = "API key"           # what a 401 rejects ("API key" / "token")
    error_cls: type[ArrClientError] = ArrClientError
    max_retries = 2                  # retries after the first attempt
    backoff = 0.5                    # seconds; doubles each retry

    def __init__(self, base_url: str, timeout: int = 30,
                 headers: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)

    # -- internals --------------------------------------------------------
    def _error(self, message: str, status: int | None = None) -> ArrClientError:
        return self.error_cls(message, status=status)

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.backoff * (2 ** attempt)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay)

    def _request(self, method: str, path: str, *,
                 timeout: int | None = None, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        last_exc: requests.RequestException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, url,
                                            timeout=timeout or self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    log.debug("%s %s attempt %d failed: %s", method, path, attempt + 1, exc)
                    self._sleep(attempt)
                continue
            if resp.status_code == 401:
                raise self._error(
                    f"{self.service} rejected the {self.auth_label} (401)", status=401)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    self._sleep(attempt, resp.headers.get("Retry-After"))
                    continue
                raise self._error(
                    f"{self.service} {resp.status_code} on {method} {path}: "
                    f"{resp.text[:200]}", status=resp.status_code)
            if not resp.ok:
                raise self._error(
                    f"{self.service} {resp.status_code} on {method} {path}: "
                    f"{resp.text[:200]}", status=resp.status_code)
            return resp
        raise self._error(f"{self.service} unreachable at {self.base_url}: {last_exc}")

    # -- verb helpers -----------------------------------------------------
    def _get(self, path: str, params: dict | None = None,
             timeout: int | None = None) -> dict:
        return self._request("GET", path, params=params, timeout=timeout).json()

    def _post(self, path: str, timeout: int | None = None, **kwargs) -> dict:
        return self._request("POST", path, timeout=timeout, **kwargs).json()

    def _put(self, path: str, params: dict | None = None,
             timeout: int | None = None, **kwargs) -> None:
        self._request("PUT", path, params=params, timeout=timeout, **kwargs)

    def _delete(self, path: str, timeout: int | None = None, **kwargs) -> None:
        self._request("DELETE", path, timeout=timeout, **kwargs)
