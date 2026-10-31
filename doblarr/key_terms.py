"""The names and terms an episode keeps saying, and whether its dub keeps them.

A translator working a batch of lines at a time renders the same term one way
here and another way twenty lines later ("the harbor" as "el puerto", then as
"la bahía"). Three parts keep a show's wording steady:

- `find` picks the key terms from the source lines with no model at all:
  phrases capitalised mid-sentence ("Lantern Exam", "Cove of the Fallen
  Leaves"), the words inside them with their lowercase uses ("cove"), and
  names with an honorific. A capitalised word that is far more often written
  in lowercase ("The", "What") is ordinary English, and a word capitalised
  only at the start of a sentence ("Hm", "Well") is not a term. Plurals count
  as one term ("Leaves" is "Leaf").
- `read_back` reads how a set of lines rendered one term: the wording that
  appears in most of the term's lines and rarely elsewhere. It works on any
  text lined up with the source: this dub's translation, or the transcript of
  an official dub track.
- `enforce` makes the decided renderings hold: every line whose source says a
  term must say its rendering, or it is rewritten (kept only when the rewrite
  does), or it keeps a finding for a person.

Matching ignores case, accents, plural endings and leading articles, so
"la Ensenada Oculta del Faro" holds in "las ensenadas ocultas del Faro".
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

READER = "key-terms/1"
DETECTOR = "key-terms/1"
CODE = "term_inconsistent"

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_PHRASE_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*|[,;:()]")
_CONNECT = {"of", "the", "in"}
_HONORIFIC = re.compile(r"-(sensei|san|sama|chan|kun|dono|senpai|sempai|niisan|neesan)$", re.I)
# Sentence boundaries, plus quotes and dashes: a word after them is
# capitalised for the sentence, not for itself.
_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\.\.\.|…|—|[\"“”«»]")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_ARTICLES = {"el", "la", "los", "las", "lo", "un", "una", "the", "a", "an"}
# Words that never start or end a rendering ("del Faro" is "Faro").
_EDGE = {"de", "del", "y", "a", "al", "en", "que", "se", "su", "sus", "por", "con", "para",
         "es", "no", "mi", "me", "te", "tu", "le", "les", "nos", "o", "of", "and", "to", "in",
         "is", "it", "on", "for", "with", *_ARTICLES}


def fold(text: str) -> str:
    """Lowercase without accents."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _singular(word: str) -> str:
    """One form for singular and plural, in English and Spanish alike.

    Crude on purpose: it only has to map a word and its plural to the same
    key ("leaves"/"leaf", "exams"/"exam", "puertos"/"puerto",
    "examenes"/"examen"), never to produce a real word.
    """
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("ves"):
        return w[:-3] + "f"
    if w.endswith("es") and w[-3] in "nrldz" and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    """The comparable words of a text: folded and singular."""
    return [_singular(t) for t in _TOKEN.findall(fold(text))]


def _key(phrase: str) -> list[str]:
    """The words a rendering must show, without a leading article."""
    words = tokens(phrase)
    while words and words[0] in _ARTICLES:
        words = words[1:]
    return words


def says(text: str, phrase: str) -> bool:
    """Whether `text` contains `phrase`, by its words in order or near it.

    The words may sit one apart and swap places ("Mira-sensei" holds in
    "la sensei Mira"), but must all be there."""
    want = _key(phrase)
    if not want:
        return True
    have = tokens(text)
    width = len(want) + 1
    for i in range(len(have)):
        window = have[i:i + width]
        if want[0] not in window and window[:1] != want[:1]:
            continue
        if all(w in window for w in want):
            return True
    return False


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCES.split(text or "") if s and s.strip()]


def _words(sentence: str) -> list[str]:
    return [re.sub(r"'s$", "", t) for t in _WORD.findall(sentence)]


