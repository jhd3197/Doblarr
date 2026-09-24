"""Flag Spain-only Spanish in a Latin American dub's translated text.

The Latin American translator directions (see `doblarr.languages`) already ask
for ustedes, no "vale" and so on; this is the check that the translation
actually followed them. It reads text only, costs nothing, and never edits a
line — each hit becomes a review finding on that cue.

The markers were measured, not guessed: across ~50 titles with both a Latin
American and a Spain subtitle track, every word below appears in the Spain
track of at least 8 titles and (almost) never in the Latin American one. Words
that are Spain-leaning but also normal in some Latin American country ("rollo"
is everyday Mexican, "tío" is also "uncle", and Latin American subtitles use
"puto"/"joder" on purpose for strong English profanity) are deliberately left
out: a finding must be worth a reviewer's time. Measured on the same corpus,
it flags ~6.6% of Spain lines and ~0.04% of Latin American ones.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .languages import base_language, parse
from .stages.quality import apply_findings

DETECTOR = "dialect/1"
CODE = "spain_spanish"

# category -> {marker: Latin American alternative}
_MARKERS: dict[str, dict[str, str]] = {
    "vosotros": {
        "vosotros": "ustedes", "vosotras": "ustedes", "os": "les / los / las",
        "vuestro": "su", "vuestra": "su", "vuestros": "sus", "vuestras": "sus",
        "sois": "son", "habéis": "han", "estáis": "están", "tenéis": "tienen",
        "sabéis": "saben", "queréis": "quieren", "podéis": "pueden",
        "hacéis": "hacen", "vais": "van", "veis": "ven", "hagáis": "hagan",
        "mirad": "miren", "esperad": "esperen", "escuchad": "escuchen",
        "venid": "vengan", "corred": "corran", "abrid": "abran", "parad": "paren",
        "callaos": "cállense", "marchaos": "váyanse",
    },
    "vocabulary": {
        "guay": "genial / padre", "mola": "está genial", "molan": "están geniales",
        "chaval": "chico / muchacho", "chavales": "chicos", "móvil": "celular",
        "coche": "auto / carro", "enhorabuena": "felicidades",
        "apetece": "tengo ganas / quiero", "pillar": "atrapar / agarrar",
        "pillado": "atrapado", "pillé": "atrapé", "vale": "bien / está bien / de acuerdo",
    },
    # "Take/grab" in Spain, "to fuck" in much of Latin America. Latin American
    # subtitles use it on purpose for the sexual sense, so this needs a human:
    # the finding asks which meaning the line has.
    "coger": {
        "coger": "agarrar / tomar", "coge": "agarra / toma", "coges": "agarras",
        "cogí": "agarré", "cogió": "agarró", "cogiste": "agarraste",
        "cogido": "agarrado", "cogida": "agarrada", "cógelo": "agárralo",
        "cógela": "agárrala", "cogerlo": "agarrarlo", "cogerla": "agarrarla",
        "cogemos": "agarramos", "cogen": "agarran",
    },
    "profanity": {
        "hostia": "mierda / maldición", "hostias": "mierda", "gilipollas": "idiota / imbécil",
        "capullo": "idiota", "coño": "carajo / maldita sea", "cojones": "carajo",
    },
}
_WORD = {word: (category, alternative)
         for category, words in _MARKERS.items() for word, alternative in words.items()}
_TOKEN = re.compile(r"\w+", re.UNICODE)
# "vale" is Spain's "okay" only when it stands alone as its own clause
# ("Vale.", "Vale, gracias", "Bueno, vale."); "vale la pena" and "¿cuánto
# vale?" are shared. Capitalised mid-sentence it is a name ("el Norte, Vale,").
_VALE = re.compile(r"(?:(?:^|[.!?¡¿…—-]\s*)[Vv]ale|[,;:]\s*vale)\s*(?:[.!?,;:…]|$)")


def applies(target_locale: str | None) -> bool:
    """A regional Spanish target outside Spain (es-419, es-MX, es-VE, …)."""
    tag = parse(target_locale or "")
    if not tag or base_language(tag) != "es" or "-" not in tag:
        return False
    return tag.split("-")[-1] != "ES"


def markers(text: str) -> list[dict]:
    """Spain-only words in one line, each with a Latin American alternative."""
    text = unicodedata.normalize("NFC", text or "")
    hits, seen = [], set()
    for token in _TOKEN.findall(text):
        word = token.lower()
        if word == "vale" or word in seen or word not in _WORD:
            continue
        seen.add(word)
        category, alternative = _WORD[word]
        hits.append({"word": token, "category": category, "suggest": alternative})
    if _VALE.search(text):
        hits.append({"word": "vale", "category": "vocabulary",
                     "suggest": _WORD["vale"][1]})
    return hits


def check(job) -> int:
    """Record a finding on every translated cue that uses Spain-only Spanish.

    Returns how many lines were flagged. A job whose target is not Latin
    American clears any earlier findings from this detector instead.
    """
    locale = job.target_locale or job.target_lang
    active = applies(locale)
    flagged = 0
    for seg in job.segments:
        text = seg.text_translated or ""
        inputs = hashlib.sha1(f"{DETECTOR}|{locale}|{text}".encode()).hexdigest()[:16]
        hits = markers(text) if active else []
        observed = []
        if hits:
            flagged += 1
            observed.append((CODE, "content", "warning", None, {
                "locale": locale, "text": text, "markers": hits,
                "note": "Spain-only Spanish in a Latin American dub",
            }))
        apply_findings(seg, DETECTOR, inputs, observed)
    if active:
        job.metrics["spain_spanish_lines"] = flagged
    else:
        job.metrics.pop("spain_spanish_lines", None)
    return flagged
