"""Stage 1 — extract the original audio track from the video (ffmpeg)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..models import DubJob

log = logging.getLogger("doblarr.extract")


def run(job: DubJob, work_dir: Path, dry_run: bool = False) -> None:
    out = work_dir / f"{job.input_file.stem}.source.wav"
    job.source_audio = out
    cmd = [
        "ffmpeg", "-y", "-i", str(job.input_file),
        "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out),
    ]
    log.info("extract audio -> %s", out.name)
    if dry_run:
        log.info("  [dry-run] %s", " ".join(cmd))
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
