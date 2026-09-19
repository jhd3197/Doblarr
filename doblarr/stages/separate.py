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
    if dry_run:
        log.info("  [dry-run] would run Demucs on %s", job.source_audio)
        return

    # If Demucs is available, isolate dialogue; otherwise fall back to mixing over
    # the original audio (v1 — the score/original speech stays under the dub).
    try:
        import demucs  # noqa: F401
    except ImportError:
        job.background = job.source_audio
        job.vocals = None
        log.warning("Demucs not installed — mixing over the original audio "
                    "(install demucs for clean dialogue separation)")
        return

    # TODO: run Demucs two-stems and set job.vocals / job.background.
    job.background = job.source_audio
    log.info("separate (%s): Demucs present but not yet wired — using original bed", model)
