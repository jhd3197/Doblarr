"""Spoken-content verification: what was asked for against what was heard.

This is the comparison half of D03. It owns no I/O and no policy side effects —
`stages/quality.py` decides which lines to listen to and pays for recognition;
everything here is pure text work over a pair of strings, so the hard cases can
be pinned down by fixtures without a recognizer.

Three things it refuses to do, because each one manufactures evidence:

- It never compares display wording. The comparison is against the *effective
  spoken text* the engine was actually given, so an approved pronunciation
  spelling ("Ginko" spoken as "Guinko") is a match, not an error.
- It never lets a whole-line similarity score decide. A single flipped negation
  scores ~0.95 and changes the meaning of the line, so critical terms are
  compared term by term and reported on their own.
- It never turns "we could not tell" into "we heard something wrong". Empty
  recognition, an unsupported language, a recognizer failure and a genuinely
  different word are four separate states.

Language support is honest about its limits. Word alignment needs tokens, and
tokens need a tokenizer; for scripts that do not put spaces between words the
fallback is character-level comparison, which is recorded as the tokenizer that
ran so nobody reads a character diff as a word diff.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .cues import Verification, now
from .fingerprints import verification as verification_fingerprint

CHECKER = "content-verify/1"

# Below this, the recognition is too different from the request for a word-level
# alignment to mean anything: report it as a mismatch, not as twenty edits.
WHOLESALE_MISMATCH = 0.35
# A line whose alignment is this close is reported as a match even with a stray
# edit, because recognizers routinely drop a clitic or merge two short words.
CLEAN_MATCH = 0.92
# Recognition confidence at or below this is uncertainty, not evidence.
LOW_CONFIDENCE = 0.4

# Scripts that do not delimit words with spaces. Splitting these on whitespace
# would produce one enormous "word" and hide every real difference, so they get
# character tokens and say so.
UNSPACED = frozenset({"ja", "zh", "yue", "wuu", "th", "lo", "km", "my", "bo"})

# Negation carriers. Losing or gaining one of these inverts the line, so they
# are compared individually whatever the whole-line similarity says. Languages
# absent from this table are not guessed at: their negation findings are simply
# not produced, and the verification records which table was available.
NEGATIONS: dict[str, frozenset[str]] = {
    "en": frozenset({"no", "not", "nt", "never", "none", "nothing", "nobody",
                     "nowhere", "neither", "nor", "cannot", "cant", "dont",
                     "doesnt", "didnt", "wont", "isnt", "arent", "wasnt",
                     "werent", "havent", "hasnt", "hadnt", "without"}),
    "es": frozenset({"no", "nunca", "jamas", "nada", "nadie", "ninguno",
                     "ninguna", "ningun", "ni", "tampoco", "sin"}),
    "pt": frozenset({"nao", "nunca", "jamais", "nada", "ninguem", "nenhum",
                     "nenhuma", "nem", "tampouco", "sem"}),
    "fr": frozenset({"ne", "pas", "non", "jamais", "rien", "personne",
                     "aucun", "aucune", "ni", "sans"}),
    "it": frozenset({"non", "mai", "niente", "nulla", "nessuno", "ne", "senza"}),
    "de": frozenset({"nicht", "nein", "kein", "keine", "keinen", "keiner",
                     "niemals", "nichts", "niemand", "ohne", "weder", "noch"}),
    "ja": frozenset({"ない", "ません", "なかっ", "いいえ", "しない", "ではない"}),
}

# Spelled-out numerals worth folding to digits so "veintitres" and "23" are the
# same term. Deliberately small and per-language: a general number grammar is
# not needed to notice that a price or a room number changed.
NUMBER_WORDS: dict[str, dict[str, str]] = {
    "en": {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
           "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
           "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
           "fourteen": "14", "fifteen": "15", "sixteen": "16",
           "seventeen": "17", "eighteen": "18", "nineteen": "19",
           "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
           "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
           "hundred": "100", "thousand": "1000"},
    "es": {"cero": "0", "uno": "1", "una": "1", "un": "1", "dos": "2",
           "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7",
           "ocho": "8", "nueve": "9", "diez": "10", "once": "11", "doce": "12",
           "trece": "13", "catorce": "14", "quince": "15", "dieciseis": "16",
           "diecisiete": "17", "dieciocho": "18", "diecinueve": "19",
           "veinte": "20", "treinta": "30", "cuarenta": "40", "cincuenta": "50",
           "sesenta": "60", "setenta": "70", "ochenta": "80", "noventa": "90",
           "cien": "100", "ciento": "100", "mil": "1000"},
}

# Prefixes that build a compound spoken numeral out of a table entry
# ("veintitres", "dieciocho"). Recognizing the *shape* is enough: the point is
# to tell "the recognizer spelled the number out" apart from "the number
# changed", not to compute its value.
NUMBER_PREFIXES: dict[str, tuple[str, ...]] = {
    "es": ("veinti", "dieci", "treinta", "cuarenta", "cincuenta", "sesenta",
           "setenta", "ochenta", "noventa", "ciento", "cientos", "mill"),
    "en": ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
           "ninety", "hundred", "thousand", "million"),
}

_DIGITS = re.compile(r"\d")
_WORD = re.compile(r"\w+", re.UNICODE)
# Separators that carry no spoken content. Everything else — including letters
# with diacritics and CJK — survives normalization.
_STRIP = re.compile(r"[^\w\s]", re.UNICODE)


def base_language(language: str) -> str:
    """'es-MX' -> 'es'. Comparison tables are keyed by base language."""
    return str(language or "").replace("_", "-").split("-")[0].lower()


def fold(text: str) -> str:
    """Casefold and strip accents so 'jamás' and 'jamas' are the same token.

    Accent folding is deliberate: a recognizer that drops a diacritic has not
    said a different word, and treating it as one would fill review with noise.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFKC", stripped).casefold()


