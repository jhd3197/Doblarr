"""Stage 2 — separate dialogue (vocals) from music + effects (M&E).

Replacing only the vocals and keeping the original M&E is what makes a dub sound
like a real dub instead of a voiceover. Uses Demucs (htdemucs_ft).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import DubJob

log = logging.getLogger("doblarr.separate")


def run(job: DubJob, work_dir: Path, model: str = "htdemucs_ft",
        dry_run: bool = False) -> None:
    job.vocals = work_dir / f"{job.input_file.stem}.vocals.wav"
    job.background = work_dir / f"{job.input_file.stem}.background.wav"
    log.info("separate (%s) -> vocals + background", model)
    if dry_run:
        log.info("  [dry-run] would run Demucs on %s", job.source_audio)
        return
    # TODO: implement with Demucs.
    #   from demucs.separate import main as demucs_main
    #   demucs_main(["-n", model, "--two-stems", "vocals", str(job.source_audio)])
    #   then map the 'no_vocals' stem -> job.background, 'vocals' -> job.vocals.
    raise NotImplementedError("separate: wire up Demucs (see TODO). Use --dry-run for now.")
