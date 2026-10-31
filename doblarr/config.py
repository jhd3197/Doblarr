"""Load Doblarr configuration from YAML with defaults, env overrides, and
schema validation.

Loading only warns about invalid values, so a hand-edited file still boots.
Saving from the app refuses a change that would add an invalid value.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from .config_schema import ConfigModel, config_problems, validate_config
from .errors import InvalidSettings
from .fileio import write_text
from .languages import base_language, normalize
from .languages import parse as parse_language_tag

log = logging.getLogger("doblarr.config")

DEFAULTS: dict[str, Any] = ConfigModel().model_dump()


# Dotted keys whose values must never be sent to the browser or written back
# from a redacted round-trip.
SECRET_KEYS = {
    "connect.radarr_api_key", "connect.sonarr_api_key", "connect.plex_token",
    "web.api_key", "notify.discord_webhook", "voicestudio.api_key",
    "research.opensubtitles_api_key", "research.tmdb_api_key",
}
SECRET_SENTINEL = "••••••"

# Each service URL and the secret Doblarr sends to it. A changed URL without a
# new secret drops the stored one, so editing a URL cannot send a saved key to
# another server.
URL_SECRETS = {
    "connect.radarr_url": "connect.radarr_api_key",
    "connect.sonarr_url": "connect.sonarr_api_key",
    "connect.plex_url": "connect.plex_token",
    "voicestudio.base_url": "voicestudio.api_key",
}

# Environment variables that override the matching YAML value when set
# (documented in config.example.yaml).
ENV_OVERRIDES = {
    "DOBLARR_RADARR_API_KEY": ("connect", "radarr_api_key"),
    "DOBLARR_SONARR_API_KEY": ("connect", "sonarr_api_key"),
    "DOBLARR_PLEX_TOKEN": ("connect", "plex_token"),
    "DOBLARR_VOICESTUDIO_API_KEY": ("voicestudio", "api_key"),
}


def _migrate_legacy_locale(data: dict) -> None:
    """Fill dub.target_locale from a legacy Spanish translate.locale when compatible.

    A concrete (non-auto) Spanish regional translate.locale becomes the target
    locale only when the primary target language is Spanish; a Spanish-regional
    setting next to a non-Spanish target is surfaced and never applied to
    another language.
    """
    dub = data.setdefault("dub", {})
    if dub.get("target_locale"):
        return
    legacy = str(data.get("translate", {}).get("locale") or "auto")
    if legacy == "auto":
        return
    parsed = parse_language_tag(legacy)
    if not parsed or base_language(parsed) != "es":
        return
    targets = data.get("general", {}).get("target_languages") or []
    first = str(targets[0]) if targets else ""
    base = base_language(normalize(first) or first) if first else ""
    if base == "es":
        dub["target_locale"] = parsed
        log.info("config: migrated legacy translate.locale=%s to dub.target_locale", parsed)
    elif first:
        log.warning(
            "config: translate.locale=%s is Spanish-specific but the target language "
            "is %s; leaving dub.target_locale unset",
            legacy, first,
        )


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


# Settings a queued job or a saved title plan may never change: where files are
# written, which servers are called, and the secrets sent to them. Only Settings
# can. Checked where overrides arrive (JobStore.add, the title plan route).
PROTECTED_OVERRIDES = ("paths.", "connect.", "web.", "speech.",
                       "research.opensubtitles_api_key", "research.tmdb_api_key",
                       "voicebox.base_url", "voicestudio.base_url", "voicestudio.api_key",
                       "translate.endpoint", "knowledge.pack_distribution_url")


def _protected(dotted: str) -> bool:
    return any(dotted.startswith(rule) if rule.endswith(".") else dotted == rule
               for rule in PROTECTED_OVERRIDES)


def check_overrides(overrides: dict) -> None:
    """Raise InvalidSettings when an override touches a protected setting."""
    keys = []
    for key, value in overrides.items():
        # {"paths": {"output_dir": …}} is the nested form of "paths.output_dir".
        if isinstance(value, dict) and "." not in key:
            keys += [f"{key}.{inner}" for inner in value]
        else:
            keys.append(key)
    refused = sorted(k for k in keys if _protected(k))
    if refused:
        raise InvalidSettings(f"a job cannot override {', '.join(refused)}; "
                              "change it in Settings")


def _dotted(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _drop_secrets_for_moved_urls(current: dict, changes: dict) -> dict:
    """Blank the secret of every service whose URL `changes` moves elsewhere."""
    out = copy.deepcopy(changes)
    for url_key, secret_key in URL_SECRETS.items():
        new_url = _dotted(changes, url_key)
        if new_url is None or new_url == _dotted(current, url_key):
            continue
        if _dotted(changes, secret_key) not in (None, SECRET_SENTINEL):
            continue        # a new secret arrived with the new URL
        section, key = secret_key.split(".")
        out.setdefault(section, {})[key] = ""
        if _dotted(current, secret_key):
            log.warning("config: %s changed, so the saved %s was cleared; enter it again",
                        url_key, secret_key)
    return out


def load_user_data(path: str | Path) -> dict:
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def save_user_data(path: str | Path, data: dict) -> None:
    """Write the user config atomically: a crash leaves the old file, never an empty one."""
    write_text(Path(path), yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


class Config:
    """Dot/section access over the merged config dict."""

    def __init__(self, data: dict[str, Any], path: str | Path = "config.yaml"):
        self._data = data
        self._path = Path(path)
        self._save_lock = threading.Lock()

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
    def db_path(self) -> Path:
        configured = self._data["paths"].get("db")
        return Path(configured) if configured else self.work_dir / "doblarr.db"

    @property
    def output_dir(self) -> Path:
        return Path(self._data["paths"]["output_dir"])

    def as_dict(self, redact_secrets: bool = False) -> dict:
        data = copy.deepcopy(self._data)
        if redact_secrets:
            for dotted in SECRET_KEYS:
                _redact(data, dotted)
        return data

    def with_overrides(self, overrides: dict) -> Config:
        """Return an independent config, accepting nested or dotted title settings."""
        nested: dict[str, Any] = {}
        for dotted, value in copy.deepcopy(overrides).items():
            parts = dotted.split(".")
            node = nested
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return Config(_deep_merge(self.as_dict(), nested), self.path)

    def apply_and_save(self, changes: dict) -> dict:
        """Merge changes into the user config file, persist, and reload in place.

        Raises InvalidSettings, and saves nothing, when the change adds a value
        the schema rejects. Problems the file already had do not block a save.
        """
        with self._save_lock:
            clean = _strip_sentinels(_drop_secrets_for_moved_urls(self._data, changes))
            merged_user = _deep_merge(load_user_data(self._path), clean)
            data = _deep_merge(DEFAULTS, merged_user)
            _apply_env_overrides(data)
            _migrate_legacy_locale(data)
            added = [p for p in config_problems(data) if p not in config_problems(self._data)]
            if added:
                raise InvalidSettings("; ".join(added))
            save_user_data(self._path, merged_user)
            self._data = data
        return merged_user

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load config.yaml if present, else fall back to defaults."""
        data = copy.deepcopy(DEFAULTS)
        candidate = Path(path) if path else Path("config.yaml")
        if candidate.exists():
            data = _deep_merge(data, load_user_data(candidate))
        _apply_env_overrides(data)
        _migrate_legacy_locale(data)
        validate_config(data)
        return cls(data, path=candidate)