def normalize(text: str, language: str = "") -> str:
    """Drop punctuation and collapse whitespace, keeping every spoken character."""
    folded = fold(text)
    return " ".join(_STRIP.sub(" ", folded).split())


def canonical(token: str, language: str = "") -> str:
    """One token's comparison form, with small spelled-out numbers folded to digits."""
    word = fold(token).strip()
    if not word:
        return ""
    table = NUMBER_WORDS.get(base_language(language), {})
    return table.get(word, word)


def tokenize(text: str, language: str = "") -> tuple[list[str], str]:
    """Comparison tokens plus the name of the tokenizer that produced them.

    Returns character tokens for scripts without word spacing, and says so, so
    a character-level diff is never displayed as a list of missing words.
    """
    normalized = normalize(text, language)
    if not normalized:
        return [], "empty"
    code = base_language(language)
    if code in UNSPACED or (" " not in normalized and _needs_characters(normalized)):
        return [c for c in normalized if not c.isspace()], f"character/{code or 'und'}"
    return [canonical(t, language) for t in normalized.split() if t], f"whitespace/{code or 'und'}"


def _needs_characters(text: str) -> bool:
    """True when an unspaced string is CJK/Thai rather than one long Latin word."""
    for char in text:
        if char.isspace():
            continue
        name = unicodedata.name(char, "")
        if name.startswith(("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO",
                            "KHMER", "MYANMAR", "HANGUL")):
            return True
    return False


def is_number(token: str) -> bool:
    """A written numeral. Spelled-out numbers are `spelled_number` instead."""
    return bool(_DIGITS.search(token))


def spelled_number(token: str, language: str) -> bool:
    """A number said in words, including the common compounds.

    Shape only. Knowing that "veintitres" is *a* number is what separates "the
    recognizer wrote it out" from "the number changed"; knowing that it is 23
    would need a number grammar per language, which this does not claim to be.
    """
    code = base_language(language)
    table = NUMBER_WORDS.get(code, {})
    if token in table or token in table.values():
        return True
    return any(token.startswith(prefix) for prefix in NUMBER_PREFIXES.get(code, ()))


def numeric(token: str, language: str) -> bool:
    return is_number(token) or spelled_number(token, language)


def is_negation(token: str, language: str) -> bool:
    return token in NEGATIONS.get(base_language(language), frozenset())


def supports_negation(language: str) -> bool:
    return base_language(language) in NEGATIONS


def names(text: str, language: str) -> set[str]:
    """Capitalized mid-sentence words, as a best-effort proper-noun signal.

    Only meaningful for cased scripts; for everything else this returns nothing
    rather than a guess, and `critical_terms` records that names were not
    checked instead of silently reporting none.
    """
    if not cased(language):
        return set()
    found: set[str] = set()
    for sentence in re.split(r"[.!?\n]+", str(text)):
        words = _WORD.findall(sentence)
        for position, word in enumerate(words):
            if position and word[:1].isupper() and not word.isupper():
                found.add(canonical(word, language))
    return found


