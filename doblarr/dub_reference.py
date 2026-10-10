"""What a published dub of this episode says, line by line.

A file that carries a professional dub in the target language is the best
evidence there is of how that audience hears the show: the names, the invented terms,
the catchphrases, and how long each line runs when a person performs it. This
transcribes that track (faster-whisper, word timestamps) and gives each script
line the words spoken during it, mapped through the track's verified alignment
(see doblarr.track_alignment).

It is research data, never applied by itself: the result is a sidecar next to
the script, `<stem>.dubref.<stream>.json`, which review tools and reports read.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .fileio import write_json

log = logging.getLogger("doblarr.dub_reference")

VERSION = "dubref/1"
# Words a little outside a cue still belong to it: dubs drift a few hundred
# milliseconds from the original's timing.
PAD = 0.35


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if unicodedata.category(c) != "Mn")


def transcribe(audio: Path, language: str, model: str = "large-v3",
               device: str = "cuda") -> list[dict]:
    """Every recognised word of a track as {"start", "end", "word"}."""
    from faster_whisper import WhisperModel

    try:
        whisper = WhisperModel(model, device=device,
                               compute_type="float16" if device == "cuda" else "int8")
    except Exception as exc:  # noqa: BLE001 - no GPU runtime: fall back to CPU
        log.warning("dub reference: %s on %s failed (%s); using cpu", model, device, exc)
        whisper = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _info = whisper.transcribe(str(audio), language=language, word_timestamps=True,
                                         vad_filter=True, beam_size=5,
                                         condition_on_previous_text=False)
    words = []
    for segment in segments:
        for word in segment.words or []:
            words.append({"start": round(word.start, 3), "end": round(word.end, 3),
                          "word": word.word.strip()})
    return words


def assign(words: list[dict], lines: list[dict], offset: float = 0.0,
           rate: float = 1.0) -> list[dict]:
    """Give each line the words spoken during it on the dub's timeline.

    `lines` carry "index", "start", "end" on the source timeline. A word goes to
    the line whose (padded) span holds its midpoint, nearest centre on a tie.
    """
    spans = [((ln["start"] - offset) / rate, (ln["end"] - offset) / rate) for ln in lines]
    bucket: dict[int, list[dict]] = {i: [] for i in range(len(lines))}
    for word in words:
        mid = (word["start"] + word["end"]) / 2
        best = None
        for i, (start, end) in enumerate(spans):
            if start - PAD <= mid <= end + PAD:
                distance = abs(mid - (start + end) / 2)
                if best is None or distance < best[0]:
                    best = (distance, i)
        if best is not None:
            bucket[best[1]].append(word)
    out = []
    for i, line in enumerate(lines):
        heard = bucket[i]
        out.append({
            "index": line["index"],
            "text": " ".join(w["word"] for w in heard),
            "seconds": round(heard[-1]["end"] - heard[0]["start"], 3) if heard else None,
        })
    return out


def term_usage(lines: list[dict], terms: list[str]) -> dict[str, int]:
    """How often each term (folded) is said across the dub's lines."""
    said = " ".join(_fold(line["text"]) for line in lines)
    return {term: len(re.findall(r"\b" + re.escape(_fold(term)) + r"\b", said))
            for term in terms if term.strip()}


def frequent_names(lines: list[dict], minimum: int = 2) -> list[tuple[str, int]]:
    """Capitalised words the dub says repeatedly, mid-sentence: names and terms."""
    counts: Counter[str] = Counter()
    for line in lines:
        words = line["text"].split()
        for position, word in enumerate(words):
            clean = word.strip("¿¡!?.,;:…\"'()-")
            if (position > 0 and clean[:1].isupper() and len(clean) > 2
                    and not words[position - 1].endswith((".", "!", "?", "…"))):
                counts[clean] += 1
    return [(w, n) for w, n in counts.most_common() if n >= minimum]


def sidecar(script: Path, stream: int) -> Path:
    return script.with_name(script.name.replace(".script.json", f".dubref.{stream}.json"))


