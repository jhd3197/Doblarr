"""Shared HTTP base for the *arr-style service clients.

Wraps a `requests.Session` with a base URL, default timeout, and tenacity-based
retry: exponential backoff on transient failures (connection errors, timeouts,
429 — honoring `Retry-After` — and 5xx), with errors mapped uniformly into the
`doblarr.errors` hierarchy. Per-request `timeout=` overrides are supported for
long-running calls (e.g. voicebox generation).
"""

from __future__ import annotations

import logging

import requests
from tenacity import (Retrying, before_sleep_log, retry_if_exception,
                      stop_after_attempt)

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

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        return isinstance(exc, ArrClientError) and (
            exc.status is None or exc.status == 429 or exc.status >= 500)

    def _wait(self, retry_state) -> float:
        """Exponential backoff, raised to `Retry-After` when the server asks."""
        exc = retry_state.outcome.exception()
        delay = self.backoff * (2 ** (retry_state.attempt_number - 1))
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        return delay

    def _attempt(self, method: str, path: str, *,
                 timeout: int | None = None, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url,
                                        timeout=timeout or self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise self._error(
                f"{self.service} unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise self._error(
                f"{self.service} rejected the {self.auth_label} (401)", status=401)
        if not resp.ok:
            err = self._error(
                f"{self.service} {resp.status_code} on {method} {path}: "
                f"{resp.text[:200]}", status=resp.status_code)
            err.retry_after = resp.headers.get("Retry-After")
            raise err
        return resp

    def _request(self, method: str, path: str, *,
                 timeout: int | None = None, **kwargs) -> requests.Response:
        retryer = Retrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception(self._is_transient),
            before_sleep=before_sleep_log(log, logging.DEBUG),
            reraise=True,
        )
        return retryer(self._attempt, method, path, timeout=timeout, **kwargs)

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
