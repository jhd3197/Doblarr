"""Marcarr (media analysis) through its own typed client, `marcarr.client`.

Marcarr is an optional extra, so its package is imported when a client is
built, never at module import. Every call carries the API key: Marcarr asks
for one from any client that is not on loopback.
"""

from __future__ import annotations

from typing import Any

from ..errors import ArrClientError, ConfigError


class MarcarrError(ArrClientError, RuntimeError):
    pass


def _marcarr() -> Any:
    try:
        from marcarr import client
    except ImportError as exc:
        raise ConfigError('Marcarr support is not installed: '
                          'run `pip install "doblarr[marcarr]"`.') from exc
    return client


class MarcarrClient:
    """`marcarr.client.MarcarrClient` with Doblarr's errors.

    Its methods (`analysis`, `shots`, `scenes`, `faces`, ...) are reached by
    name and return Marcarr's own models.
    """

    service = "Marcarr"
    error_cls = MarcarrError

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        if not api_key:
            raise ConfigError("Marcarr needs an API key (marcarr.api_key); "
                              "Doblarr never calls it without one.")
        module = _marcarr()
        self._api_error: type[Exception] = module.APIError
        self.base_url = base_url.rstrip("/")
        self.client = module.MarcarrClient(base_url, api_key=api_key, timeout=timeout)

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.client, name)(*args, **kwargs)
        except self._api_error as exc:
            raise self.error_cls(str(exc), status=getattr(exc, "status", None)) from exc

    def status(self) -> dict:
        """Version and queue state; the cheapest call that proves URL and key."""
        return self._call("status")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or not callable(getattr(self.client, name, None)):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)
