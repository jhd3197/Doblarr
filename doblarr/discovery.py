"""Library discovery — find titles that lack an audio track in a target language.

This is the first "real" thing Doblarr does: read what audio each title already
has and decide whether it needs a dub. Radarr is the first source because a single
bulk call gives the original language AND the audio languages present in the file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# English language name (as Radarr reports originalLanguage.name) -> ISO 639-1
NAME_TO_ISO2 = {
    "english": "en", "spanish": "es", "korean": "ko", "japanese": "ja",
    "chinese": "zh", "mandarin": "zh", "cantonese": "zh", "german": "de",
    "french": "fr", "italian": "it", "portuguese": "pt", "russian": "ru",
    "hindi": "hi", "arabic": "ar", "dutch": "nl", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi", "polish": "pl",
    "turkish": "tr", "thai": "th", "vietnamese": "vi", "indonesian": "id",
    "hebrew": "he", "greek": "el", "czech": "cs", "hungarian": "hu",
    "romanian": "ro", "ukrainian": "uk", "tagalog": "tl", "malayalam": "ml",
    "tamil": "ta", "telugu": "te", "persian": "fa", "flemish": "nl",
}

# ISO 639-2/B (what mediaInfo.audioLanguages uses) -> ISO 639-1
ISO3_TO_ISO2 = {
    "eng": "en", "spa": "es", "kor": "ko", "jpn": "ja", "chi": "zh", "zho": "zh",
    "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "ita": "it", "por": "pt",
    "rus": "ru", "hin": "hi", "ara": "ar", "dut": "nl", "nld": "nl", "swe": "sv",
    "nor": "no", "dan": "da", "fin": "fi", "pol": "pl", "tur": "tr", "tha": "th",
    "vie": "vi", "ind": "id", "heb": "he", "gre": "el", "ell": "el", "cze": "cs",
    "ces": "cs", "hun": "hu", "rum": "ro", "ron": "ro", "ukr": "uk", "tgl": "tl",
    "mal": "ml", "tam": "ta", "tel": "te", "per": "fa", "fas": "fa",
}


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
                path=s.get("path")))
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
            path=s.get("path")))

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
