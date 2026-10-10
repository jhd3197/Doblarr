"""What an official dub track says, line by line, for the names and terms.

When a file carries a professional dub in the language being dubbed into
("Latino" beside an es-419 dub), its translators already chose how the show's
terms are said. Its words, transcribed once and laid on the source lines,
let `key_terms.read_back` read those choices off it.

The result is a proposal, never a rule: a dub is adapted to lip movement and
the picture, so it drops or rephrases terms freely, and only a person decides
that its wording becomes the show's. The track must be the same cut on the
same clock (the alignment the voice grouping verified); words are placed by
that alignment, each on the source line nearest to it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .fileio import write_json
from .languages import base_language, parse

log = logging.getLogger("doblarr.dub_text")

READER = "dub-text/1"
# How far (seconds) a word may sit outside a line and still belong to it: a
# dub's line starts and ends near, not exactly on, the original's.
REACH = 1.0

_REGIONS = (
    (re.compile(r"latin|latam|latino|am[eé]rica|m[eé]xic|lat\.?\b", re.I), "419"),
    (re.compile(r"castellano|castilian|espa[ñn]a|spain|\bes-es\b|\bcast\b", re.I), "ES"),
    (re.compile(r"brasil|brazil|\bbr\b", re.I), "BR"),
    (re.compile(r"portugal", re.I), "PT"),
    (re.compile(r"qu[eé]bec|canad|\bvfq\b", re.I), "CA"),
)


def region(title: str) -> str:
    """The region a track title names ("Latino" -> "419"), or ""."""
    for pattern, code in _REGIONS:
        if pattern.search(title or ""):
            return code
    return ""


def pick_track(tracks: list[dict], target_locale: str, original: str = "") -> dict | None:
    """The dub track in the target language, region included when told.

    `tracks` are ``{stream, lang, title}``. A track whose title names
    another region of the language ("Castellano" for es-419) is never
    picked; one that names no region is picked only when it is the only
    track of the language.
    """
    tag = parse(target_locale or "")
    base = base_language(tag or target_locale or "")
    if not base or base == base_language(original or ""):
        return None
    wanted = tag.split("-")[-1] if tag and "-" in tag else ""
    same = [t for t in tracks if base_language(str(t.get("lang") or "")) == base]
    if not wanted:
        return same[0] if len(same) == 1 else None
    named = [t for t in same if region(str(t.get("title") or "")) == wanted]
    if named:
        return named[0]
    plain = [t for t in same if not region(str(t.get("title") or ""))]
    return plain[0] if len(same) == 1 and plain else None


def path_for(folder: Path, stem: str, stream: int) -> Path:
    return Path(folder) / f"{stem}.dubtext.{stream}.json"


def transcribe(wav: Path, language: str, model: str = "large-v3", device=None) -> list[dict]:
    """The words of a track with their times: [{start, end, text}]."""
    from . import whisper

    with whisper.loaded(model, device) as recognizer:
        segments, _info = recognizer.transcribe(str(wav), language=language, vad_filter=True,
                                                word_timestamps=True)
        return [{"start": round(w.start, 2), "end": round(w.end, 2), "text": w.word.strip()}
                for s in segments for w in (s.words or []) if w.word.strip()]


def ensure(folder: Path, stem: str, track: dict, wav: Path, *, offset: float = 0.0,
           rate: float = 1.0, model: str = "large-v3", device=None) -> Path:
    """The track's transcript beside the other audio files, made once."""
    path = path_for(folder, stem, int(track["stream"]))
    if path.is_file():
        return path
    words = transcribe(wav, base_language(str(track.get("lang") or "")) or "", model, device)
    write_json(path, {
        "reader": READER, "stream": int(track["stream"]), "lang": track.get("lang", ""),
        "title": track.get("title", ""), "model": model, "offset": offset, "rate": rate,
        "words": words}, indent=None)
    log.info("dub text: %d words from audio %s (%s)", len(words), track["stream"],
             track.get("title") or track.get("lang"))
    return path


def lines(words: list[dict], spans: list[tuple[float, float]], *, offset: float = 0.0,
          rate: float = 1.0) -> list[str]:
    """The dub's words on each source line: a word goes to the line nearest
    to it on the source clock (source = track * rate + offset), or nowhere
    when every line is more than `REACH` away."""
    out: list[list[str]] = [[] for _ in spans]
    if not spans:
        return []
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    for word in words:
        middle = (float(word["start"]) + float(word["end"])) / 2 * rate + offset
        best, gap = None, REACH
        for i in order:
            start, end = spans[i]
            if start - middle > gap:
                break
            away = 0.0 if start <= middle <= end else min(abs(middle - start), abs(middle - end))
            if away <= gap:
                best, gap = i, away
        if best is not None:
            out[best].append(word["text"])
    return [" ".join(x) for x in out]


def for_script(folder: Path, stem: str, spans: list[tuple[float, float]],
               target_locale: str) -> dict | None:
    """The transcript of the dub in `target_locale` laid on these lines:
    {title, stream, lines}, or None when there is none on disk."""
    tag = parse(target_locale or "")
    base = base_language(tag or target_locale or "")
    wanted = tag.split("-")[-1] if tag and "-" in tag else ""
    for path in sorted(Path(folder).glob(f"{stem}.dubtext.*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if base_language(str(data.get("lang") or "")) != base:
            continue
        named = region(str(data.get("title") or ""))
        if wanted and named and named != wanted:
            continue
        return {"title": data.get("title") or data.get("lang"), "stream": data.get("stream"),
                "lines": lines(data.get("words") or [], spans,
                               offset=float(data.get("offset") or 0.0),
                               rate=float(data.get("rate") or 1.0))}
    return None
