"""Stage 1 — extract the original audio track from the video (ffmpeg)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..ffmpeg import run_ffmpeg
from ..models import DubJob
from .common import DryRunPlan, dry, stage

log = logging.getLogger("doblarr.extract")


@stage("extract")
def run(job: DubJob, work_dir: Path, dry_run: bool = False,
        cancel: threading.Event | None = None) -> DryRunPlan | None:
    out = work_dir / f"{job.input_file.stem}.source.wav"
    job.source_audio = out
    args = [
        "-y", "-i", str(job.input_file),
        "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out),
    ]
    log.info("extract audio -> %s", out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, cancel=cancel)
    return None