def cased(language: str) -> bool:
    """Whether this language's script distinguishes upper and lower case."""
    return base_language(language) not in UNSPACED and base_language(language) not in {
        "ar", "he", "fa", "ur", "hi", "bn", "ta", "te", "ka", "am"}


def critical_terms(text: str, language: str) -> list[dict]:
    """Numbers, negations and probable names in the expected text.

    Each entry says which rule found it, so review can show *why* a term is
    treated as critical and a language without a negation table is visibly
    unsupported rather than quietly clean.
    """
    tokens, _ = tokenize(text, language)
    proper = names(text, language)
    terms: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        kinds = []
        if numeric(token, language):
            kinds.append("number")
        if is_negation(token, language):
            kinds.append("negation")
        if token in proper:
            kinds.append("name")
        for kind in kinds:
            if (kind, token) in seen:
                continue
            seen.add((kind, token))
            terms.append({"kind": kind, "term": token})
    return terms


def repeated_spans(tokens: list[str], minimum: int = 3) -> list[dict]:
    """Runs of one token repeated `minimum` times or more."""
    spans: list[dict] = []
    start = 0
    for index in range(1, len(tokens) + 1):
        if index < len(tokens) and tokens[index] == tokens[start]:
            continue
        if index - start >= minimum:
            spans.append({"term": tokens[start], "start": start, "count": index - start})
        start = index
    return spans


