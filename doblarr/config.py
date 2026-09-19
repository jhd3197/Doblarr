"""Load Doblarr configuration from YAML with sensible defaults."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "paths": {"work_dir": "./work", "output_dir": "./output"},
    "general": {"target_languages": ["en", "es"]},
    "web": {"host": "127.0.0.1", "port": 6363},
    "connect": {"radarr_url": None, "radarr_api_key": None},
    "discovery": {"only_original_foreign": True, "treat_undefined_as": "original"},
    "voicebox": {
        "base_url": "http://127.0.0.1:17493",
        "timeout_seconds": 600,
        "default_engine": "chatterbox-multilingual",
    },
    "translate": {"provider": "claude", "model": "claude-sonnet-5"},
    "transcribe": {
        "source": "subtitles",
        "whisper_model": "large-v3",
        "diarize": True,
    },
    "separate": {"model": "htdemucs_ft"},
    "dub": {
        "voice_mode": "clone",
        "duration_match": True,
        "max_fit_attempts": 5,
        "ducking_ratio": "12:1",
        "track_name_template": "AI - {language}",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Dot/section access over the merged config dict."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def work_dir(self) -> Path:
        return Path(self._data["paths"]["work_dir"])

    @property
    def output_dir(self) -> Path:
        return Path(self._data["paths"]["output_dir"])

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Load config.yaml if present, else fall back to defaults."""
        data = copy.deepcopy(DEFAULTS)
        candidate = Path(path) if path else Path("config.yaml")
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8") as fh:
                user_data = yaml.safe_load(fh) or {}
            data = _deep_merge(data, user_data)
        return cls(data)
