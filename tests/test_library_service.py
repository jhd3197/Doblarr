"""Library scans: one at a time, and a waiting caller reuses the fresh result."""

import threading
import time
from types import SimpleNamespace

from doblarr.config import Config
from doblarr.errors import ConfigError
from doblarr.events import EventBus
from doblarr.library_service import LibraryService
from doblarr.store import Database


class SlowRadarr:
    def __init__(self):
        self.calls = 0

    def list_movies(self):
        self.calls += 1
        time.sleep(0.3)
        return []


def test_concurrent_forced_scans_run_once(tmp_path):
    radarr = SlowRadarr()

    class Services(SimpleNamespace):
        @property
        def sonarr(self):
            raise ConfigError("not configured")

    config = Config.load(tmp_path / "config.yaml").with_overrides({
        "paths.work_dir": str(tmp_path / "work"), "discovery.plex": False})
    library = LibraryService(config, Database(tmp_path / "d.db"), EventBus(),
                             Services(radarr=radarr))
    threads = [threading.Thread(target=library.scan, kwargs={"force": True}) for _ in range(2)]
    for t in threads:
        t.start()
        time.sleep(0.05)        # the second asks while the first is still scanning
    for t in threads:
        t.join()
    assert radarr.calls == 1