def differences(expected: list[str], heard: list[str]) -> list[dict]:
    """Ordered omissions, insertions and substitutions between two token lists.

    The ops are the alignment itself, not a summary of it: each one carries the
    position in the expected text so review can point at the place in the line.
    """
    ops: list[dict] = []
    matcher = SequenceMatcher(None, expected, heard, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        entry = {"op": {"delete": "omission", "insert": "insertion",
                        "replace": "substitution"}[tag],
                 "at": i1,
                 "expected": expected[i1:i2],
                 "heard": heard[j1:j2]}
        ops.append(entry)
    return ops


def _critical_severity(term: dict, expected: list[str], heard: list[str],
                       language: str) -> tuple[str, str]:
    """How seriously to take one missing critical term.

    A dropped or altered number and a changed negation are hard evidence. A
    name is not: recognizers mangle proper nouns constantly, and calling that a
    wrong word would bury real findings under noise. A number the recognizer
    spelled out (or wrote in digits) is the same number said differently until
    something proves otherwise, so it is surfaced as uncertain, not as an error.
    """
    kind, word = term["kind"], term["term"]
    if kind == "negation":
        return "mismatch", "recognition dropped a negation"
    if kind == "name":
        return "uncertain", f"the name {word!r} was not recognized as written"
    # kind == "number"
    expected_numbers = {t for t in expected if numeric(t, language)}
    heard_numbers = {t for t in heard if numeric(t, language)}
    new_numbers = heard_numbers - expected_numbers
    if not heard_numbers:
        return "mismatch", f"the number {word!r} was not heard at all"
    written_out = {t for t in new_numbers if is_number(word) != is_number(t)}
    if written_out:
        return "uncertain", (f"the number {word!r} may have been said as "
                             f"{sorted(written_out)[0]!r}")
    if new_numbers:
        return "mismatch", f"the number {word!r} was heard as {sorted(new_numbers)[0]!r}"
    return "mismatch", f"the number {word!r} was not heard"


def _term_counts(tokens: list[str], terms: list[dict]) -> dict[str, int]:
    wanted = {t["term"] for t in terms}
    counts = dict.fromkeys(wanted, 0)
    for token in tokens:
        if token in wanted:
            counts[token] += 1
    return counts


def compare(expected_text: str, heard_text: str, language: str = "",
            confidence: float | None = None, heard_language: str = "") -> dict:
    """Compare one expected line against one recognition, with no side effects.

    Returns the fields of a `Verification` that this comparison can establish.
    The caller owns policy, caching, findings and any repair.

    `heard_language` is what the recognizer says it transcribed, when it says
    so. A recognition of a different language is not evidence about these
    words, so it is reported as unsupported rather than as a wrong line.
    """
    expected_tokens, tokenizer = tokenize(expected_text, language)
    heard_tokens, _ = tokenize(heard_text, language)
    terms = critical_terms(expected_text, language)
    result: dict = {
        "expected": str(expected_text or ""),
        "heard": str(heard_text or ""),
        "language": language,
        "tokenizer": tokenizer,
        "confidence": confidence,
        "differences": [],
        "critical": [],
        "checker": CHECKER,
    }
    if not expected_tokens:
        result["state"] = "skipped"
        result["reason"] = "the line has no spoken text to verify"
        return result
    if not heard_tokens:
        # Silence from the recognizer is a recognition outcome, not proof that
        # the clip is empty — the clip checks own that judgement.
        result["state"] = "empty"
        result["reason"] = "recognition returned no words"
        result["similarity"] = 0.0
        return result
    if (heard_language and language
            and base_language(heard_language) != base_language(language)):
        result["state"] = "unsupported"
        result["reason"] = (f"the recognizer transcribed this as "
                            f"{base_language(heard_language)}, not "
                            f"{base_language(language)}")
        return result

    similarity = SequenceMatcher(None, expected_tokens, heard_tokens,
                                 autojunk=False).ratio()
    result["similarity"] = round(similarity, 4)
    result["differences"] = differences(expected_tokens, heard_tokens)

    # Critical terms are counted, not aligned: a name that moved is still said,
    # and a negation that disappeared is a different line however it aligned.
    # Each entry carries its own severity, because the three kinds do not
    # deserve the same verdict — see `_critical_severity`.
    expected_counts = _term_counts(expected_tokens, terms)
    heard_counts = _term_counts(heard_tokens, terms)
    for term in terms:
        word = term["term"]
        wanted, got = expected_counts[word], heard_counts[word]
        if wanted == got:
            continue
        severity, note = _critical_severity(term, expected_tokens, heard_tokens, language)
        result["critical"].append({**term, "expected": wanted, "heard": got,
                                   "missing": max(0, wanted - got),
                                   "severity": severity, "note": note})
    # A negation the recognizer *added* inverts the line just as badly.
    if supports_negation(language):
        for token in sorted(set(heard_tokens)):
            if is_negation(token, language) and token not in expected_counts:
                result["critical"].append(
                    {"kind": "negation", "term": token, "expected": 0,
                     "heard": heard_tokens.count(token), "missing": 0,
                     "severity": "mismatch", "note": "recognition added a negation"})
    elif any(t["kind"] == "negation" for t in terms):  # pragma: no cover - table-driven
        result["notes"] = ["no negation table for this language"]
    if not cased(language):
        result["notes"] = [*result.get("notes", []), "names are not checked in this script"]

    intentional = {span["term"] for span in repeated_spans(expected_tokens)}
    loops = [span for span in repeated_spans(heard_tokens, minimum=4)
             if span["term"] not in intentional]

    soft = [c for c in result["critical"] if c.get("severity") != "mismatch"]
    if confidence is not None and confidence <= LOW_CONFIDENCE and not result["critical"]:
        result["state"] = "uncertain"
        result["reason"] = f"recognizer reported low confidence ({confidence:.2f})"
        return result
    hard = [c for c in result["critical"] if c.get("severity") == "mismatch"]
    if hard:
        kinds = sorted({c["kind"] for c in hard})
        result["state"] = "mismatch"
        result["reason"] = f"critical term difference ({', '.join(kinds)})"
        return result
    if loops:
        result["state"] = "mismatch"
        result["reason"] = f"recognition repeats {loops[0]['term']!r} {loops[0]['count']} times"
        result["repetition"] = loops
        return result
    if similarity < WHOLESALE_MISMATCH:
        result["state"] = "mismatch"
        result["reason"] = "recognition bears little resemblance to the requested line"
        return result
    if soft:
        result["state"] = "uncertain"
        result["reason"] = soft[0]["note"] or f"{soft[0]['kind']} term differs"
        return result
    if similarity >= CLEAN_MATCH or not result["differences"]:
        result["state"] = "match"
        result["reason"] = "recognition matches the requested line"
        return result
    # Somewhere in between: real edits, no critical term involved. That is
    # evidence worth showing a reviewer and not proof of a wrong word.
    result["state"] = "uncertain"
    result["reason"] = "recognition differs from the requested line"
    return result


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

POLICIES = ("off", "suspicious", "all")
# Deterministic sampling: a line is sampled when this many leading hex digits
# of its verification key fall inside the configured fraction.
_SAMPLE_SPACE = 1 << 16


def expected_for(seg, pronunciations=None) -> str:
    """The exact text the engine was asked to speak for this cue.

    `tts_text` is what synthesis resolved after pronunciation and locale rules;
    the flat legacy map is the fallback for a run without the resolver. Display
    wording is never used, because comparing it would flag every approved
    spoken substitution as an error.
    """
    if seg.tts_text:
        return seg.tts_text
    from .stages.quality import spoken_form

    return spoken_form(seg.text_translated or seg.text_src, pronunciations or {})


def sampled(key: str, fraction: float) -> bool:
    """Deterministic membership in a sampling fraction, stable across reruns."""
    if fraction <= 0:
        return False
    if fraction >= 1:
        return True
    try:
        bucket = int(key[:4], 16)
    except (TypeError, ValueError):
        return False
    return bucket < int(_SAMPLE_SPACE * fraction)


def should_check(seg, policy: str, key: str = "", sample: float = 0.0,
                 acoustic_issues=()) -> tuple[bool, str]:
    """Whether to spend recognition on this line, and the reason either way.

    The reason is recorded on the cue: a line that was never listened to must
    never read as verified, and a reviewer has to be able to see which of the
    two it is.
    """
    if policy not in POLICIES:
        raise ValueError("quality.asr must be off, suspicious or all")
    if policy == "off":
        return False, "verification is off"
    if policy == "all":
        return True, "all-mode covers every generated line"
    reasons = []
    if acoustic_issues:
        reasons.append("acoustic issues were reported")
    if seg.revision:
        reasons.append("the line was regenerated")
    if seg.translation_provenance.get("timing_rewritten"):
        reasons.append("timing repair rewrote the wording")
    if len(seg.audio.takes) > 1:
        reasons.append("a take was chosen from several candidates")
    if critical_terms(expected_for(seg), ""):
        reasons.append("the line carries a name, number or negation")
    if reasons:
        return True, "; ".join(reasons)
    if sample and sampled(key, sample):
        return True, f"deterministic {sample:.0%} sample of clean lines"
    return False, "no suspicion and not sampled"


def verification_key(*, audio: str, expected: str, language: str, policy: str,
                     recognizer: str) -> str:
    """Identity of one check: the audio, the words, the language and the checker.

    Deliberately excludes anything machine-local, so moving a work directory
    does not invalidate evidence, and deliberately includes the checker and
    recognizer versions, so a checker upgrade re-checks without touching audio.
    """
    return verification_fingerprint({
        "audio": audio,
        "expected": normalize(expected, language),
        "language": language,
        "policy": policy,
        "recognizer": recognizer,
        "checker": CHECKER,
    })


def skipped(seg, policy: str, reason: str, language: str = "",
            expected: str = "") -> Verification:
    """A cue that was deliberately not listened to, recorded as exactly that."""
    return Verification(state="skipped", policy=policy, reason=reason,
                        expected=expected, language=language, checker=CHECKER,
                        at=now(), attempts=seg.verification.attempts)


def failure(seg, policy: str, reason: str, language: str, expected: str,
            recognizer: str = "") -> Verification:
    """A recognizer that could not answer. Reviewable, never an approval."""
    return Verification(state="failed", policy=policy, reason=reason,
                        expected=expected, language=language,
                        recognizer=recognizer, checker=CHECKER, at=now(),
                        attempts=seg.verification.attempts)


# Finding codes this checker can raise. `content_unverified` is deliberately
# informational: not knowing is worth showing and is not a defect in the audio.
CODES = {
    "content_mismatch": ("content", "error"),
    "content_uncertain": ("content", "warning"),
    "content_critical_term": ("content", "error"),
    "content_repetition": ("content", "warning"),
    "content_unverified": ("content", "info"),
}


def findings_for(result: Verification) -> list[tuple]:
    """Observations for `stages.quality.apply_findings`, from one verification."""
    observed: list[tuple] = []
    evidence = {
        "expected": result.expected,
        "heard": result.heard,
        "similarity": result.similarity,
        "tokenizer": result.tokenizer,
        "differences": result.differences[:12],
        "critical": result.critical,
        "reason": result.reason,
    }
    if result.state == "mismatch":
        code = "content_critical_term" if result.critical else "content_mismatch"
        kind, severity = CODES[code]
        observed.append((code, kind, severity, result.similarity, evidence))
    elif result.state == "uncertain":
        kind, severity = CODES["content_uncertain"]
        observed.append(("content_uncertain", kind, severity, result.similarity, evidence))
    elif result.state in ("empty", "failed", "unsupported"):
        kind, severity = CODES["content_unverified"]
        observed.append(("content_unverified", kind, severity, None,
                         {**evidence, "state": result.state}))
    return observed