def find(texts: list[str], min_lines: int = 2) -> list[dict]:
    """The key terms of a script: [{term, lines, kind}], most-said first.

    `lines` are positions in `texts`. `kind` is "name" for a single
    capitalised word or a name with an honorific, "term" for the rest; what
    a term really is (a name the dub keeps, a word it translates) shows in
    `read_back`, not here.
    """
    cap_mid: Counter[str] = Counter()
    lowercase: Counter[str] = Counter()
    for text in texts:
        for sentence in _sentences(text):
            for i, word in enumerate(_words(sentence)):
                if "'" in word:
                    continue
                if word[0].isupper():
                    if i:
                        cap_mid[_singular(word)] += 1
                else:
                    lowercase[_singular(word)] += 1

    def proper(word: str) -> bool:
        if _HONORIFIC.search(word):
            return True
        base = _singular(word)
        return cap_mid[base] > 0 and cap_mid[base] >= lowercase[base]

    phrases: dict[str, set[int]] = {}
    shown: dict[str, str] = {}
    for n, text in enumerate(texts):
        for sentence in _sentences(text):
            # Commas and the like stay in as tokens: a phrase never runs
            # across one ("Mira-sensei, the Lantern Exam" is two).
            words = [re.sub(r"'s$", "", t) for t in _PHRASE_TOKEN.findall(sentence)]
            runs: list[list[str]] = [[]]
            for i, word in enumerate(words):
                run = runs[-1]
                if "'" in word or word == "I":
                    runs.append([])
                elif word[0].isupper() and (proper(word) or (i and (
                        run or (i + 1 < len(words) and words[i + 1][:1].isupper())))):
                    # Alone, a capitalised word must be capitalised more
                    # often than not; inside a capitalised phrase mid-sentence
                    # ("the Third Lantern Docks") it belongs to the phrase.
                    run.append(word)
                elif (run and word in _CONNECT and i + 1 < len(words)
                      and words[i + 1][0].isupper()):
                    run.append(word)
                else:
                    runs.append([])
            for run in runs:
                while run and run[-1] in _CONNECT:
                    run.pop()
                if run:
                    key = " ".join(_singular(w) for w in run)
                    phrases.setdefault(key, set()).add(n)
                    shown.setdefault(key, " ".join(run))

    found: dict[str, set[int]] = {}
    multi = {k: v for k, v in phrases.items()
             if len([w for w in k.split() if w not in _CONNECT]) > 1}
    for key, lines in phrases.items():
        words = [w for w in key.split() if w not in _CONNECT]
        if len(words) > 1 or cap_mid[key] >= 2 or _HONORIFIC.search(key):
            found.setdefault(key, set()).update(lines)
    # What two phrases share is a term too: "Third Lantern Docks" and
    # "Lantern Dock" are both the lantern dock.
    keys = sorted(multi)
    for a_i, a in enumerate(keys):
        for b in keys[a_i + 1:]:
            shared = _shared_run(a.split(), b.split())
            if len([w for w in shared if w not in _CONNECT]) >= 2:
                key = " ".join(shared)
                found.setdefault(key, set()).update(multi[a] | multi[b])
                shown.setdefault(key, _shown_run(shown[a], shared))
    # The words inside a multi-word term, with their lowercase uses
    # ("cove" in "the cove"), and the name inside "Name-sensei".
    parts: dict[str, set[int]] = {}
    for key in list(found):
        words = [w for w in key.split() if w not in _CONNECT]
        if len(words) > 1:
            for w in words:
                if len(found[key]) >= min_lines or (cap_mid[w] >= 2 and proper(w)):
                    parts.setdefault(w, set())
        if _HONORIFIC.search(key) and " " not in key:
            parts.setdefault(_HONORIFIC.sub("", key), set())
    for word in parts:
        pattern = re.compile(rf"(?<![\w'-]){re.escape(word)}(?:s|es)?(?![\w'])", re.I)
        leaves = re.compile(rf"\b{re.escape(word[:-1])}ves\b", re.I) if word.endswith("f") else None
        for n, text in enumerate(texts):
            if pattern.search(text) or (leaves and leaves.search(text)):
                parts[word].add(n)
        shown.setdefault(word, _display(word, texts))
        found.setdefault(word, set()).update(parts[word])
    out = []
    for key, lines in found.items():
        if len(lines) < min_lines:
            continue
        # A word only ever said inside one longer term is that term.
        if " " not in key and any(
                " " in other and key in other.split() and found[other] == lines
                for other in found):
            continue
        name = shown.get(key) or key
        kind = "name" if (" " not in key or _HONORIFIC.search(key)) else "term"
        out.append({"term": name, "lines": sorted(lines), "kind": kind})
    out.sort(key=lambda t: (-len(t["lines"]), t["term"]))
    return out


