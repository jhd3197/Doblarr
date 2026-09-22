"""Stage 9 — mux the finished dub back into the video as a new audio track.

Non-destructive: keeps the original video, all original audio tracks, and
subtitles; just appends the dub, tagged with its language and a friendly title.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..artifacts import matches, record, stamp
from ..ffmpeg import FFmpegError, run_ffmpeg, run_ffprobe
from ..languages import catalog, display_name, iso3_for
from ..models import DubJob
from .common import Plan, cached, dry, stage

log = logging.getLogger("doblarr.mux")

# ISO-639-2/B codes ffmpeg wants for the language metadata tag.
_LANG3 = {e.id: e.iso3 for e in catalog() if e.id == e.base and e.iso3}


@stage("mux")
def run(
    job: DubJob,
    output_dir: Path,
    track_name_template: str = "{language_name} AI",
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    force: bool = False,
    duration: int | None = None,
    audio_codec: str = "copy",
    bitrate: str = "192k",
) -> Plan | None:
    if job.kind == "audition":
        out = output_dir / f"{job.input_file.stem}.audition.wav"
        job.output_file = out
        if dry_run:
            return dry("would export a short audio audition montage")
        request: dict = {"dub": stamp(job.dubbed_track), "kind": "audition"}
        if matches([out], request, force):
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(".partial.wav")
        run_ffmpeg(["-y", "-i", str(job.dubbed_track), "-c:a", "pcm_s16le", str(temp)], cancel)
        temp.replace(out)
        record([out], request)
        return None
    if audio_codec not in {"copy", "aac", "flac"}:
        raise ValueError("dub audio codec must be copy, aac or flac")
    if job.kind == "tease":
        # A tease is a standalone clip, not a track added to the full video.
        out = output_dir / f"{job.input_file.stem}.tease{job.input_file.suffix}"
    else:
        out = output_dir / job.input_file.name
    job.output_file = out
    if out.resolve() == job.input_file.resolve():
        raise ValueError("output must not overwrite the original video")
    # e.g. "English AI", teases "Spanish AI (tease)"; {language} stays the code.
    locale = job.target_locale or job.target_lang
    title = track_name_template.format(
        language=job.target_lang.upper(), language_name=display_name(locale)
    )
    if job.kind == "tease":
        title += " (tease)"
    lang3 = iso3_for(locale)
    request = {
        "input": stamp(job.input_file),
        "dub": stamp(job.dubbed_track),
        "title": title,
        "language": lang3,
        "duration": duration,
        "codec": audio_codec,
        "bitrate": bitrate,
    }

    if not dry_run:
        hit = cached(out, job.input_file, force)
        original = _original_streams(job.input_file, cancel)
        audio_index = original.get("audio", 0)
        # Which stream the dub *is*, recorded before it is written. Export
        # validation identifies the added track by this rather than measuring
        # whichever audio stream happens to come first, and the original
        # stream counts are what proves nothing was dropped to make room.
        job.metrics["mux"] = {"audio_index": audio_index, "language": lang3,
                              "title": title, "codec": audio_codec,
                              "bitrate": bitrate if audio_codec == "aac" else None,
                              "original_streams": original}
        if hit and matches([out], request, force):
            return hit
    else:
        audio_index = 1
    temp = out.with_name(out.stem + ".partial" + out.suffix)

    args = [
        "-y",
        "-i",
        str(job.input_file),
        "-i",
        str(job.dubbed_track) if job.dubbed_track else "MISSING_DUB",
    ]
    if duration:
        args += ["-t", str(duration)]  # tease: cut the output at the teaser length
    args += [
        "-map",
        "0",  # everything from the original
        "-map",
        "-0:t?",  # put attachments after all timed streams, including the new dub
        "-map",
        "1:a",  # plus the new dub audio
        "-map",
        "0:t?",
        "-c",
        "copy",
        f"-c:a:{audio_index}",
        audio_codec,
        f"-metadata:s:a:{audio_index}",
        f"language={lang3}",
        f"-metadata:s:a:{audio_index}",
        f"title={title}",
        f"-disposition:a:{audio_index}",
        "0",
        str(temp),
    ]
    if audio_codec == "aac":
        args[-1:-1] = [f"-b:a:{audio_index}", bitrate]
    log.info("mux new track '%s' (%s) -> %s", title, lang3, out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, cancel=cancel)
    temp.replace(out)
    record([out], request)
    return None


def _original_streams(path: Path, cancel=None) -> dict:
    """How many streams of each kind the source already has.

    Counted before muxing so the export check can assert the dub was *added*
    rather than swapped in: a container that comes out with one fewer subtitle
    track than it went in with has lost something nobody asked it to drop.
    """
    counts: dict = {}
    try:
        probe = json.loads(run_ffprobe(
            ["-v", "error", "-show_entries", "stream=index,codec_type",
             "-of", "json", str(path)], cancel=cancel))
    except (FFmpegError, ValueError):
        return counts
    for stream in probe.get("streams", []):
        kind = stream.get("codec_type") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts
