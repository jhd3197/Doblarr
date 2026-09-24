"""Sound a take makes that its words do not account for.

A recognizer compares words, and a recognizer that hears "Puhuhuhu. Sí, con mis
ojos." reports "Sí, con mis ojos." — laughter, humming and invented breaths are
exactly what speech recognition is trained to leave out. So a take that opens
with a giggle nobody asked for passes the content check with a perfect score.

This check asks the other question: how much voiced audio is there, and how
much could the requested words plausibly take? When the answer is "far more",
the take is split at its internal silences and the edge pieces are heard on
their own. A piece that shares no word with the line is sound the engine
invented.

Two things it will not do:

- **Call slow speech an invention.** Pauses are not voiced time, and the
  margin is wide. Only an edge piece that contains none of the asked-for words
  is ever reported, and only with its time and what the recognizer made of it.
- **Guess without a recognizer.** Without one, only an extreme excess is
  reported, and it is reported as `suspicious`, never as confirmed.
"""

from __future__ import annotations

import math
import sys
import tempfile
import wave
from array import array
from difflib import SequenceMatcher
from pathlib import Path

from .ffmpeg import run_ffmpeg
from .verify import base_language, normalize

CHECKER = "extra-sound/1"
FRAME = 0.02
# Below the loudest frame by this much counts as silence.
FLOOR_DB = 30.0
# Silences shorter than this stay inside one piece of speech.
BRIDGE = 0.22
# A voiced piece shorter than this is a click, not a vocalisation.
SHORTEST = 0.12
# Characters per second a normal line is spoken at. Deliberately generous so a
# fast reader never looks short and a slow one never looks padded.
RATE = {"ja": 8.0, "zh": 5.0, "ko": 8.0}
DEFAULT_RATE = 13.0
# Voiced time must exceed the words' estimate by both of these to be looked at.
EXCESS_RATIO = 2.0
EXCESS_SECONDS = 0.7
# Without a recognizer, only this much excess is worth a mention.
UNHEARD_RATIO = 3.2
UNHEARD_SECONDS = 1.5
# Share of a heard piece's letters that must come from the line for the piece
# to count as the line's own words. Recognizers glue and split words freely
# ("comisojos"), so this compares letters, not tokens.
ACCOUNTED = 0.6


def _mono(path: Path) -> tuple[array, int]:
    """16-bit mono samples, converting through FFmpeg only when needed."""
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getsampwidth() == 2:
                rate, channels = audio.getframerate(), audio.getnchannels()
                samples = array("h", audio.readframes(audio.getnframes()))
                if sys.byteorder != "little":
                    samples.byteswap()
                if channels > 1:
                    samples = array("h", (
                        int(sum(samples[i:i + channels]) / channels)
                        for i in range(0, len(samples), channels)))
                return samples, rate
    except (wave.Error, EOFError):
        pass
    with tempfile.TemporaryDirectory() as scratch:
        converted = Path(scratch) / "mono.wav"
        run_ffmpeg(["-y", "-i", str(path), "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(converted)])
        return _mono(converted)


def voiced_regions(path: Path) -> tuple[list[tuple[float, float]], float]:
    """Voiced stretches of a take as (start, end) seconds, and its duration."""
    samples, rate = _mono(Path(path))
    step = max(1, int(rate * FRAME))
    levels = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        power = sum(v * v for v in chunk) / max(1, len(chunk))
        levels.append(10 * math.log10(max(power, 1.0) / (32768 * 32768)))
    duration = len(samples) / rate if rate else 0.0
    if not levels:
        return [], duration
    floor = max(max(levels) - FLOOR_DB, -60.0)
    regions: list[list[float]] = []
    for index, level in enumerate(levels):
        if level < floor:
            continue
        start, end = index * FRAME, (index + 1) * FRAME
        if regions and start - regions[-1][1] <= BRIDGE:
            regions[-1][1] = end
        else:
            regions.append([start, end])
    return [(round(a, 3), round(min(b, duration), 3)) for a, b in regions
            if b - a >= SHORTEST], duration


def expected_seconds(text: str, language: str) -> float:
    """A generous estimate of how long the words take to say."""
    spoken = sum(ch.isalnum() for ch in str(text))
    return max(0.35, spoken / RATE.get(base_language(language), DEFAULT_RATE))


def accounted_for(heard: str, text: str, language: str = "") -> bool:
    """Whether a heard piece is made of the line's own words."""
    piece = normalize(heard, language).replace(" ", "")
    if not piece:
        return False
    line = normalize(text, language).replace(" ", "")
    blocks = SequenceMatcher(None, piece, line, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(piece) >= ACCOUNTED


def _cut(path: Path, start: float, end: float, dest: Path) -> Path:
    run_ffmpeg(["-y", "-ss", f"{max(0.0, start - 0.05):.3f}", "-t",
                f"{end - start + 0.1:.3f}", "-i", str(path), "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(dest)])
    return dest


def check(path: Path, text: str, language: str, vb=None) -> dict:
    """Whether a take holds voiced sound its own words cannot account for.

    Returns `state` — `clean`, `extra` (heard and confirmed), `suspicious`
    (too much sound, nobody to listen) or `unchecked` — plus the measurements
    that explain it, so a reviewer can go straight to the moment.
    """
    regions, duration = voiced_regions(Path(path))
    voiced = round(sum(b - a for a, b in regions), 3)
    expected = round(expected_seconds(text, language), 3)
    unaccounted: list[dict] = []
    result = {"checker": CHECKER, "state": "clean", "voiced": voiced,
              "expected": expected, "duration": round(duration, 3),
              "regions": [list(r) for r in regions], "unaccounted": unaccounted}
    excess = voiced - expected
    if not regions or excess < EXCESS_SECONDS or voiced < expected * EXCESS_RATIO:
        return result
    if vb is None or len(regions) < 2:
        # One unbroken stretch cannot be split into "words" and "not words";
        # the recognizer's own repetition check is the tool for that shape.
        if excess >= UNHEARD_SECONDS and voiced >= expected * UNHEARD_RATIO:
            result["state"] = "suspicious"
        return result
    edges = [regions[0], regions[-1]]
    with tempfile.TemporaryDirectory() as scratch:
        for position, (start, end) in zip(("leading", "trailing"), edges, strict=True):
            try:
                piece = _cut(Path(path), start, end, Path(scratch) / f"{position}.wav")
                heard = str((vb.transcribe(piece, language=language) or {}).get("text") or "")
            except Exception:  # noqa: BLE001 - a failed listen is unchecked, not clean
                result["state"] = "unchecked"
                continue
            if accounted_for(heard, text, language):
                continue
            unaccounted.append({"position": position, "start": start,
                                "end": end, "heard": heard.strip()})
    if unaccounted:
        result["state"] = "extra"
    return result


def _short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def describe(result: dict) -> str:
    """One sentence for review."""
    if result.get("state") == "extra":
        parts = [f"{u['position']} {u['start']:.1f}–{u['end']:.1f}s"
                 + (f" (“{_short(u['heard'])}”)" if u["heard"] else " (no words)")
                 for u in result["unaccounted"]]
        return "sound the line does not account for: " + "; ".join(parts)
    if result.get("state") == "suspicious":
        return (f"{result['voiced']:.1f}s of voiced audio for words that take about "
                f"{result['expected']:.1f}s")
    return ""