def _shared_run(a: list[str], b: list[str]) -> list[str]:
    best: list[str] = []
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > len(best):
                best = a[i:i + k]
    while best and best[0] in _CONNECT:
        best = best[1:]
    while best and best[-1] in _CONNECT:
        best = best[:-1]
    return best


def _shown_run(shown: str, shared: list[str]) -> str:
    words = shown.split()
    keys = [_singular(w) for w in words]
    for i in range(len(keys)):
        if keys[i:i + len(shared)] == shared:
            return " ".join(words[i:i + len(shared)])
    return " ".join(shared)


def _display(word: str, texts: list[str]) -> str:
    """How a term word is written in the script, capitalised if it ever is."""
    pattern = re.compile(rf"\b({re.escape(word)})\b", re.I)
    forms = Counter(m.group(1) for t in texts for m in pattern.finditer(t))
    capital = [f for f in forms if f[:1].isupper()]
    return max(capital or forms or [word], key=lambda f: forms.get(f, 0))


def _grams(text: str, longest: int = 4) -> set[str]:
    words = tokens(text)
    out = set()
    for a in range(len(words)):
        for b in range(a + 1, min(len(words), a + longest) + 1):
            gram = words[a:b]
            if gram[0] in _EDGE or gram[-1] in _EDGE:
                continue
            out.add(" ".join(gram))
    return out


def read_back(term: str, lines: list[int], renders: list[str],
              sources: list[str] | None = None) -> dict:
    """How the lines rendered `term`: {rendering, share, variants, missing}.

    `renders` are the target texts, one per source line (the same positions
    `find` returned). The rendering is the wording found in most of the
    term's lines and rarely elsewhere; a term kept as it is ("Harbor Guild") is
    its own rendering. `share` is the part of the term's lines that say it;
    `variants` are the other wordings with the lines that used them, and
    `missing` the lines with none of them. A rendering is a key, folded and
    singular ("ensenada del faro"); `example` is it as one line wrote it.
    With `sources` (the source lines), a word a line copied from its own
    source ("Kaito" beside the term) is another word's rendering, not this
    term's.
    """
    lines = [n for n in lines if 0 <= n < len(renders) and (renders[n] or "").strip()]
    if not lines:
        return {"rendering": None, "share": 0.0, "variants": [], "missing": [], "example": ""}
    if sum(says(renders[n], term) for n in lines) * 2 >= len(lines):
        return _summary(term, lines, renders, " ".join(_key(term)), [])
    grams = [_grams(t) for t in renders]
    if sources is not None:
        own = set(tokens(term))
        for n, text in enumerate(sources[:len(grams)]):
            copied = set(tokens(text)) - own
            grams[n] = {g for g in grams[n] if not set(g.split()) <= copied}
    frequency = Counter(g for gs in grams for g in gs)
    support = Counter(g for n in lines for g in grams[n])
    content = len([w for w in term.split() if w.lower() not in _CONNECT])
    wanted = min(2, content)

    def score(gram: str) -> float:
        # Said in many of the term's lines, and mostly there: "puerto" in four
        # of seven beats "puerto escondido" in two, and "todo" (everywhere)
        # is nobody's rendering.
        return support[gram] * (support[gram] / frequency[gram]) ** 0.5 \
            + 0.01 * len(gram.split())

    ranked = sorted((g for g in support
                     if (support[g] >= 2 or len(lines) == 1)
                     and support[g] / frequency[g] >= 0.2),
                    key=score, reverse=True)
    if not ranked:
        return {"rendering": None, "share": 0.0, "variants": [],
                "missing": lines, "example": ""}
    best = ranked[0]
    if len([w for w in best.split() if w not in _EDGE]) < wanted:
        # A two-word term read back as one word ("muelle" for
        # "Lantern Dock") hides how the other word went: the wordings
        # around it are the real renderings ("faro del muelle",
        # "linterna del muelle").
        around: Counter[str] = Counter(
            g for n in lines if says(renders[n], best) and (g := _around(best, renders[n])))
        if len(around) > 1 or (around and next(iter(around)) != best):
            ordered = [g for g, _ in around.most_common()]
            return _summary(term, lines, renders, ordered[0], ordered[1:])
    others = [g for g in ranked[1:6]
              if score(g) >= score(best) * 0.5 and not set(g.split()) <= set(best.split())
              and not set(best.split()) <= set(g.split())]
    return _summary(term, lines, renders, best, others)


