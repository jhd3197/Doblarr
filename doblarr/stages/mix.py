"""Stage 8 — mix dubbed dialogue over the original music + effects.

Places each clip at its segment start on a silent timeline, sums with the
separated background, and applies sidechain ducking so the score dips under
speech. Produces one finished dub track.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import DubJob

log = logging.getLogger("doblarr.mix")


def run(job: DubJob, work_dir: Path, ducking_ratio: str = "12:1",
        dry_run: bool = False) -> None:
    out = work_dir / f"{job.input_file.stem}.{job.target_lang}.dub.wav"
    job.dubbed_track = out
    log.info("mix dialogue + background (ducking %s) -> %s", ducking_ratio, out.name)
    if dry_run:
        log.info("  [dry-run] would place %d clips on a timeline and duck the background",
                 len(job.segments))
        return
    # TODO: build the dialogue timeline (adelay per clip, amix), then
    #   sidechaincompress the background against the dialogue bus, then amix.
    #   ffmpeg filter_complex with [bg][dialogue]sidechaincompress=ratio=...
    raise NotImplementedError("mix: implement ffmpeg timeline + ducking (see TODO).")
