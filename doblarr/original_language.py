"""Which audio track of a video is the original performance.

A library item does not always say what language a title was made in (a show
only Plex has carries no original language), and a release can carry four
audio tracks. The file itself usually tells, without asking any service:

- a track flagged ``original``, or the default track when several languages
  are present;
- titles: "Latino", "Castellano", "Dubbed", "Doblaje", "VF" mark a dub;
  "Original", "VO", "OV" mark the original; a commentary or audio
  description is neither;
- full subtitles in a language next to an audio track in that language:
  a release subtitles a foreign original, so that audio is likely a dub
  ("Signs and songs" or SDH subtitles do not count).

Each audio language gets a score from those clues, and a language wins only
with a clear margin; otherwise the answer is "unknown" with the candidates,
never a guess. A show's answer is cached (doblarr.routes.series) so one probe
serves every episode, and a person can always set it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .ffmpeg import probe_json

DETECTOR = "original-audio/1"
MARGIN = 2.0

_DUB = re.compile(r"\b(dub|dubbed|dubbing|latino|latam|castellano|doblaje|doblado|dublado|"
                  r"synchro|synchron|vf|vff|vfq|fandub|ai)\b", re.I)
_ORIGINAL = re.compile(r"\b(original|vo|ov|native)\b", re.I)
_NEITHER = re.compile(r"\b(commentary|comment|audio description|descriptive|ad)\b", re.I)
_PARTIAL_SUBS = re.compile(r"\b(signs?|songs?|forced|sdh|cc|karaoke|lyrics)\b", re.I)


def probe(path: Path) -> list[dict]:
    """Audio and subtitle streams: {index, kind, lang, title, default, original, forced}."""
    from .discovery import ISO3_TO_ISO2

    found = probe_json(path, "-show_entries",
                       "stream=index,codec_type:stream_tags=language,title:stream_disposition="
                       "default,original,forced,comment,hearing_impaired,visual_impaired")
    streams = []
    for row in found.get("streams") or []:
        kind = row.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        tags = row.get("tags") or {}
        flags = row.get("disposition") or {}
        code = str(tags.get("language") or "und").lower()
        streams.append({"index": int(row["index"]), "kind": kind,
                        "lang": ISO3_TO_ISO2.get(code, code), "title": str(tags.get("title") or ""),
                        "default": bool(flags.get("default")),
                        "original": bool(flags.get("original")),
                        "forced": bool(flags.get("forced")),
                        "described": bool(flags.get("comment") or flags.get("visual_impaired")),
                        "sdh": bool(flags.get("hearing_impaired"))})
    return streams


def detect(streams: list[dict], metadata: str = "") -> dict:
    """The original language and its audio stream, with the clues that decided.

    `metadata` is the title's original language from a library service when
    it knows one: it decides when the file carries that language.
    """
    audio = [s for s in streams if s["kind"] == "audio" and not s.get("described")
             and not _NEITHER.search(s.get("title") or "")]
    tagged = [s for s in audio if s["lang"] not in ("und", "")]
    languages = sorted({s["lang"] for s in tagged})
    if metadata and metadata in languages:
        stream = _best_stream([s for s in tagged if s["lang"] == metadata])
        return {"lang": metadata, "stream": stream["index"], "confidence": "metadata",
                "evidence": [f"the library says the original language is {metadata}"],
                "candidates": languages, "detector": DETECTOR}
    if len(languages) == 1:
        stream = _best_stream(tagged)
        return {"lang": languages[0], "stream": stream["index"], "confidence": "only",
                "evidence": ["the only audio language in the file"], "candidates": languages,
                "detector": DETECTOR}
    if not languages:
        return {"lang": "", "stream": None, "confidence": "none",
                "evidence": ["no audio track says its language"], "candidates": [],
                "detector": DETECTOR}
    full_subs = {s["lang"] for s in streams if s["kind"] == "subtitle" and not s.get("forced")
                 and not s.get("sdh") and not _PARTIAL_SUBS.search(s.get("title") or "")}
    score: dict[str, float] = {lang: 0.0 for lang in languages}
    why: dict[str, list[str]] = {lang: [] for lang in languages}
    def clue(lang: str, points: float, reason: str) -> None:
        score[lang] += points
        why[lang].append(reason)

    for s in tagged:
        title = s.get("title") or ""
        if s.get("original"):
            clue(s["lang"], 4, "flagged as the original track")
        if s.get("default"):
            clue(s["lang"], 2, "the default track")
        if _ORIGINAL.search(title):
            clue(s["lang"], 3, f"titled “{title}”")
        if _DUB.search(title):
            clue(s["lang"], -4, f"titled “{title}”, a dub")
    for lang in languages:
        if lang in full_subs:
            clue(lang, -2, "the file also has full subtitles in it, as releases do for a dub")
    ranked = sorted(languages, key=lambda lang: -score[lang])
    best, runner = ranked[0], ranked[1]
    if score[best] - score[runner] < MARGIN:
        return {"lang": "", "stream": None, "confidence": "none",
                "evidence": [f"{lang}: {', '.join(why[lang]) or 'no clue'}" for lang in ranked],
                "candidates": ranked, "scores": score, "detector": DETECTOR}
    stream = _best_stream([s for s in tagged if s["lang"] == best])
    return {"lang": best, "stream": stream["index"],
            "confidence": "strong" if score[best] - score[runner] >= 2 * MARGIN else "likely",
            "evidence": why[best] + [f"{lang}: {', '.join(why[lang])}" for lang in ranked[1:]
                                     if why[lang]],
            "candidates": ranked, "scores": score, "detector": DETECTOR}


def _best_stream(streams: list[dict]) -> dict:
    """Among tracks of one language: the original-flagged, else default, else first."""
    return sorted(streams, key=lambda s: (not s.get("original"), not s.get("default"),
                                          bool(_DUB.search(s.get("title") or "")), s["index"]))[0]


# ---- a title's answer, kept per show or film ----

def cache_key(media_type: str, tvdb_id=None, tmdb_id=None) -> str:
    if media_type == "show" and tvdb_id:
        return f"original-language:show:tvdb:{tvdb_id}"
    if tmdb_id:
        return f"original-language:movie:tmdb:{tmdb_id}"
    return ""


def remembered(db, key: str) -> dict:
    return ((db.load_plan(key) or {}).get("plan") or {}) if key else {}


def remember(db, key: str, found: dict, *, source: str) -> dict:
    saved = {**found, "source": source}
    db.save_plan(key, "Original language", saved)
    return saved


def for_title(db, key: str, files: list, metadata: str = "") -> dict:
    """The title's original language: a person's choice, else the library's,
    else what the first reachable file shows (probed once, then kept)."""
    saved = remembered(db, key)
    if saved.get("source") == "manual" or (saved.get("lang") and not metadata):
        return saved
    if metadata:
        return {"lang": metadata, "confidence": "metadata", "source": "library",
                "evidence": [f"the library says the original language is {metadata}"]}
    for path in files[:3]:
        if path and Path(path).is_file():
            found = detect(probe(Path(path)))
            found["probed"] = Path(path).name
            if found["lang"] or not saved:
                return remember(db, key, found, source="file") if key else found
    return saved