def _around(word: str, text: str) -> str | None:
    """`word` with the nearest content word before it ("faro del
    muelle"), else after it; None when it stands alone."""
    words = tokens(text)
    if word not in words:
        return None
    i = words.index(word)
    for lo in range(i - 1, max(-1, i - 4), -1):
        if words[lo] not in _EDGE:
            return " ".join(words[lo:i + 1])
    for hi in range(i + 1, min(len(words), i + 4)):
        if words[hi] not in _EDGE:
            return " ".join(words[i:hi + 1])
    return None


def _summary(term: str, lines: list[int], renders: list[str], best: str,
             others: list[str]) -> dict:
    holding = [n for n in lines if says(renders[n], best)]
    rest = [n for n in lines if n not in holding]
    variants = []
    for other in others:
        used = [n for n in rest if says(renders[n], other)]
        if used:
            variants.append({"rendering": other, "lines": used})
            rest = [n for n in rest if n not in used]
    return {"rendering": best, "share": round(len(holding) / len(lines), 2),
            "variants": variants, "missing": rest,
            "example": _as_written(best, renders[holding[0]]) if holding else ""}


def _as_written(rendering: str, text: str) -> str:
    """The words of `text` that make up `rendering`, as the line wrote them."""
    want = rendering.split()
    words = _TOKEN.findall(text or "")
    keys = [_singular(fold(w)) for w in words]
    for i in range(len(keys)):
        window = keys[i:i + len(want) + 1]
        if keys[i] in want and all(w in window for w in want):
            end = i + max(window.index(w) for w in want) + 1
            return " ".join(words[i:end])
    return rendering


def episode(texts: list[str], renders: list[str], *, decided: dict[str, str] | None = None,
            dub: list[str] | None = None) -> list[dict]:
    """Every key term with how this dub rendered it and, when given, how the
    official dub did; `decided` (source -> rendering) marks the ones settled."""
    decided = {fold(k): v for k, v in (decided or {}).items()}
    out = []
    for term in find(texts):
        row = {**term, "ours": read_back(term["term"], term["lines"], renders, texts)}
        # Kept as it is (a name, "Harbor Guild") or translated (a term).
        row["kind"] = ("name" if row["ours"]["rendering"] == " ".join(_key(term["term"]))
                       else "term")
        if dub is not None:
            ours = row["ours"]["rendering"]
            heard = [n for n in term["lines"] if (dub[n] or "").strip()]
            agree = [n for n in heard if ours and says(dub[n], ours)]
            # Co-occurrence alone cannot tell "Faro" from the "ensenada" always
            # said beside it; where the official dub says our wording in most
            # lines it heard, that is its wording too.
            row["dub"] = (_summary(term["term"], heard, dub, ours, [])
                          if heard and len(agree) * 2 >= len(heard)
                          else read_back(term["term"], term["lines"], dub, texts))
        chosen = decided.get(fold(term["term"]))
        if chosen:
            row["decided"] = chosen
            row["holds"] = [n for n in term["lines"] if says(renders[n] or "", chosen)]
        out.append(row)
    return out


def _source_says(text: str, term: str, proper: bool = False) -> bool:
    """Whether a source line says `term`, ignoring case and plurals; with
    `proper`, only where the line writes it capitalised."""
    want = tokens(term)
    words = _TOKEN.findall(text or "")
    have = [_singular(fold(w)) for w in words]
    for i in range(len(have) - len(want) + 1):
        if have[i:i + len(want)] == want and (
                not proper or all(w[:1].isupper() for w in words[i:i + len(want)])):
            return True
    return False


