"""Logging setup: console + rotating file, env level override, uvicorn logs.

`DOBLARR_LOG_LEVEL` (or `LOG_LEVEL`) overrides the configured/verbose level.
Server logs go to `<general.log_file>` (default `<work_dir>/logs/doblarr.log`)
via a RotatingFileHandler, and uvicorn's loggers are folded into the same
format and handlers (`uvicorn.run(..., log_config=None)` leaves them to us).
"""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATEFMT = "%H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# The bus itself must never feed back into the bus (recursion guard).
_BUS_MUTED_LOGGERS = {"doblarr.events"}


class BusLogHandler(logging.Handler):
    """Publishes log records to the EventBus on the `log` topic (SSE log stream).

    Streams at its own level (default INFO) regardless of the console level.
    emit() never logs and never raises — a failure here must not recurse.
    """

    def __init__(self, bus, level: int = logging.INFO):
        super().__init__(level)
        self.bus = bus
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in _BUS_MUTED_LOGGERS:
            return
        # A logging handler must never explode (or recurse) — swallow everything.
        with suppress(Exception):
            self.bus.publish("log", {"level": record.levelname,
                                     "logger": record.name,
                                     "message": self.format(record)})


def attach_log_stream(bus, level: int = logging.INFO) -> None:
    """Stream log records onto the bus; the newest app instance replaces old ones."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, BusLogHandler):
            root.removeHandler(handler)
    root.addHandler(BusLogHandler(bus, level=level))


def _level(verbose: bool) -> int:
    name = os.environ.get("DOBLARR_LOG_LEVEL") or os.environ.get("LOG_LEVEL")
    if name:
        return getattr(logging, name.strip().upper(), logging.INFO)
    return logging.DEBUG if verbose else logging.INFO


def log_file_for(config) -> Path:
    configured = (config.get("general", {}) or {}).get("log_file")
    return Path(configured) if configured else config.work_dir / "logs" / "doblarr.log"


def setup_logging(config=None, verbose: bool = False, uvicorn: bool = False) -> None:
    """Configure root logging; call once at process entry (see doblarr.cli)."""
    level = _level(verbose)
    fmt = logging.Formatter(FORMAT, DATEFMT)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if config is not None:
        path = log_file_for(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                                           backupCount=BACKUPS, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    if uvicorn:
        for name in UVICORN_LOGGERS:
            lg = logging.getLogger(name)
            lg.handlers.clear()
            lg.setLevel(level)
            lg.propagate = True
