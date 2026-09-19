"""Load Doblarr configuration from YAML with sensible defaults."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .config_schema import validate_config

log = logging.getLogger("doblarr.config")

DEFAULTS: dict[str, Any] = {
    "paths": {"work_dir": "./work", "output_dir": "./output"},
    "general": {"target_languages": ["en", "es"]},
    "web": {"host": "127.0.0.1", "port": 6363, "api_key": ""},
    "connect": {"radarr_url": None, "radarr_api_key": None,
                "sonarr_url": None, "sonarr_api_key": None,
                "plex_url": None, "plex_token": None},
    "discovery": {"only_original_foreign": True, "treat_undefined_as": "original",
                  "rescan_interval": "6h", "auto_scan": False},
    "filtering": {
        "tag_missing_dub": "needs-dub",
        "hidden_collection_name": "Not in your language",
        "kometa_handoff": True,
        "kometa_file": None,
        "auto_label": False,
    },
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
        "segment_limit": None,   # cap lines per job (handy for CPU test runs)
    },
}


# Dotted keys whose values must never be sent to the browser or written back
# from a redacted round-trip.
SECRET_KEYS = {
    "connect.radarr_api_key", "connect.sonarr_api_key", "connect.plex_token",
    "web.api_key", "notify.discord_webhook",
}
SECRET_SENTINEL = "••••••"

# Environment variables that override the matching YAML value when set
# (documented in config.example.yaml).
ENV_OVERRIDES = {
    "DOBLARR_RADARR_API_KEY": ("connect", "radarr_api_key"),
    "DOBLARR_SONARR_API_KEY": ("connect", "sonarr_api_key"),
    "DOBLARR_PLEX_TOKEN": ("connect", "plex_token"),
}


def _apply_env_overrides(data: dict) -> None:
    for env_var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value:
            data.setdefault(section, {})[key] = value


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _redact(data: dict, dotted: str) -> None:
    parts = dotted.split(".")
    d = data
    for p in parts[:-1]:
        if not isinstance(d, dict) or p not in d:
            return
        d = d[p]
    last = parts[-1]
    if isinstance(d, dict) and d.get(last):
        d[last] = SECRET_SENTINEL


def _strip_sentinels(d: Any) -> Any:
    """Drop any key whose value is the redaction sentinel (an unchanged secret)."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if v == SECRET_SENTINEL:
            continue
        if isinstance(v, dict):
            v = _strip_sentinels(v)
        out[k] = v
    return out


def load_user_data(path: str | Path) -> dict:
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def save_user_data(path: str | Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


class Config:
    """Dot/section access over the merged config dict."""

    def __init__(self, data: dict[str, Any], path: str | Path = "config.yaml"):
        self._data = data
        self._path = Path(path)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def work_dir(self) -> Path:
        return Path(self._data["paths"]["work_dir"])

    @property
    def output_dir(self) -> Path:
        return Path(self._data["paths"]["output_dir"])

    def as_dict(self, redact_secrets: bool = False) -> dict:
        data = copy.deepcopy(self._data)
        if redact_secrets:
            for dotted in SECRET_KEYS:
                _redact(data, dotted)
        return data

    def apply_and_save(self, changes: dict) -> dict:
        """Merge changes into the user config file, persist, and reload in place."""
        clean = _strip_sentinels(changes)
        merged_user = _deep_merge(load_user_data(self._path), clean)
        save_user_data(self._path, merged_user)
        self._data = _deep_merge(DEFAULTS, merged_user)
        _apply_env_overrides(self._data)
        validate_config(self._data)
        return merged_user

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Load config.yaml if present, else fall back to defaults."""
        data = copy.deepcopy(DEFAULTS)
        candidate = Path(path) if path else Path("config.yaml")
        if candidate.exists():
            data = _deep_merge(data, load_user_data(candidate))
        _apply_env_overrides(data)
        validate_config(data)
        return cls(data, path=candidate)
