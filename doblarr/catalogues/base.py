"""What every public catalogue provider shares.

A provider answers two questions about a title, in one shape:

- `search(query)` lists entries that might be the title. It stores nothing.
- `read(url)` returns one entry with its cast, each character in the shape
  `doblarr.published_cast` stores (name, native, alternative names, role,
  gender, age, description, voice actors by language, url).

The rules are the ones the published cast already follows. Only the title or
the entry's id leaves the machine, and only when a person asks. A provider
that cannot be reached, is rate limited or has no key raises `Unreachable`,
so a caller skips it quietly instead of failing: coverage is luck per title.
Each host is asked at most once per its interval, and answers are cached on
disk for `cache_days` once `configure` has been given a folder.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..errors import DoblarrError

USER_AGENT = "Doblarr/1 (title research; https://github.com/jhd3197/Doblarr)"
ROLES = ("MAIN", "SUPPORTING", "BACKGROUND")

# Language codes some catalogues use, named the way AniList names them, so
# voices from every source group under one label.
LANGUAGES = {
    "JA": "Japanese", "EN": "English", "ES": "Spanish", "PT": "Portuguese", "FR": "French",
    "DE": "German", "IT": "Italian", "KO": "Korean", "ZH": "Chinese", "HE": "Hebrew",
    "HU": "Hungarian", "PL": "Polish", "RU": "Russian", "TL": "Tagalog", "AR": "Arabic",
    "CA": "Catalan", "NL": "Dutch", "SV": "Swedish", "FI": "Finnish", "TR": "Turkish",
    "TH": "Thai", "ID": "Indonesian", "VI": "Vietnamese", "EL": "Greek", "CS": "Czech",
}

# Romanizations of one long vowel: Hyūga, Hyuuga, Hyuga; Kōichi, Kouichi.
_LONG_VOWELS = (("ou", "o"), ("oo", "o"), ("uu", "u"), ("aa", "a"), ("ii", "i"), ("ee", "e"))

Fetch = Callable[..., Any]


def name_key(text: str) -> str:
    """A name folded for matching only: accents, case and long-vowel spellings."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", str(text or ""))
                   if not unicodedata.combining(ch))
    text = " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())
    for long, short in _LONG_VOWELS:
        text = text.replace(long, short)
    return text


class Unreachable(DoblarrError):
    """The catalogue could not answer now (offline, rate limited, no key)."""

    http_status = 502


class CastProvider(Protocol):
    name: str
    label: str
    host: str

    def can_handle(self, url: str) -> bool: ...

    def search(self, query: str, *, max_results: int = 8) -> list[dict]: ...

    def read(self, url: str, *, max_characters: int = 100, fresh: bool = False) -> dict: ...


# -- politeness --------------------------------------------------------------

_throttle_lock = threading.Lock()
_last_call: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}


def wait_turn(host: str, interval: float) -> None:
    """Block until `interval` seconds have passed since the last call to `host`."""
    with _throttle_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        delay = _last_call.get(host, 0.0) + interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _last_call[host] = time.monotonic()


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             json_body: Any = None, method: str = "GET", timeout: float = 20.0,
             interval: float = 1.0) -> str:
    """One polite request; any failure to answer becomes `Unreachable`."""
    import requests

    wait_turn(urlsplit(url).netloc, interval)
    try:
        resp = requests.request(method, url, params=params, json=json_body, timeout=timeout,
                                headers={"User-Agent": USER_AGENT, **(headers or {})})
    except requests.RequestException as exc:
        raise Unreachable(f"{urlsplit(url).netloc} did not answer: {exc.__class__.__name__}"
                          ) from exc
    if resp.status_code == 404:
        raise LookupError(f"{url} was not found")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise Unreachable(f"{urlsplit(url).netloc} answered {resp.status_code}")
    if resp.status_code >= 400:
        raise Unreachable(f"{urlsplit(url).netloc} refused the request ({resp.status_code})")
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def get_json(fetch: Fetch | None, url: str, **kwargs: Any) -> Any:
    text = (fetch or http_get)(url, **kwargs)
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError as exc:
        raise Unreachable(f"{urlsplit(url).netloc} answered something that is not JSON") from exc


# -- cache -------------------------------------------------------------------

_cache_dir: Path | None = None
_cache_days = 30.0
_memory: dict[str, tuple[float, Any]] = {}


def configure(*, cache_dir: str | Path | None = None, cache_days: float | None = None) -> None:
    """Where catalogue answers are kept and for how long (from `research.*`)."""
    global _cache_dir, _cache_days
    _cache_dir = Path(cache_dir) if cache_dir else None
    if cache_days is not None:
        _cache_days = max(0.0, float(cache_days))


def configure_from(config) -> None:
    research = (config.get("research") or {}) if hasattr(config, "get") else {}
    configure(cache_dir=Path(config.work_dir) / "cache" / "catalogues",
              cache_days=research.get("cache_days", 30))


def _cache_file(key: str) -> Path | None:
    if _cache_dir is None:
        return None
    name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return _cache_dir / key.split(":", 1)[0] / f"{name}.json"


def cached(key: str, produce: Callable[[], Any], *, fresh: bool = False) -> Any:
    """The answer for `key`, produced once per `cache_days`."""
    max_age = _cache_days * 86400
    now = time.time()
    if not fresh:
        hit = _memory.get(key)
        if hit and now - hit[0] < max_age:
            return hit[1]
        path = _cache_file(key)
        if path is not None and path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if now - float(stored["at"]) < max_age:
                    _memory[key] = (float(stored["at"]), stored["value"])
                    return stored["value"]
            except (ValueError, KeyError, OSError):
                pass
    value = produce()
    _memory[key] = (now, value)
    path = _cache_file(key)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at": now, "key": key, "value": value},
                                       ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return value


def clear_memory() -> None:
    _memory.clear()


# -- shapes ------------------------------------------------------------------

def voice(name: str, language: str = "", native: str = "") -> dict:
    return {"name": " ".join(str(name or "").split()), "native": native or "",
            "language": language or ""}


def character(*, cid: Any, name: str, native: str = "", alternative=(), role: str = "",
              gender: str | None = None, age: str | None = None, description: str = "",
              voice_actors=(), url: str | None = None) -> dict:
    """A cast member in the shape the AniList reader returns."""
    return {"id": cid, "name": " ".join(str(name or "").split()), "native": native or "",
            "alternative": [a for a in alternative if a], "role": (role or "").upper(),
            "gender": gender, "age": age, "description": description or "",
            "voice_actors": list(voice_actors), "url": url}


def hit(*, source: str, cid: Any, title: str, url: str, titles: dict | None = None,
        year: int | None = None, fmt: str | None = None, episodes: int | None = None) -> dict:
    return {"source": source, "id": cid, "title": title, "titles": titles or {}, "year": year,
            "format": fmt, "episodes": episodes, "url": url}


def year_of(text: Any) -> int | None:
    digits = str(text or "")[:4]
    return int(digits) if digits.isdigit() else None


def int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
