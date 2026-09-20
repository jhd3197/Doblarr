"""Isolated browser-test server: temporary data and a fake media library."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from doblarr.config import Config
from doblarr.server import create_app

_temp = tempfile.TemporaryDirectory(prefix="doblarr-browser-")
_root = Path(_temp.name)
config = Config.load(_root / "config.yaml")
config.apply_and_save({
    "paths": {"work_dir": str(_root / "work"), "output_dir": str(_root / "output")},
})
app = create_app(config)
app.state.services._cache["radarr"] = SimpleNamespace(list_movies=lambda: [{
    "title": "Test Film", "year": 2024, "hasFile": True, "tmdbId": 42,
    "originalLanguage": {"name": "Korean"},
    "movieFile": {"path": str(_root / "film.mkv"),
                  "mediaInfo": {"audioLanguages": "kor"}},
}])
app.state.services._cache["voicebox"] = SimpleNamespace(list_voices=lambda: [])