def to_hold(texts: list[str], renders: list[str], glossary: dict[str, str] | None,
            min_share: float = 0.6) -> list[dict]:
    """The renderings an episode must keep: [{source, rendering, proper}].

    The glossary's come first and hold wherever their source appears. Every
    other key term the episode rendered one way in at least `min_share` of
    its lines (two or more) settles on that way, but only where the source
    writes it capitalised: a word that is a term in "the Lantern Dock" can be
    a plain verb in "docking", and only a person should decide that one.
    A term with no clear majority is left to a person too."""
    # A glossary source written capitalised ("Will") binds capitalised uses
    # only, so the common word ("I will") is left alone; a lowercase one
    # ("harbor") binds every use.
    # A glossary value may carry a register label ({"term", "register"});
    # enforcement matches the plain rendering.
    hold: list[dict] = [{"source": k,
                         "rendering": v["term"] if isinstance(v, dict) else v,
                         "proper": k[:1].isupper()}
            for k, v in (glossary or {}).items()
            if v and any(_source_says(t, k, k[:1].isupper()) for t in texts)]
    held = {fold(row["source"]) for row in hold}
    for term in find(texts):
        if fold(term["term"]) in held:
            continue
        seen = read_back(term["term"], term["lines"], renders, texts)
        if (seen["rendering"] and seen["example"] and seen["share"] >= min_share
                and seen["share"] < 1 and len(term["lines"]) - len(seen["missing"]) >= 2):
            hold.append({"source": term["term"], "rendering": seen["example"], "proper": True})
    return hold


def enforce(job, translator=None, glossary: dict[str, str] | None = None) -> dict:
    """Make every translated line keep the episode's key renderings.

    A line whose source says a term but whose translation lacks its
    rendering is rewritten by the translator when it can (kept only when the
    rewrite says every rendering the line needs); what stays wrong becomes a
    finding on that cue. Returns {checked, held, revised, open} counts.
    """
    from .cues import apply_findings

    texts = [s.text_src or "" for s in job.segments]
    renders = [s.text_translated or "" for s in job.segments]
    hold = to_hold(texts, renders, glossary)
    revise_line = getattr(translator, "revise", None)
    language = job.target_locale or job.target_lang
    counts = {"terms": len(hold), "revised": 0, "open": 0}
    for seg in job.segments:
        needed = {row["source"]: row["rendering"] for row in hold
                  if _source_says(seg.text_src or "", row["source"], row["proper"])}
        text = seg.text_translated or ""
        lacking = {k: v for k, v in needed.items() if not says(text, v)}
        if lacking and revise_line is not None and text.strip():
            # The rewrite sees the source line: told only '"harbor" must be
            # "puerto"', a model cannot tell which Spanish word was the harbor.
            swaps = "; ".join(f'"{k}" is "{v}"' for k, v in needed.items())
            instruction = (
                f'This line translates "{seg.text_src}". The show always says these terms '
                f"one way: {swaps}. Replace the words that render those terms with exactly "
                "that wording (adjusting articles and agreement), and change nothing else.")
            for _attempt in range(2):
                try:
                    better = revise_line(text, language, instruction)
                except Exception:  # noqa: BLE001 - a line that cannot be revised keeps its finding
                    break
                if better and better.strip() and all(says(better, v) for v in needed.values()):
                    from . import dialect

                    if not (dialect.applies(language) and dialect.markers(better)):
                        seg.text_translated = text = better.strip()
                        seg.tts_text = ""
                        counts["revised"] += 1
                        lacking = {}
                        break
        observed = []
        if lacking:
            counts["open"] += 1
            observed.append((CODE, "content", "warning", None, {
                "terms": [{"source": k, "rendering": v} for k, v in lacking.items()],
                "text": text, "note": "a key term is not rendered the way the rest of the "
                                      "episode renders it"}))
        inputs = hashlib.sha1(
            f"{DETECTOR}|{sorted(needed.items())}|{text}".encode()).hexdigest()[:16]
        apply_findings(seg, DETECTOR, inputs, observed)
    job.metrics["key_terms"] = counts
    return counts
