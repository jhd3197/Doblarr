"""Library discovery — find titles that lack an audio track in a target language.

This is the first "real" thing Doblarr does: read what audio each title already
has and decide whether it needs a dub. Radarr is the first source because a single
bulk call gives the original language AND the audio languages present in the file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import languages as _languages

# Derived from the canonical catalog (doblarr.languages): English language name
# (as Radarr reports originalLanguage.name) -> ISO 639-1, ISO 639-2/B (what
# mediaInfo.audioLanguages uses) -> ISO 639-1, and ISO 639-1 -> display name.
NAME_TO_ISO2: dict[str, str] = {}
ISO3_TO_ISO2: dict[str, str] = {}
ISO2_TO_NAME: dict[str, str] = {}
for _entry in _languages.catalog():
    if _entry.id != _entry.base:
        continue  # regional entries don't define base-language mappings
    ISO2_TO_NAME[_entry.id] = _entry.name
    NAME_TO_ISO2[_entry.name.lower()] = _entry.id
    if _entry.iso3:
        ISO3_TO_ISO2[_entry.iso3] = _entry.id
    for _alias in _entry.aliases:
        if len(_alias) == 3 and _alias.isalpha():
            ISO3_TO_ISO2[_alias.lower()] = _entry.id
        else:
            NAME_TO_ISO2[_alias.lower()] = _entry.id


def lang_name(code: str) -> str:
    """'es' -> 'Spanish'; unknown codes fall back to the uppercased code."""
    return _languages.display_name(code)


@dataclass
class LibraryItem:
    title: str
    year: int | None
    original: str          # ISO 639-1 of the original language
    source: str            # e.g. "Radarr · Films"
    existing_audio: str    # normalized audio languages present, e.g. "en/ja"
    label: str             # "needs-dub" | "partial" | "available"
    status: str            # same as label, used for UI filtering
    auto_dub: bool
    path: str | None = None   # media file/folder path, for enqueuing a dub
    poster: str | None = None          # remote poster URL (TMDB CDN) for the UI
    audio_langs: list[str] = field(default_factory=list)  # ISO 639-1 codes present
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    media_type: str | None = None


def _poster_url(images: list | None) -> str | None:
    """Poster remoteUrl from a Radarr/Sonarr images array (None if unavailable).

    Only the remote (TMDB) URL is used — the local `url` is an *arr-relative
    /MediaCover path that the browser cannot reach.
    """
    if not images:
        return None
    poster = next((i for i in images if i.get("coverType") == "poster"), None)
    img = poster or images[0]
    return img.get("remoteUrl")


# Order used when sorting the merged library: work-to-do first.
STATUS_ORDER = {"needs-dub": 0, "partial": 1, "available": 2}


def sort_items(items: list[LibraryItem]) -> list[LibraryItem]:
    items.sort(key=lambda i: (STATUS_ORDER.get(i.status, 3), i.title.lower()))
    return items


def _name_to_iso2(name: str | None) -> str:
    if not name:
        return ""
    return NAME_TO_ISO2.get(name.strip().lower(), name.strip().lower()[:2])


def _audio_iso2(audio_str: str | None, original: str, targets: set[str],
                treat_undefined_as: str) -> set[str]:
    """Turn a mediaInfo audioLanguages string into a set of ISO 639-1 codes."""
    codes: set[str] = set()
    for tok in (audio_str or "").split("/"):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok == "und":
            if treat_undefined_as == "original" and original:
                codes.add(original)
            elif treat_undefined_as == "target":
                codes.update(targets)
            # "unknown" -> contributes nothing
            continue
        iso2 = ISO3_TO_ISO2.get(tok) or (tok if len(tok) == 2 else None)
        if iso2:
            codes.add(iso2)
    return codes


def scan_radarr(movies: list[dict], targets: list[str],
                source_label: str = "Radarr · Films",
                only_original_foreign: bool = True,
                treat_undefined_as: str = "original") -> list[LibraryItem]:
    """Classify every downloaded movie as needs-dub or available."""
    target_set = {t.strip().lower() for t in targets}
    items: list[LibraryItem] = []

    for m in movies:
        if not m.get("hasFile"):
            continue
        original = _name_to_iso2((m.get("originalLanguage") or {}).get("name"))
        media = (m.get("movieFile") or {}).get("mediaInfo") or {}
        audio_codes = _audio_iso2(media.get("audioLanguages"), original,
                                  target_set, treat_undefined_as)
        has_target = bool(audio_codes & target_set)

        # Already watchable: has a target track, or is originally in a target language.
        watchable = has_target or (only_original_foreign and original in target_set)
        label = "available" if watchable else "needs-dub"

        items.append(LibraryItem(
            title=m.get("title", "?"),
            year=m.get("year"),
            original=original or "??",
            source=source_label,
            existing_audio="/".join(sorted(audio_codes)) if audio_codes else "none",
            label=label,
            status=label,
            auto_dub=(label == "needs-dub"),
            path=(m.get("movieFile") or {}).get("path"),
            poster=_poster_url(m.get("images")),
            audio_langs=sorted(audio_codes),
            tmdb_id=m.get("tmdbId"),
            media_type="movie",
        ))

    return sort_items(items)


def scan_sonarr(series_list: list[dict], fetch_files, targets: list[str],
                source_label: str = "Sonarr · Shows",
                only_original_foreign: bool = True,
                treat_undefined_as: str = "original") -> list[LibraryItem]:
    """Classify every series with files.

    A series is available if every episode has a target track, needs-dub if none
    do, and partial in between. `fetch_files(series_id)` returns its episode files;
    it's only called for foreign-original series, to keep the scan cheap.
    """
    target_set = {t.strip().lower() for t in targets}
    items: list[LibraryItem] = []

    for s in series_list:
        stats = s.get("statistics") or {}
        if stats.get("episodeFileCount", 0) <= 0:
            continue
        original = _name_to_iso2((s.get("originalLanguage") or {}).get("name"))

        # Originally in a target language -> watchable; no per-episode probe needed.
        if only_original_foreign and original in target_set:
            items.append(LibraryItem(
                title=s.get("title", "?"), year=s.get("year"), original=original or "??",
                source=source_label, existing_audio="—",
                label="available", status="available", auto_dub=False,
                path=s.get("path"), poster=_poster_url(s.get("images")),
                audio_langs=[original] if original else [],
                tvdb_id=s.get("tvdbId"), media_type="show"))
            continue

        files = fetch_files(s.get("id")) or []
        total = len(files)
        if total == 0:
            continue
        with_target = 0
        all_codes: set[str] = set()
        for f in files:
            codes = _audio_iso2((f.get("mediaInfo") or {}).get("audioLanguages"),
                                original, target_set, treat_undefined_as)
            all_codes |= codes
            if codes & target_set:
                with_target += 1

        if with_target == 0:
            status = "needs-dub"
        elif with_target == total:
            status = "available"
        else:
            status = "partial"
        audio_disp = "/".join(sorted(all_codes)) if all_codes else "none"

        items.append(LibraryItem(
            title=s.get("title", "?"), year=s.get("year"), original=original or "??",
            source=source_label, existing_audio=f"{audio_disp} ({with_target}/{total})",
            label=status, status=status, auto_dub=(status != "available"),
            path=s.get("path"), poster=_poster_url(s.get("images")),
            audio_langs=sorted(all_codes), tvdb_id=s.get("tvdbId"), media_type="show"))

    return sort_items(items)


def summarize(items: list[LibraryItem]) -> dict:
    return {
        "total": len(items),
        "needs_dub": sum(1 for i in items if i.status == "needs-dub"),
        "partial": sum(1 for i in items if i.status == "partial"),
        "available": sum(1 for i in items if i.status == "available"),
    }


def to_dicts(items: list[LibraryItem]) -> list[dict]:
    return [asdict(i) for i in items]


def from_dicts(dicts: list[dict]) -> list[LibraryItem]:
    """Rebuild items from to_dicts() output (e.g. the persisted scan state)."""
    return [LibraryItem(**d) for d in dicts]
