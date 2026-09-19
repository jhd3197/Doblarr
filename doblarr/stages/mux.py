"""Stage 9 — mux the finished dub back into the video as a new audio track.

Non-destructive: keeps the original video, all original audio tracks, and
subtitles; just appends the dub, tagged with its language and a friendly title.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..ffmpeg import run_ffmpeg
from ..models import DubJob
from .common import dry, stage

log = logging.getLogger("doblarr.mux")

# ISO-639-2/B codes ffmpeg wants for the language metadata tag.
_LANG3 = {"en": "eng", "es": "spa", "ko": "kor", "ja": "jpn", "fr": "fre",
          "de": "ger", "zh": "chi", "pt": "por", "it": "ita", "ru": "rus"}


@stage("mux")
def run(job: DubJob, output_dir: Path, track_name_template: str = "AI - {language}",
        dry_run: bool = False) -> None:
    out = output_dir / job.input_file.name
    job.output_file = out
    title = track_name_template.format(language=job.target_lang.upper())
    lang3 = _LANG3.get(job.target_lang, job.target_lang)

    args = [
        "-y",
        "-i", str(job.input_file),
        "-i", str(job.dubbed_track) if job.dubbed_track else "MISSING_DUB",
        "-map", "0",            # everything from the original
        "-map", "1:a",          # plus the new dub audio
        "-c", "copy",
        "-c:a:0", "copy",
        "-metadata:s:a:1", f"language={lang3}",
        "-metadata:s:a:1", f"title={title}",
        "-disposition:a:1", "0",
        str(out),
    ]
    log.info("mux new track '%s' (%s) -> %s", title, lang3, out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args)
