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

from ..cues import SOURCE, Span, split_cue
from ..model_pool import model as pooled_model
from ..models import DubJob, Segment, Speaker
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
    turns = [
        (turn.start, turn.end, label) for turn, _, label in diarization.itertracks(yield_label=True)
    ]

    def speaker_at(start, end, fallback):
        label, overlap = fallback, 0.0
        for left, right, candidate in turns:
            duration = min(end, right) - max(start, left)
            if duration > overlap:
                label, overlap = candidate, duration
        return label

    split = []
    for seg in job.segments:
        best_label, best_overlap = seg.speaker, 0.0
        for start, end, label in turns:
            overlap = min(seg.end, end) - max(seg.start, start)
            if overlap > best_overlap:
                best_label, best_overlap = label, overlap
        seg.speaker = best_label  # no overlap: keep the default SPEAKER_00
        if best_overlap <= 0:
            seg.issues.append("speaker_uncertain")
        if (
            seg.words
            and not seg.text_translated
            and all(w.get("start") is not None and w.get("end") is not None for w in seg.words)
        ):
            groups: list[tuple[str, list[dict]]] = []
            for word in seg.words:
                label = speaker_at(word["start"], word["end"], best_label)
                if not groups or groups[-1][0] != label:
                    groups.append((label, []))
                groups[-1][1].append(word)
            if len(groups) > 1:
                # A real split: children get fresh IDs with the parent recorded
                # as lineage, and the parent is retired so a stale edit naming
                # it raises a conflict instead of landing on one arbitrary half.
                children = [
                    Segment(
                        0,
                        words[0]["start"],
                        words[-1]["end"],
                        " ".join(w["word"].strip() for w in words),
                        speaker=label,
                        words=words,
                    )
                    for label, words in groups
                ]
                for child in children:
                    child.source.spans = [Span(child.start, child.end, SOURCE)]
                    child.source.speaker = child.speaker
                    child.source.method = seg.source.method
                    child.source.word_domain = SOURCE
                    child.source.word_method = seg.source.word_method
                split_cue(job, seg, children)
                split.extend(children)
                continue
        split.append(seg)
    if len(split) != len(job.segments):
        for i, segment in enumerate(split):
            segment.index = i
        job.segments = split
    used = sorted({seg.speaker for seg in job.segments}) or ["SPEAKER_00"]
    job.speakers = {label: Speaker(label=label) for label in used}


@stage("diarize")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False) -> DryRunPlan | None:
    if not enabled:
        if job.speakers:
            # Turning diarization off means "do not run the model", not "throw
            # away the cast". A restored script, an imported run or a saved
            # review already knows who speaks each line, and flattening that to
            # one voice would silently recast the episode on a resume.
            log.info("diarize disabled -> keeping the %d speaker(s) already assigned",
                     len(job.speakers))
            return None
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
        raise RuntimeError("diarize needs vocals or source audio (extract/separate must run first)")

    try:
        context = pooled_model(
            ("diarization", MODEL_ID),
            lambda: _load_pipeline(token),
            job.transcription_options.get("keep_models_loaded", False),
        )
        with context as pipe:
            log.info("diarizing %s with %s", audio.name, MODEL_ID)
            diarization = pipe(str(audio))
            if hasattr(diarization, "speaker_diarization"):
                diarization = diarization.speaker_diarization
            del pipe
    except Exception as exc:  # noqa: BLE001 — gated model, bad token, download failure
        _single_narrator(
            job,
            f"could not load {MODEL_ID} ({exc}); accept the model terms at "
            f"huggingface.co/{MODEL_ID} and check HF_TOKEN",
        )
        return None

    _assign_speakers(job, diarization)
    log.info("diarize -> %d speakers (%s)", len(job.speakers), ", ".join(job.speakers))
    return None
