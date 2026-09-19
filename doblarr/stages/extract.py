"""Stage 1 — extract the original audio track from the video (ffmpeg)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..ffmpeg import run_ffmpeg
from ..models import DubJob
from .common import Plan, cached, dry, stage, work_stem

log = logging.getLogger("doblarr.extract")


@stage("extract")
def run(job: DubJob, work_dir: Path, dry_run: bool = False,
        cancel: threading.Event | None = None, force: bool = False,
        duration: int | None = None) -> Plan | None:
    out = work_dir / f"{work_stem(job)}.source.wav"
    job.source_audio = out
    args = ["-y", "-i", str(job.input_file)]
    if duration:
        args += ["-t", str(duration)]  # tease: only the first `duration` seconds
    args += ["-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)]
    log.info("extract audio -> %s", out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    hit = cached(out, job.input_file, force)
    if hit:
        return hit
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, cancel=cancel)
    return None
