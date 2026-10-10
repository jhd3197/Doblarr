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

from .cues import apply_findings
from .languages import base_language, parse

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
        "pillado": "atrapado", "pillé": "atrapé", "pillaste": "atrapaste",
        "pilla": "atrapa", "pillas": "atrapas", "pillo": "atrapo", "pillamos": "atrapamos",
        "vale": "bien / está bien / de acuerdo",
        "ordenador": "computadora", "ordenadores": "computadoras", "nevera": "refrigerador",
        "patata": "papa", "patatas": "papas", "zumo": "jugo", "aparcar": "estacionar",
        "despacho": "oficina",
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


# "Ir a por algo" — 182 Spain subtitle lines across 29 titles, 5 Latin American.
_A_POR = re.compile(r"\ba por\b", re.IGNORECASE)
# Vosotros verb forms by their endings: present (habláis, coméis), preterite
# (hablasteis, comisteis). No Latin American variety uses them in speech.
_VOSOTROS_VERB = re.compile(r"\b\w+(?:áis|éis|asteis|isteis)\b", re.IGNORECASE)
# Vosotros imperatives: a closed list, since "-ad/-ed/-id" also ends nouns
# (verdad, pared, Madrid).
_VOSOTROS_IMPERATIVE = re.compile(
    r"\b(?:pasad|entrad|salid|venid|idos|tened|decid|callad|dejad|tomad|traed|haced|"
    r"poned|volved|seguid|subid|bajad|atacad|luchad|huid|ayudad|daos|sentaos|quedaos|"
    r"largaos|preparaos|calmaos|moveos|levantaos|apartaos|esconded|corred|mirad)\b",
    re.IGNORECASE)


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
    for pattern in (_VOSOTROS_VERB, _VOSOTROS_IMPERATIVE):
        for match in pattern.finditer(text):
            if match.group(0).lower() not in seen:
                seen.add(match.group(0).lower())
                hits.append({"word": match.group(0), "category": "vosotros",
                             "suggest": "the ustedes form"})
    found = _A_POR.search(text)
    if found:
        hits.append({"word": found.group(0), "category": "grammar",
                     "suggest": "por (voy por agua)"})
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


def revise(job, translator) -> int:
    """Rewrite the lines `check` flagged into Latin American Spanish.

    The translator is asked to replace exactly the Spain-only words found
    (vosotros, "pillar", "vale"…) with their Latin American alternatives, and
    to prefer the simple past for something just done ("¿Volviste?" over
    "¿Has vuelto?"). A rewrite is kept only when it no longer has a marker;
    otherwise the line keeps its finding for a person. Returns lines fixed.
    """
    revise_line = getattr(translator, "revise", None)
    locale = job.target_locale or job.target_lang
    if revise_line is None or not applies(locale):
        return 0
    fixed = 0
    for seg in job.segments:
        hits = markers(seg.text_translated or "")
        if not hits:
            continue
        swaps = "; ".join(f"{h['word']} -> {h['suggest']}" for h in hits)
        try:
            better = revise_line(
                seg.text_translated, locale,
                f"Rewrite this line in Latin American Spanish ({locale}). Replace the Spain-only "
                f"Spanish: {swaps}. Use ustedes, never vosotros; prefer the simple past for "
                "something that just happened.")
        except Exception:  # noqa: BLE001 - a line that cannot be revised keeps its finding
            continue
        if better and better.strip() and not markers(better):
            seg.text_translated = better.strip()
            seg.tts_text = ""
            fixed += 1
    if fixed:
        job.metrics["spain_spanish_revised"] = fixed
        check(job)
    return fixed
