"""Stage 4 — speaker diarization: label who speaks each segment.

Lets Doblarr clone a distinct voice per character. Two ways to get there:

- `pyannote`: the full diarization pipeline (needs HF_TOKEN and the model
  terms accepted on HuggingFace). It finds turns in the audio itself.
- `local`: doblarr.speakers groups the already-cut lines by voice with an
  ungated embedding model. No account, nothing leaves the machine.

`transcribe.diarizer: auto` uses pyannote when a token is set and the local
grouping otherwise. Only when neither can run does a job fall back to a single
narrator voice, so teases and full dubs still run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .. import hardware, speakers, voice_models
from ..cues import SOURCE, Span, split_cue
from ..model_pool import model as pooled_model
from ..models import DubJob, Segment, Speaker
from .common import DryRunPlan, dry, stage, work_stem

log = logging.getLogger("doblarr.diarize")

MODEL_ID = "pyannote/speaker-diarization-3.1"


def _single_narrator(job: DubJob, reason: str) -> None:
    """Give every line one voice, and record why so the job says so and a resume retries."""
    job.speakers = {"NARRATOR": Speaker(label="NARRATOR")}
    for seg in job.segments:
        seg.speaker = "NARRATOR"
    job.metrics["diarize"] = {"method": "narrator", "reason": reason}
    log.warning("diarize: %s — falling back to a single narrator voice", reason)


def narrator_fallback(job: DubJob) -> str | None:
    """Why this job's lines all share one voice, or None when diarization ran."""
    found = job.metrics.get("diarize") or {}
    return found.get("reason") if found.get("method") == "narrator" else None


def _load_pipeline(token: str, device: hardware.Device | None = None):
    """Load the pyannote diarization pipeline on the chosen device."""
    from pyannote.audio import Pipeline

    from_pretrained: Any = Pipeline.from_pretrained
    try:
        pipe = from_pretrained(MODEL_ID, use_auth_token=token)
    except TypeError:  # pyannote.audio >= 4 renamed the kwarg
        pipe = from_pretrained(MODEL_ID, token=token)

    device = device or hardware.Device()
    if device.kind != "cpu":
        import torch

        pipe.to(torch.device(device.torch))
    log.info("diarize: using %s", device)
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


def usable_tracks(job, audio, voices: dict) -> tuple[list, list[dict]]:
    """The permitted other audio tracks that line up with the original, and a
    record of every track considered (doblarr.track_alignment).

    `speakers.tracks` is the permission (which tracks may be heard at all);
    alignment is the check. A track that is not verifiably the same cut on the
    same clock is reported, never used.
    """
    from .. import track_alignment

    if not voices.get("tracks"):
        return [], []
    try:
        found = speakers.other_tracks(job.input_file, job.source_lang, audio.parent,
                                      work_stem(job), voices["tracks"])
    except (OSError, ValueError) as exc:
        log.warning("diarize: the other audio tracks could not be read (%s)", exc)
        return [], []
    if not found:
        return [], []
    described = {t["stream"]: t for t in speakers.audio_tracks(job.input_file)} \
        if job.input_file and job.input_file.is_file() else {}
    reference = job.source_track or job.source_audio or audio
    evidence = track_alignment.check_tracks(
        reference, [{"stream": stream, "path": path,
                     "lang": described.get(stream, {}).get("lang", ""),
                     "title": described.get(stream, {}).get("title", "")}
                    for stream, path in found],
        evaluation_streams=set(voices.get("evaluation_streams") or ()),
        max_offset=float(voices.get("max_offset", 2.0)),
        min_correlation=float(voices.get("min_correlation", 0.45)),
        verify=bool(voices.get("verify", True)))
    allowed = track_alignment.usable(evidence)
    for row in evidence:
        if int(row["stream"]) not in allowed:
            log.info("diarize: not using audio %s (%s): %s", row["stream"], row.get("state"),
                     row.get("reason", ""))
    return [(s, p) for s, p in found if s in allowed], evidence


def _local(job: DubJob, audio, device, why: str, voices: dict | None = None) -> bool:
    """Group the lines by voice locally; False when that cannot run either."""
    voices = voices or {}
    try:
        sidecar = audio.parent / f"{work_stem(job)}.speakers.json"
        tracks, evidence = usable_tracks(job, audio, voices)
        speakers.assign(job, audio, sidecar=sidecar,
                        device="cpu" if device is None else device.torch,
                        threshold=voices.get("threshold"), models=voices.get("models"),
                        models_dir=voices.get("models_dir"), tracks=tracks,
                        track_evidence=evidence)
    except Exception as exc:  # noqa: BLE001 - missing model or library: narrator fallback
        chosen = voices.get("models") or []
        if [m.id for m in chosen] != [voice_models.DEFAULT]:
            # A model that could not be downloaded (offline, moved release)
            # should not cost the cast: the Hub default has no such step.
            log.warning("diarize: voice models %s failed (%s); trying %s alone",
                        voice_models.combined_id(chosen) or "default", exc, voice_models.DEFAULT)
            return _local(job, audio, device, why, {
                **voices, "models": voice_models.resolve(voice_models.DEFAULT), "threshold": None})
        log.warning("diarize: local voice grouping failed (%s)", exc)
        return False
    job.metrics.setdefault("diarize", {}).update(method="local", reason=why)
    log.info("diarize (local, %s) -> %d speakers", why, len(job.speakers))
    return True


@stage("diarize")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False,
        compute: dict | None = None, method: str = "auto",
        voices: dict | None = None) -> DryRunPlan | None:
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

    if job.speakers and narrator_fallback(job) is None:
        log.info("diarize: speakers already assigned (restored script) — skipping")
        return None
    if job.speakers:
        # The cached cast is a fallback from a run that could not diarize
        # (out of memory, offline Hub); the cause may be gone, so try again.
        log.info("diarize: retrying after an earlier narrator fallback")
        job.speakers = {}
        job.metrics.pop("diarize", None)

    token = os.environ.get("HF_TOKEN")
    if method == "local" or (method == "auto" and not token):
        audio = job.vocals or job.source_audio
        if audio is not None and _local(job, audio, None, "no HF_TOKEN" if not token
                                        else "transcribe.diarizer is local", voices):
            return None
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

    # Resolved outside the fallback below: an explicit device that is missing
    # is a configuration error to fix, not a reason to quietly lose the cast.
    device = hardware.resolve_device("diarize", compute)
    job.metrics.setdefault("devices", {})["diarize"] = device.torch
    try:
        context = pooled_model(
            ("diarization", MODEL_ID, device.torch),
            lambda: _load_pipeline(token, device),
            job.transcription_options.get("keep_models_loaded", False),
        )
        with context as pipe:
            log.info("diarizing %s with %s", audio.name, MODEL_ID)
            diarization = pipe(str(audio))
            if hasattr(diarization, "speaker_diarization"):
                diarization = diarization.speaker_diarization
            del pipe
    except Exception as exc:  # noqa: BLE001 — gated model, bad token, download failure
        if method == "auto" and _local(job, audio, device, f"pyannote unavailable: {exc}",
                                         voices):
            return None
        _single_narrator(
            job,
            f"could not load {MODEL_ID} ({exc}); accept the model terms at "
            f"huggingface.co/{MODEL_ID} and check HF_TOKEN",
        )
        return None

    _assign_speakers(job, diarization)
    log.info("diarize -> %d speakers (%s)", len(job.speakers), ", ".join(job.speakers))
    return None
