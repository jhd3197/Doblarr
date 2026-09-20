"""Library discovery, persisted scan state, labels and webhook orchestration."""

import datetime as _dt
import logging
from typing import Any

from . import discovery, plex_labels
from .cache import TTLCache
from .clients.plex import PlexError
from .clients.radarr import RadarrError
from .clients.sonarr import SonarrError
from .config import Config
from .errors import ConfigError
from .events import EventBus
from .services import Services
from .store import Database
from .webhooks import Debouncer, is_test_event, should_rescan

log = logging.getLogger("doblarr.library")


class LibraryService:
    def __init__(self, config: Config, db: Database, bus: EventBus, services: Services):
        self.config, self.db, self.bus, self.services = config, db, bus, services
        self.cache = TTLCache(max_size=4)
        self.state: dict = {"last_scan": None, "counts": None}

        # Restore the last scan so /api/status and /api/library survive a restart.
        saved_scan = self.db.load_scan()
        if saved_scan and saved_scan["last_scan"]:
            self.state.update(last_scan=saved_scan["last_scan"],
                              counts=saved_scan["counts"])
            self.cache.set("library", (discovery.from_dicts(saved_scan["items"]), []))

        self.debouncer = Debouncer(lambda: self.scan_with_events("webhook"),
                                   delay=self.webhook_debounce)

    def clear(self) -> None:
        self.cache.clear()

    def _scan_ttl(self) -> float:
        try:
            return float(self.config.get("discovery", {}).get("cache_ttl", 300))
        except (TypeError, ValueError):
            return 300.0

    def scan(self, force: bool = False) -> tuple[list, list[str]]:
        if not force:
            cached = self.cache.get("library", ttl=self._scan_ttl())
            if cached is not None:
                return cached
        targets = self.config["general"]["target_languages"]
        disc = self.config.get("discovery", {})
        only_foreign = disc.get("only_original_foreign", True)
        undefined = disc.get("treat_undefined_as", "original")
        items: list = []
        warnings: list[str] = []
        try:
            movies = self.services.radarr.list_movies()
            items += discovery.scan_radarr(movies, targets,
                only_original_foreign=only_foreign, treat_undefined_as=undefined)
        except ConfigError:
            pass  # Radarr not configured
        except RadarrError as exc:
            warnings.append(f"Radarr: {exc}")
        try:
            sc = self.services.sonarr
            items += discovery.scan_sonarr(sc.list_series(), sc.episode_files, targets,
                only_original_foreign=only_foreign, treat_undefined_as=undefined)
        except ConfigError:
            pass  # Sonarr not configured
        except SonarrError as exc:
            warnings.append(f"Sonarr: {exc}")
        discovery.sort_items(items)
        self.state["last_scan"] = _dt.datetime.now().isoformat(timespec="seconds")
        self.state["counts"] = discovery.summarize(items)
        self.db.save_scan(self.state["last_scan"], self.state["counts"],
                          discovery.to_dicts(items))
        result = (items, warnings)
        self.cache.set("library", result)
        return result

    def scan_and_label(self):
        items, _ = self.scan(force=True)  # a scheduled rescan refreshes the cache
        if self.config.get("filtering", {}).get("auto_label"):
            try:
                plex_labels.sync_labels(items, self.services.plex, self.config, apply=True)
            except ConfigError:
                pass  # Plex not configured
            except PlexError as exc:
                log.warning("auto label sync failed: %s", exc)
        return self.state["counts"]

    def scan_with_events(self, source: str):
        self.bus.publish("scan", {"type": "started", "source": source})
        counts = self.scan_and_label()
        self.bus.publish("scan", {"type": "completed", "counts": counts, "source": source})

    def webhook_debounce(self) -> float:
        try:
            return float(self.config.get("discovery", {}).get("webhook_debounce", 30))
        except (TypeError, ValueError):
            return 30.0

    def handle_webhook(self, source: str, body: dict[str, Any]):
        event_type = body.get("eventType", "")
        if is_test_event(body):
            return {"ok": True, "test": True}
        if not should_rescan(body):
            return {"ok": True, "ignored": event_type}
        log.info("%s webhook (%s) — rescan in %.0fs",
                 source, event_type, self.webhook_debounce())
        self.debouncer.trigger()
        return {"ok": True, "scan": "scheduled"}

