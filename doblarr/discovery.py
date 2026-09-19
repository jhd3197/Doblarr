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
    label: str             # "needs-dub" | "available"
    status: str            # same as label, used for UI filtering
    auto_dub: bool


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
        ))

    # needs-dub first, then alphabetical
    items.sort(key=lambda i: (i.status != "needs-dub", i.title.lower()))
    return items


def summarize(items: list[LibraryItem]) -> dict:
    return {
        "total": len(items),
        "needs_dub": sum(1 for i in items if i.status == "needs-dub"),
        "available": sum(1 for i in items if i.status == "available"),
    }


def to_dicts(items: list[LibraryItem]) -> list[dict]:
    return [asdict(i) for i in items]
