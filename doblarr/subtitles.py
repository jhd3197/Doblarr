"""Read subtitle tracks embedded in a video (ffprobe/ffmpeg).

Doblarr uses embedded subs as the timed script when no external .srt is given:
the target-language track (e.g. English) is already the translation, and its
timestamps drive where each dubbed line lands.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("doblarr.subtitles")


def sub_streams(video: Path) -> list[dict]:
    """List embedded text subtitle streams as {index, lang, codec}."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index,codec_name:stream_tags=language",
         "-of", "json", str(video)],
        capture_output=True, text=True)
    data = json.loads(out.stdout or "{}")
    streams = []
    for s in data.get("streams", []):
        streams.append({
            "index": s["index"],
            "codec": s.get("codec_name", ""),
            "lang": (s.get("tags") or {}).get("language", "und"),
        })
    return streams


# Text subtitle codecs we can convert to SRT (image subs like PGS can't be).
_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt"}


def pick_stream(streams: list[dict], prefer_lang: str) -> dict | None:
    text = [s for s in streams if s["codec"] in _TEXT_CODECS]
    if not text:
        return None
    p = prefer_lang.strip().lower()[:2]
    # match ISO-639-1 prefix against the (usually 639-2) tag
    for s in text:
        if s["lang"].lower().startswith(p):
            return s
    return None


def extract_srt(video: Path, stream_index: int, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-map", f"0:{stream_index}", str(dest)],
        check=True, capture_output=True)
    return dest
