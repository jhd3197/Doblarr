"""Plex as a library source: everything on disk that Sonarr and Radarr do not list.

Sonarr and Radarr remain the source for what they manage; they know the
original language and drive downloads. Plex sees every file in its libraries
whether or not an *arr manages it (a show added by hand, a folder copied from
elsewhere), so it fills the gaps: a title already listed by an *arr, matched
by TVDB/TMDB id or by folder, is never listed twice.

Plex lists episodes without their audio streams, so each file's languages
take one metadata call. Those are cached by item and Plex's `updatedAt`, so a
rescan only asks about files that are new or changed.

Two limits, stated rather than guessed around:

- Plex does not say what language a title was originally made in, so a Plex
  item's original language is unknown ("??"). A title is "available" when its
  files carry a target language, never because it is presumed original.
- An item Plex could not match to TMDB or TVDB (home videos, YouTube) is left
  out unless `discovery.plex_unmatched` is on.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from .discovery import LibraryItem, _audio_iso2
from .fileio import write_json

log = logging.getLogger("doblarr.plex_library")

WORKERS = 8
CACHE_FILE = "plex-audio.json"


def library_path_key(path: str | None) -> str:
    """A library path as a comparable string. Case is always folded: Plex and
    the *arr apps may report the same file from machines that disagree on case."""
    return str(path or "").replace("\\", "/").rstrip("/").casefold()


def _inside(path: str, roots: set[str]) -> bool:
    p = library_path_key(path)
    return any(p == r or p.startswith(r + "/") for r in roots)


def show_folder(files: list[str]) -> str | None:
    """The show's own folder: the common parent, above any 'Season NN' folder."""
    dirs = [os.path.dirname(f.replace("\\", "/")) for f in files if f]
    if not dirs:
        return None
    try:
        common = os.path.commonpath(dirs)
    except ValueError:  # files on different drives
        common = dirs[0]
    if Path(common).name.lower().startswith(("season", "specials")):
        common = str(Path(common).parent)
    return str(Path(common))


def poster_url(thumb: str | None) -> str | None:
    """A browser-safe poster: Doblarr fetches it from Plex with its own token."""
    if not thumb or not str(thumb).startswith("/library/metadata/"):
        return None
    return "/api/plex/thumb?path=" + quote(thumb, safe="")


