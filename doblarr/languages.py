"""Canonical language catalog — BCP-47-style identities shared by every feature.

The catalog describes identity only: canonical IDs, display names, aliases, and
media-code mappings. Regional entries (es-MX, es-VE, …) keep their full identity
through plans, jobs, review, and output metadata, while engines and media tags
keep working with the base language. Malformed tags are rejected; valid but
uncatalogued tags (fr-CA) parse fine and simply report supported=False — an
unknown language is never guessed by truncating to two letters.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("doblarr.languages")

# lang (2-3 alpha) [-script (4 alpha)] [-region (2 alpha | 3 digit)]
_TAG = re.compile(r"^([a-zA-Z]{2,3})(?:-([a-zA-Z]{4}))?(?:-([a-zA-Z]{2}|[0-9]{3}))?$")


@dataclass(frozen=True)
class LanguageEntry:
    id: str  # canonical BCP-47-style id, e.g. "es-MX", "es-419"
    name: str  # English display name, e.g. "Spanish — Mexico"
    native_name: str
    base: str  # base language id, e.g. "es"
    script: str | None = None
    region: str | None = None  # e.g. "MX", "VE", "419"
    iso3: str | None = None  # ISO 639-2/B media code (base languages only)
    aliases: tuple[str, ...] = ()
    direction: str = ""  # translator regional-direction prompt text
    supported: bool = True


_ES_419_DIRECTION = (
    "Neutral Latin American Spanish for studio dubbing. Use ustedes, not vosotros; "
    "avoid Spain-specific vocabulary and heavy regional slang."
)
_ES_MX_DIRECTION = (
    "Mexican Spanish for studio dubbing. Use natural Mexican vocabulary "
    "without adding exaggerated slang or stereotypes."
)
_ES_ES_DIRECTION = "Spanish from Spain with consistent regional vocabulary and forms of address."
_ES_VE_DIRECTION = (
    "Venezuelan Spanish for studio dubbing. Use natural Venezuelan vocabulary and "
    "ustedes forms without adding exaggerated slang or stereotypes."
)

# The first entries are the validated dubbing targets; the rest keep every base
# language discovery already knew about selectable and correctly tagged.
_ENTRIES = [
    LanguageEntry("en", "English", "English", "en", iso3="eng"),
    LanguageEntry("es", "Spanish", "español", "es", iso3="spa"),
    LanguageEntry(
        "es-MX",
        "Spanish — Mexico",
        "español de México",
        "es",
        region="MX",
        direction=_ES_MX_DIRECTION,
    ),
    LanguageEntry(
        "es-VE",
        "Spanish — Venezuela",
        "español de Venezuela",
        "es",
        region="VE",
        direction=_ES_VE_DIRECTION,
    ),
    LanguageEntry(
        "es-419",
        "Spanish — Latin America and the Caribbean",
        "español latinoamericano",
        "es",
        region="419",
        direction=_ES_419_DIRECTION,
    ),
    LanguageEntry(
        "es-ES",
        "Spanish — Spain",
        "español de España",
        "es",
        region="ES",
        direction=_ES_ES_DIRECTION,
    ),
    LanguageEntry("ja", "Japanese", "日本語", "ja", iso3="jpn"),
    LanguageEntry("ko", "Korean", "한국어", "ko", iso3="kor"),
    LanguageEntry(
        "zh", "Chinese", "中文", "zh", iso3="chi", aliases=("zho", "mandarin", "cantonese")
    ),
    LanguageEntry("de", "German", "Deutsch", "de", iso3="ger", aliases=("deu",)),
    LanguageEntry("fr", "French", "français", "fr", iso3="fre", aliases=("fra",)),
    LanguageEntry("it", "Italian", "italiano", "it", iso3="ita"),
    LanguageEntry("pt", "Portuguese", "português", "pt", iso3="por"),
    LanguageEntry("ru", "Russian", "русский", "ru", iso3="rus"),
    LanguageEntry("hi", "Hindi", "हिन्दी", "hi", iso3="hin"),
    LanguageEntry("ar", "Arabic", "العربية", "ar", iso3="ara"),
    LanguageEntry("nl", "Dutch", "Nederlands", "nl", iso3="dut", aliases=("nld", "flemish")),
    LanguageEntry("sv", "Swedish", "svenska", "sv", iso3="swe"),
    LanguageEntry("no", "Norwegian", "norsk", "no", iso3="nor"),
    LanguageEntry("da", "Danish", "dansk", "da", iso3="dan"),
    LanguageEntry("fi", "Finnish", "suomi", "fi", iso3="fin"),
    LanguageEntry("pl", "Polish", "polski", "pl", iso3="pol"),
    LanguageEntry("tr", "Turkish", "Türkçe", "tr", iso3="tur"),
    LanguageEntry("th", "Thai", "ไทย", "th", iso3="tha"),
    LanguageEntry("vi", "Vietnamese", "Tiếng Việt", "vi", iso3="vie"),
    LanguageEntry("id", "Indonesian", "Bahasa Indonesia", "id", iso3="ind"),
    LanguageEntry("he", "Hebrew", "עברית", "he", iso3="heb"),
    LanguageEntry("el", "Greek", "Ελληνικά", "el", iso3="gre", aliases=("ell",)),
    LanguageEntry("cs", "Czech", "čeština", "cs", iso3="cze", aliases=("ces",)),
    LanguageEntry("hu", "Hungarian", "magyar", "hu", iso3="hun"),
    LanguageEntry("ro", "Romanian", "română", "ro", iso3="rum", aliases=("ron",)),
    LanguageEntry("uk", "Ukrainian", "українська", "uk", iso3="ukr"),
    LanguageEntry("tl", "Tagalog", "Tagalog", "tl", iso3="tgl"),
    LanguageEntry("ml", "Malayalam", "മലയാളം", "ml", iso3="mal"),
    LanguageEntry("ta", "Tamil", "தமிழ்", "ta", iso3="tam"),
    LanguageEntry("te", "Telugu", "తెలుగు", "te", iso3="tel"),
    LanguageEntry("fa", "Persian", "فارسی", "fa", iso3="per", aliases=("fas",)),
]

_CATALOG: dict[str, LanguageEntry] = {e.id: e for e in _ENTRIES}

# Lowercased English names, ISO 639-2 codes, and extra aliases -> canonical id.
_ALIASES: dict[str, str] = {}
for _e in _ENTRIES:
    _ALIASES[_e.name.lower()] = _e.id
    if _e.iso3:
        _ALIASES[_e.iso3] = _e.id
    for _a in _e.aliases:
        _ALIASES[_a.lower()] = _e.id


def parse(tag: str | None) -> str | None:
    """Canonical-case a BCP-47 subset tag (es-mx -> es-MX); None when malformed."""
    if not tag:
        return None
    m = _TAG.fullmatch(tag.strip())
    if not m:
        return None
    lang, script, region = m.groups()
    out = lang.lower()
    if script:
        out += "-" + script.title()
    if region:
        out += "-" + (region if region.isdigit() else region.upper())
    return out


def normalize(code: str | None) -> str | None:
    """Resolve aliases (eng, spa, Spanish…) and case to a canonical tag.

    Returns the catalog id when known, else the canonical parsed tag for a
    well-formed but uncatalogued tag, else None.
    """
    if not code or not code.strip():
        return None
    text = code.strip()
    alias = _ALIASES.get(text.lower())
    if alias:
        return alias
    return parse(text)


def get(tag: str | None) -> LanguageEntry | None:
    parsed = parse(tag)
    return _CATALOG.get(parsed) if parsed else None


def base_language(tag: str) -> str:
    parsed = parse(tag)
    if parsed:
        return parsed.split("-")[0]
    return tag.strip().split("-")[0].lower()


def display_name(tag: str) -> str:
    """'es-MX' -> 'Spanish — Mexico'; unknown codes fall back to the uppercased code."""
    entry = get(tag)
    return entry.name if entry else tag.strip().upper()


def native_name(tag: str) -> str:
    entry = get(tag)
    return entry.native_name if entry else display_name(tag)


def iso3_for(tag: str) -> str:
    """ISO 639-2/B code for media tagging; falls back to the raw base code."""
    base = base_language(tag)
    entry = _CATALOG.get(base)
    return entry.iso3 if entry and entry.iso3 else base


def is_supported(tag: str) -> bool:
    entry = get(tag)
    return bool(entry and entry.supported)


def catalog() -> list[LanguageEntry]:
    return list(_ENTRIES)


def supported_locales(base: str | None = None) -> list[LanguageEntry]:
    return [e for e in _ENTRIES if e.supported and (base is None or e.base == base)]


def resolve_target_locale(config: dict, target_lang: str) -> str:
    """Effective target locale for a job: the regional identity it runs with.

    A regional `target_lang` (es-MX) already carries its identity. Otherwise an
    explicit `dub.target_locale` matching the target's base language wins; a
    mismatching one is surfaced and ignored, never applied to another language.
    For Spanish targets a concrete legacy `translate.locale` still supplies the
    region. Falls back to the plain target language.
    """
    target = normalize(target_lang) or target_lang.strip()
    base = base_language(target)
    if base != target:
        return target
    explicit = str((config.get("dub") or {}).get("target_locale") or "").strip()
    if explicit:
        parsed = normalize(explicit)
        if parsed and base_language(parsed) == base:
            return parsed
        log.warning("dub.target_locale=%r is not a %s locale; ignoring it", explicit, target)
    if base == "es":
        legacy = str((config.get("translate") or {}).get("locale") or "").strip()
        if legacy and legacy != "auto":
            parsed = normalize(legacy)
            if parsed and base_language(parsed) == "es":
                return parsed
    return target
