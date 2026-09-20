"""Stage 4 — speaker diarization: label who speaks each segment.

Lets Doblarr clone a distinct voice per character. Uses pyannote.audio
(requires HF_TOKEN, and accepting the pyannote model terms on HuggingFace).
When diarization is disabled, pyannote isn't installed, or no token is
available, we degrade gracefully to a single narrator voice so teases and
full dubs still run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..models import DubJob, Speaker
from .common import DryRunPlan, dry, stage

log = logging.getLogger("doblarr.diarize")

MODEL_ID = "pyannote/speaker-diarization-3.1"


def _single_narrator(job: DubJob, reason: str) -> None:
    job.speakers = {"NARRATOR": Speaker(label="NARRATOR")}
    for seg in job.segments:
        seg.speaker = "NARRATOR"
    log.warning("diarize: %s — falling back to a single narrator voice", reason)


def _load_pipeline(token: str):
    """Load the pyannote diarization pipeline, on GPU when one is available."""
    from pyannote.audio import Pipeline

    from_pretrained: Any = Pipeline.from_pretrained
    try:
        pipe = from_pretrained(MODEL_ID, use_auth_token=token)
    except TypeError:  # pyannote.audio >= 4 renamed the kwarg
        pipe = from_pretrained(MODEL_ID, token=token)

    import torch

    if torch.cuda.is_available():
        pipe.to(torch.device("cuda"))
        log.info("diarize: using CUDA")
    return pipe


def _assign_speakers(job: DubJob, diarization) -> None:
    """Give each segment the speaker whose turn overlaps it most, then rebuild
    job.speakers from the labels actually used."""
    turns = [(turn.start, turn.end, label)
             for turn, _, label in diarization.itertracks(yield_label=True)]
    for seg in job.segments:
        best_label, best_overlap = seg.speaker, 0.0
        for start, end, label in turns:
            overlap = min(seg.end, end) - max(seg.start, start)
            if overlap > best_overlap:
                best_label, best_overlap = label, overlap
        seg.speaker = best_label  # no overlap: keep the default SPEAKER_00
    used = sorted({seg.speaker for seg in job.segments}) or ["SPEAKER_00"]
    job.speakers = {label: Speaker(label=label) for label in used}


@stage("diarize")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False) -> DryRunPlan | None:
    if not enabled:
        # Single-speaker fallback: everyone is SPEAKER_00.
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        for seg in job.segments:
            seg.speaker = "SPEAKER_00"
        log.info("diarize disabled -> 1 speaker")
        return None

    if dry_run:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
        return dry(f"would run pyannote diarization on {job.vocals or job.source_audio}")

    if job.speakers:
        log.info("diarize: speakers already assigned (restored script) — skipping")
        return None

    token = os.environ.get("HF_TOKEN")
    if not token:
        _single_narrator(job, "no HF_TOKEN (HuggingFace) in the environment")
        return None
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        _single_narrator(job, "pyannote.audio not installed")
        return None

    audio = job.vocals or job.source_audio
    if audio is None:
        raise RuntimeError(
            "diarize needs vocals or source audio (extract/separate must run first)")

    try:
        pipe = _load_pipeline(token)
    except Exception as exc:  # noqa: BLE001 — gated model, bad token, download failure
        _single_narrator(job, f"could not load {MODEL_ID} ({exc}); accept the model terms at "
                              f"huggingface.co/{MODEL_ID} and check HF_TOKEN")
        return None

    log.info("diarizing %s with %s", audio.name, MODEL_ID)
    diarization = pipe(str(audio))
    _assign_speakers(job, diarization)
    log.info("diarize -> %d speakers (%s)", len(job.speakers), ", ".join(job.speakers))
    return None
