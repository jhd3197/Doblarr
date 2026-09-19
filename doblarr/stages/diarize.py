"""Stage 4 — speaker diarization: label who speaks each segment.

Lets Doblarr clone a distinct voice per character. Uses pyannote.audio.
Requires a HuggingFace token (HF_TOKEN) accepting the pyannote model terms.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..models import DubJob, Speaker
from .common import dry, stage

log = logging.getLogger("doblarr.diarize")


@stage("diarize")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False) -> None:
    if not enabled:
        # Single-speaker fallback: everyone is SPEAKER_00.
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        log.info("diarize disabled -> 1 speaker")
        return

    if dry_run:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        return dry(f"would run pyannote diarization on {job.vocals or job.source_audio}")

    if not os.environ.get("HF_TOKEN"):
        raise RuntimeError("diarize needs HF_TOKEN (HuggingFace) in the environment")

    # TODO: implement with pyannote.audio.
    #   from pyannote.audio import Pipeline
    #   pipe = Pipeline.from_pretrained(
    #       "pyannote/speaker-diarization-3.1", use_auth_token=os.environ["HF_TOKEN"])
    #   diarization = pipe(str(job.vocals))
    #   assign each Segment.speaker by overlap, then build job.speakers.
    raise NotImplementedError("diarize: wire up pyannote (see TODO).")