def build(script: Path, audio: Path, stream: int, language: str, offset: float = 0.0,
          rate: float = 1.0, model: str = "large-v3", device: str = "cuda",
          words: list[dict] | None = None) -> dict:
    data = json.loads(script.read_text(encoding="utf-8"))
    lines = [{"index": s["index"], "start": s["start"], "end": s["end"]}
             for s in data.get("segments") or []]
    if words is None:
        words = transcribe(audio, language, model, device)
    result = {"version": VERSION, "stream": stream, "language": language,
              "offset": offset, "rate": rate, "model": model, "audio": str(audio),
              "words": words, "lines": assign(words, lines, offset, rate)}
    out = sidecar(script, stream)
    write_json(out, result, indent=1)
    return result


# A pause this long in the dub ends a phrase even without punctuation.
PHRASE_GAP = 0.6


def phrases(words: list[dict]) -> list[dict]:
    """The dub's words grouped into phrases: split at sentence ends and pauses."""
    out: list[dict] = []
    for word in words:
        current = out[-1] if out else None
        if (current is None or word["start"] - current["end"] > PHRASE_GAP
                or current["text"].endswith((".", "!", "?", "…"))):
            out.append({"start": word["start"], "end": word["end"], "text": word["word"]})
        else:
            current["end"] = word["end"]
            current["text"] = f"{current['text']} {word['word']}"
    return out


def reference(ref: dict) -> dict:
    """The dub as an aligned adaptation reference (see stages.translate).

    Phrases are moved onto the source timeline through the track's measured
    offset and rate, so a script line picks up the phrase spoken over it. Only
    phrases with real words are kept; the track alignment was verified before
    any of this ran, so each kept phrase is `matched`.
    """
    offset, rate = float(ref.get("offset") or 0.0), float(ref.get("rate") or 1.0)
    groups = []
    for phrase in phrases(ref.get("words") or []):
        if len(phrase["text"].split()) < 1:
            continue
        groups.append({"start": round(phrase["start"] * rate + offset - PAD, 3),
                       "end": round(phrase["end"] * rate + offset + PAD, 3),
                       "text": phrase["text"], "state": "matched", "confidence": None})
    return {"language": ref.get("language", ""), "source": "published dub track "
            f"{ref.get('stream')}", "version": VERSION, "groups": groups}


def reference_file(script: Path, stream: int) -> Path:
    return script.with_name(script.name.replace(".script.json",
                                                f".dubref.{stream}.reference.json"))


MODES = {"follow": "follow_edition", "suggest": "reference_suggestions"}


def published_track(evidence: list[dict], language: str) -> dict | None:
    """The verified dub track in `language`, a Latin-American one first."""
    tracks = [t for t in evidence or []
              if t.get("lang") == language and t.get("state") == "verified"]
    tracks.sort(key=lambda t: "latin" not in str(t.get("title") or "").casefold())
    return tracks[0] if tracks else None


def ensure(folder: Path, stem: str, track: dict, lines: list[dict], language: str,
           model: str = "large-v3", device: str = "cuda") -> tuple[dict, Path] | None:
    """The dub's reference for an episode, transcribed once and then reused.

    The transcript is cached in `<stem>.dubref.<stream>.json` next to the
    extracted track and keyed by that file's size and time, so a re-extracted
    track is heard again and an unchanged one never is.
    """
    from .artifacts import stamp

    stream = int(track["stream"])
    audio = folder / f"{stem}.audio{stream}.16k.wav"
    if not audio.is_file():
        return None
    cache = folder / f"{stem}.dubref.{stream}.json"
    saved = {}
    if cache.is_file():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
    key = {"audio": stamp(audio), "model": model, "version": VERSION}
    if saved.get("key") == key:
        words = saved["words"]
    else:
        log.info("dub reference: transcribing %s track %d", language, stream)
        words = transcribe(audio, language, model, device)
        write_json(cache, {"key": key, "words": words}, indent=None)
    offset, rate = float(track.get("offset") or 0.0), float(track.get("rate") or 1.0)
    ref = {"language": language, "stream": stream, "offset": offset, "rate": rate,
           "words": words, "lines": assign(words, lines, offset, rate)}
    payload = reference(ref)
    target = folder / f"{stem}.dubref.{stream}.reference.json"
    write_json(target, payload, indent=1)
    return ref, target
