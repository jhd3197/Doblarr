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
from ..discovery import lang_name
from ..ffmpeg import run_ffmpeg, run_ffprobe
from ..models import DubJob
from .common import Plan, cached, dry, stage

log = logging.getLogger("doblarr.mux")

# ISO-639-2/B codes ffmpeg wants for the language metadata tag.
_LANG3 = {"en": "eng", "es": "spa", "ko": "kor", "ja": "jpn", "fr": "fre",
          "de": "ger", "zh": "chi", "pt": "por", "it": "ita", "ru": "rus"}


@stage("mux")
def run(job: DubJob, output_dir: Path, track_name_template: str = "{language_name} AI",
        dry_run: bool = False, cancel: threading.Event | None = None,
        force: bool = False, duration: int | None = None) -> Plan | None:
    if job.kind == "tease":
        # A tease is a standalone clip, not a track added to the full video.
        out = output_dir / f"{job.input_file.stem}.tease{job.input_file.suffix}"
    else:
        out = output_dir / job.input_file.name
    job.output_file = out
    if out.resolve() == job.input_file.resolve():
        raise ValueError("output must not overwrite the original video")
    # e.g. "English AI", teases "Spanish AI (tease)"; {language} stays the code.
    title = track_name_template.format(language=job.target_lang.upper(),
                                       language_name=lang_name(job.target_lang))
    if job.kind == "tease":
        title += " (tease)"
    lang3 = _LANG3.get(job.target_lang, job.target_lang)
    request = {"input": stamp(job.input_file), "dub": stamp(job.dubbed_track),
               "title": title, "language": lang3, "duration": duration}

    if not dry_run:
        hit = cached(out, job.input_file, force)
        if hit and matches([out], request, force):
            return hit
        probe = json.loads(run_ffprobe([
            "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
            "-of", "json", str(job.input_file)], cancel=cancel))
        audio_index = len(probe.get("streams", []))
    else:
        audio_index = 1
    temp = out.with_name(out.stem + ".partial" + out.suffix)

    args = [
        "-y",
        "-i", str(job.input_file),
        "-i", str(job.dubbed_track) if job.dubbed_track else "MISSING_DUB",
    ]
    if duration:
        args += ["-t", str(duration)]  # tease: cut the output at the teaser length
    args += [
        "-map", "0",            # everything from the original
        "-map", "1:a",          # plus the new dub audio
        "-c", "copy",
        "-c:a:0", "copy",
        f"-metadata:s:a:{audio_index}", f"language={lang3}",
        f"-metadata:s:a:{audio_index}", f"title={title}",
        f"-disposition:a:{audio_index}", "0",
        str(temp),
    ]
    log.info("mux new track '%s' (%s) -> %s", title, lang3, out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, cancel=cancel)
    temp.replace(out)
    record([out], request)
    return None
