"""A finished dub, ready to watch in a browser with every language one click away.

A dub's output is the original file with its tracks kept and the new one
added: often 10-bit HEVC with five audio tracks, which most browsers cannot
play and none can switch between. The watch copy is made once per output:

- the picture as H.264 (on the GPU when NVENC is there), with no audio;
- one MP4 per audio track: that picture plus the track, a stream copy that
  takes seconds, made the first time the language is picked;
- each text subtitle track as WebVTT;
- a loudness comparison of the dub against the original-language track:
  where ours is clearly quieter than the original (a laugh nobody voiced, a
  sound lost with the dialogue), so a person knows where to listen.

Everything lives under the work directory's cache and is keyed by the output
file's path and modification time: a remade dub gets a fresh copy.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path

from .ffmpeg import FFmpegError, probe_json, run_ffmpeg, run_ffmpeg_bytes
from .fileio import write_json

log = logging.getLogger("doblarr.watch")

READER = "watch/1"
HEIGHT = 1080
WINDOW = 0.25           # seconds per loudness value
QUIETER_DB = 9.0        # ours this much below the original is worth a listen
MIN_SPAN = 0.5          # seconds
# A browser copy of a long film takes a while; a run with no result after this
# is stuck (a stalled share), and the next request retries it.
TRANSCODE_TIMEOUT = 3 * 3600.0

_lock = threading.Lock()
_building: dict[str, threading.Thread] = {}
_failed: dict[str, str] = {}


def folder(cache: Path, output: Path) -> Path:
    stat = output.stat()
    key = hashlib.sha1(f"{output}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:16]
    return Path(cache) / "watch" / key


def streams(output: Path) -> dict:
    """The output's audio and text subtitle tracks: {audio: [...], subtitles: [...]}."""
    from .discovery import ISO3_TO_ISO2

    found = probe_json(output, "-show_entries",
                       "stream=index,codec_type,codec_name:stream_tags=language,title:"
                       "disposition=default")
    audio, subs = [], []
    for row in found.get("streams") or []:
        tags = row.get("tags") or {}
        code = str(tags.get("language") or "und").lower()
        item = {"stream": int(row["index"]), "lang": ISO3_TO_ISO2.get(code, code),
                "title": str(tags.get("title") or ""),
                "default": bool((row.get("disposition") or {}).get("default"))}
        if row.get("codec_type") == "audio":
            audio.append({**item, "codec": row.get("codec_name")})
        elif row.get("codec_type") == "subtitle" and row.get("codec_name") in (
                "ass", "ssa", "subrip", "srt", "webvtt", "mov_text"):
            subs.append(item)
    return {"audio": audio, "subtitles": subs}


def _nvenc() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                             text=True, check=False, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "h264_nvenc" in out


