"""Stage 4 — speaker diarization: label who speaks each segment.

Lets Doblarr clone a distinct voice per character. Uses pyannote.audio
(requires HF_TOKEN accepting the pyannote model terms). Until pyannote is wired
(or when it's unavailable), we degrade gracefully to a single NARRATOR speaker
so teases and full dubs still run — pyannote slots in at the TODO below.
"""

from __future__ import annotations

import logging
import os

from ..models import DubJob, Speaker
from .common import DryRunPlan, dry, stage

log = logging.getLogger("doblarr.diarize")


def _single_narrator(job: DubJob, reason: str) -> None:
    job.speakers = {"NARRATOR": Speaker(label="NARRATOR")}
    log.warning("diarize: %s — falling back to a single narrator voice", reason)


@stage("diarize")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False) -> DryRunPlan | None:
    if not enabled:
        # Single-speaker fallback: everyone is SPEAKER_00.
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        log.info("diarize disabled -> 1 speaker")
        return None

    if dry_run:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        return dry(f"would run pyannote diarization on {job.vocals or job.source_audio}")

    if not os.environ.get("HF_TOKEN"):
        _single_narrator(job, "no HF_TOKEN (HuggingFace) in the environment")
        return None
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        _single_narrator(job, "pyannote.audio not installed")
        return None

    # TODO: implement with pyannote.audio.
    #   from pyannote.audio import Pipeline
    #   pipe = Pipeline.from_pretrained(
    #       "pyannote/speaker-diarization-3.1", use_auth_token=os.environ["HF_TOKEN"])
    #   diarization = pipe(str(job.vocals))
    #   assign each Segment.speaker by overlap, then build job.speakers.
    raise NotImplementedError("diarize: wire up pyannote (see TODO).")