class AudioCache:
    """Audio languages per Plex item, valid while the item's updatedAt holds.

    Rows also keep each audio track's tag and title (`tracks`) for the audio
    languages page; rows written before that have none and are read again.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def get(self, key: str, updated) -> list[str] | None:
        row = self.data.get(str(key))
        return row["langs"] if row and row.get("updated") == updated else None

    def tracks(self, key: str, updated) -> list[dict] | None:
        row = self.data.get(str(key))
        return row.get("tracks") if row and row.get("updated") == updated else None

    def put(self, key: str, updated, langs: list[str], tracks: list[dict] | None = None) -> None:
        self.data[str(key)] = {"updated": updated, "langs": langs, "tracks": tracks}

    def forget(self, key: str) -> None:
        self.data.pop(str(key), None)

    def save(self) -> None:
        if not self.path:
            return
        write_json(self.path, self.data, indent=None)


def read_audio(client, cache: AudioCache, leaves: list[dict]) -> None:
    """Ask Plex for the audio streams of `leaves` and store them in `cache`.

    Files are read in parallel; one Plex cannot read never ends the scan, it is
    simply asked again next time.
    """
    if not leaves:
        return
    log.info("plex: reading audio streams of %d new or changed file(s)", len(leaves))

    def probe(leaf):
        try:
            meta = client.metadata(leaf["ratingKey"])
            return leaf, client.audio_languages(meta), audio_tracks(meta)
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not end the scan
            log.warning("plex: %s: %s", leaf.get("title"), exc)
            return leaf, None, None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for leaf, langs, tracks in pool.map(probe, leaves):
            if langs is not None:
                cache.put(leaf["ratingKey"], leaf.get("updatedAt"), langs, tracks)
    cache.save()


def scan_plex(client, targets: list[str], *, known_tvdb: set[int], known_tmdb: set[int],
              known_paths: set[str], cache: AudioCache, include_unmatched: bool = False,
              treat_undefined_as: str = "original") -> list[LibraryItem]:
    """Plex movies and shows the *arr sources do not already list."""
    target_set = {t.strip().lower() for t in targets}
    roots = {library_path_key(p) for p in known_paths if p}
    titles: list[tuple[dict, dict, dict, list[dict]]] = []   # (item, ids, section, leaves)
    for section in client.sections():
        if section["type"] not in ("movie", "show"):
            continue
        for item in client.section_items(section["key"]):
            ids = client.external_ids(item)
            if not ids and not include_unmatched:
                continue
            if ids.get("tvdb") in known_tvdb or ids.get("tmdb") in known_tmdb:
                continue
            leaves = [item] if section["type"] == "movie" else client.leaves(item["ratingKey"])
            files = [f for leaf in leaves for f in _files(leaf)]
            if not files or all(_inside(f, roots) for f in files):
                continue
            titles.append((item, ids, section, leaves))

    missing = [leaf for _i, _d, _s, leaves in titles for leaf in leaves
               if cache.get(leaf["ratingKey"], leaf.get("updatedAt")) is None]
    read_audio(client, cache, missing)

    items = []
    for item, ids, section, leaves in titles:
        with_target, codes = 0, set()
        for leaf in leaves:
            found = _audio_iso2("/".join(cache.get(leaf["ratingKey"], leaf.get("updatedAt"))
                                         or []), "", target_set, treat_undefined_as)
            codes |= found
            with_target += bool(found & target_set)
        total = len(leaves)
        status = ("needs-dub" if with_target == 0 else
                  "available" if with_target == total else "partial")
        files = [f for leaf in leaves for f in _files(leaf)]
        movie = section["type"] == "movie"
        shown = "/".join(sorted(codes)) if codes else "none"
        items.append(LibraryItem(
            title=item.get("title", "?"), year=item.get("year"), original="??",
            source=f"Plex · {section['title']}",
            existing_audio=shown if movie else f"{shown} ({with_target}/{total})",
            label=status, status=status, auto_dub=status != "available",
            path=files[0] if movie else show_folder(files),
            poster=poster_url(item.get("thumb")), audio_langs=sorted(codes),
            tmdb_id=ids.get("tmdb") if movie else None,
            tvdb_id=ids.get("tvdb") if not movie else None,
            media_type="movie" if movie else "show"))
    return items


def audio_tracks(item: dict) -> list[dict]:
    """Every audio stream of an item's files as found: tag, title, channels.

    `pos` counts audio streams within one file (ffmpeg's `a:N`), which is how
    a track is addressed when it is rewritten.
    """
    found = []
    for media in item.get("Media") or []:
        for part in media.get("Part") or []:
            audio = [s for s in part.get("Stream") or [] if s.get("streamType") == 2]
            for pos, stream in enumerate(audio):
                found.append({
                    "file": part.get("file") or "", "pos": pos,
                    "lang": str(stream.get("languageCode") or "und").lower(),
                    "title": str(stream.get("title") or ""),
                    "channels": int(stream.get("channels") or 0),
                    "default": bool(stream.get("default")),
                })
    return found


def _files(leaf: dict) -> list[str]:
    return [part["file"] for media in leaf.get("Media") or []
            for part in media.get("Part") or [] if part.get("file")]


def show_inventory(client, tvdb_id: int, cache: AudioCache | None = None):
    """A Plex show in the shape the series routes read from Sonarr.

    Returns (show, episodes, files) like `SonarrClient`, so the episode list,
    queueing and the characters view work the same for a show only Plex has.
    Episode and file ids are Plex rating keys.
    """
    for section in client.sections():
        if section["type"] != "show":
            continue
        for item in client.section_items(section["key"]):
            if client.external_ids(item).get("tvdb") != tvdb_id:
                continue
            leaves = client.leaves(item["ratingKey"])
            episodes: list[dict] = []
            files: list[dict] = []
            for leaf in leaves:
                key = int(leaf["ratingKey"])
                paths = _files(leaf)
                langs = (cache.get(leaf["ratingKey"], leaf.get("updatedAt")) if cache else None)
                if langs is None:
                    langs = client.audio_languages(client.metadata(leaf["ratingKey"]))
                    if cache is not None:
                        cache.put(leaf["ratingKey"], leaf.get("updatedAt"), langs)
                episodes.append({"id": key, "seasonNumber": leaf.get("parentIndex", 0),
                                 "episodeNumber": leaf.get("index", 0),
                                 "title": leaf.get("title", ""),
                                 "episodeFileId": key if paths else 0})
                if paths:
                    files.append({"id": key, "path": paths[0],
                                  "mediaInfo": {"audioLanguages": "/".join(langs)}})
            if cache is not None:
                cache.save()
            show = {"title": item.get("title", "?"), "tvdbId": tvdb_id,
                    "path": show_folder([f["path"] for f in files]),
                    "originalLanguage": {}, "source": "Plex"}
            return show, episodes, files
    return None