def _video(output: Path, dest: Path) -> None:
    temp = dest.with_name(dest.stem + ".part.mp4")
    scale = f"scale=-2:'min({HEIGHT},ih)',format=yuv420p"
    encoders = ([["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]] if _nvenc() else []) \
        + [["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]]
    error = ""
    for codec in encoders:
        try:
            run_ffmpeg(["-v", "error", "-y", "-i", str(output), "-map", "0:v:0", "-an", "-sn",
                        "-vf", scale, *codec, "-movflags", "+faststart", str(temp)],
                       timeout=TRANSCODE_TIMEOUT)
            os.replace(temp, dest)
            return
        except FFmpegError as exc:
            error = str(exc)
        finally:
            temp.unlink(missing_ok=True)
    raise RuntimeError(f"could not make the watch copy of the picture: {error}")


def _envelope(output: Path, stream: int) -> list[float]:
    import numpy as np

    raw = run_ffmpeg_bytes(["-v", "error", "-i", str(output), "-map", f"0:{stream}", "-ac", "1",
                            "-ar", "16000", "-f", "s16le", "-"], timeout=TRANSCODE_TIMEOUT)
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    size = int(16000 * WINDOW)
    count = len(x) // size
    if not count:
        return []
    rms = np.sqrt((x[:count * size].reshape(count, size) ** 2).mean(1))
    return [round(float(v), 1) for v in 20 * np.log10(rms + 1e-6)]


def quieter(original: list[float], ours: list[float]) -> dict:
    """Where the dub is clearly quieter than the original: {offset_db, spans}.

    The whole-track difference is taken out first (a dub mixed a little
    lower overall is not a lost sound); what is left and lasts long enough is
    a span to listen to."""
    import numpy as np

    n = min(len(original), len(ours))
    if not n:
        return {"offset_db": 0.0, "spans": []}
    a, b = np.asarray(original[:n]), np.asarray(ours[:n])
    both = (a > -45) & (b > -60)
    offset = float(np.median((a - b)[both])) if both.any() else 0.0
    drop = (a - (b + offset) > QUIETER_DB) & (a > -38)
    spans, i = [], 0
    while i < n:
        if not drop[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (drop[j + 1] or (j + 2 < n and drop[j + 2])):
            j += 1
        if (j - i + 1) * WINDOW >= MIN_SPAN:
            gap = float(np.median(a[i:j + 1] - b[i:j + 1] - offset))
            spans.append({"start": round(i * WINDOW, 2), "end": round((j + 1) * WINDOW, 2),
                          "quieter_db": round(gap, 1)})
        i = j + 1
    return {"offset_db": round(offset, 1), "spans": spans}


def _prepare(output: Path, where: Path, original: int | None, ours: int | None) -> None:
    where.mkdir(parents=True, exist_ok=True)
    video = where / "video.mp4"
    if not video.is_file():
        _video(output, video)
    loud = where / "loudness.json"
    if original is not None and ours is not None and not loud.is_file():
        first, second = _envelope(output, original), _envelope(output, ours)
        found = quieter(first, second)
        # Atomic: a half-written file would read as ready for good.
        write_json(loud, {"reader": READER, "window": WINDOW, "original": original,
                          "ours": ours, **found, "curves": {"original": first, "ours": second}},
                   indent=None)


def ensure(output: Path, where: Path, original: int | None, ours: int | None) -> str:
    """Start (once) making the watch copy; "ready", "preparing" or "failed: …"."""
    key = str(where)
    if (where / "video.mp4").is_file() and (
            (where / "loudness.json").is_file() or original is None or ours is None):
        return "ready"
    with _lock:
        if key in _failed:
            return "failed: " + _failed[key]
        thread = _building.get(key)
        if thread is None or not thread.is_alive():
            def work():
                try:
                    _prepare(output, where, original, ours)
                except Exception as exc:  # noqa: BLE001 - reported to the page, retried on demand
                    log.warning("watch: %s", exc)
                    with _lock:
                        _failed[key] = str(exc)
            thread = threading.Thread(target=work, name="watch-copy", daemon=True)
            _building[key] = thread
            thread.start()
    return "preparing"


def retry(where: Path) -> None:
    with _lock:
        _failed.pop(str(where), None)


def language(output: Path, where: Path, stream: int, codec: str | None) -> Path:
    """The picture with one audio track, made the first time it is asked for."""
    dest = where / f"audio-{stream}.mp4"
    if dest.is_file():
        return dest
    temp = dest.with_name(dest.stem + f".{threading.get_ident()}.part.mp4")
    audio = ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    try:
        run_ffmpeg(["-v", "error", "-y", "-i", str(where / "video.mp4"), "-i", str(output),
                    "-map", "0:v:0", "-map", f"1:{stream}", "-c:v", "copy", *audio, "-ac", "2",
                    "-shortest", "-movflags", "+faststart", str(temp)],
                   timeout=TRANSCODE_TIMEOUT)
        os.replace(temp, dest)
    except FFmpegError as exc:
        raise RuntimeError(f"could not add the audio track: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return dest


def subtitles(output: Path, where: Path, stream: int) -> Path:
    dest = where / f"subs-{stream}.vtt"
    if not dest.is_file():
        where.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.stem + f".{threading.get_ident()}.part.vtt")
        try:
            # ffmpeg creates the file before it fails; only a clean exit is kept.
            run_ffmpeg(["-v", "error", "-y", "-i", str(output), "-map", f"0:{stream}",
                        "-f", "webvtt", str(temp)], timeout=TRANSCODE_TIMEOUT)
            os.replace(temp, dest)
        except FFmpegError as exc:
            raise RuntimeError(f"could not read the subtitle track: {exc}") from exc
        finally:
            temp.unlink(missing_ok=True)
    return dest
