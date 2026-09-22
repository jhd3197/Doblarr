"""Shared plumbing for pipeline stages.

Two uniform short-circuits, both expressed as sentinels the `@stage` decorator
logs and swallows (the pipeline ignores stage return values; stages mutate the
job in place):

- `dry("what it would do")` — dry-run: the stage planned (job paths set) but
  skips the real work.
- `cached(artifacts, job.input_file, force)` — checkpoint/resume: if every
  declared artifact exists and is at least as fresh as the job's input file,
  the stage's work is already done and it returns the skip signal. `force`
  bypasses. Stages with in-memory outputs (transcribe/diarize/translate) don't
  declare artifacts and never skip.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from ..artifacts import read_json, stamp
from ..cues import (
    CUE_SCHEMA_VERSION,
    SourceReference,
    adopt_legacy,
    apply_cue_payload,
    check_schema,
    cue_payload,
    ensure_identity,
    script_ref,
    validate_cues,
)


class DryRunPlan:
    """Returned by a stage in dry-run mode; carries what would have run."""

    def __init__(self, detail: str):
        self.detail = detail


class CachedPlan:
    """Returned by a stage whose declared artifacts are fresh enough to skip."""

    def __init__(self, artifacts: list[Path]):
        self.artifacts = artifacts


Plan = DryRunPlan | CachedPlan


def dry(detail: str) -> DryRunPlan:
    return DryRunPlan(detail)


def work_stem(job) -> str:
    """Work-dir artifact stem; teases get a '.tease' namespace so a teaser never
    poisons the full dub's checkpoint cache (and vice versa)."""
    stem = job.input_file.stem
    return f"{stem}.{job.kind}" if job.kind in {"tease", "audition"} else stem


def cached(artifacts: Path | Iterable[Path], input_file: Path,
           force: bool = False) -> CachedPlan | None:
    """A CachedPlan if all artifacts exist and are fresh vs the input, else None."""
    if force:
        return None
    paths = [Path(a) for a in ([artifacts] if isinstance(artifacts, Path) else artifacts)]
    if not paths:
        return None
    try:
        input_mtime = input_file.stat().st_mtime
    except OSError:
        return None  # can't verify freshness without the input file
    for p in paths:
        try:
            if p.stat().st_size == 0 or p.stat().st_mtime < input_mtime:
                return None  # stale artifact — redo the stage
        except OSError:
            return None      # missing artifact — do the work
    return CachedPlan(paths)


def script_path(job, work_dir: Path) -> Path:
    """Where the persisted transcript+translation lives for a job."""
    return work_dir / f"{work_stem(job)}.script.json"


def save_script(job, work_dir: Path) -> Path:
    """Persist segments + speakers so a retry skips transcribe/diarize/translate.

    voicebox profile ids are deliberately NOT saved — synthesize re-resolves
    them by profile name, so a reset voicebox server can't poison the cache.
    """
    p = script_path(job, work_dir)
    ensure_identity(job)
    validate_cues(job.segments, job.cue_lineage)
    payload = {
        "cue_schema": CUE_SCHEMA_VERSION,
        "script_ref": job.script_ref,
        "source_reference": (job.source_reference.as_dict()
                             if job.source_reference else None),
        "cue_lineage": {k: list(v) for k, v in job.cue_lineage.items()},
        "nonverbal": job.nonverbal,
        # The measured dialogue reference is frozen with the script: a resume
        # must compare against the baseline this run established, not one
        # recomputed over whichever cues happen to be measurable next time.
        "dialogue_baseline": job.dialogue_baseline,
        "manual_gains": job.manual_gains,
        "source_track": str(job.source_track) if job.source_track else None,
        "transcription_options": job.transcription_options,
        "translation_options": job.translation_options,
        "audio": stamp(job.source_audio),
        "script_lang": job.script_lang,
        "identity": {"input": str(job.input_file.resolve()),
                     "source_lang": job.source_lang, "target_lang": job.target_lang,
                     "subtitles": (str(job.subtitle_file.resolve())
                                   if job.subtitle_file else None)},
        "script_is_target": job.script_is_target,
        "speakers": [s.label for s in job.speakers.values()],
        "segments": [
            {"index": s.index, "start": s.start, "end": s.end,
             "text_src": s.text_src, "speaker": s.speaker,
             "words": s.words, "issues": s.issues, "delivery": s.delivery,
             "voice": s.voice, "revision": s.revision,
             "source_start": s.source_start,
             "tts_text": s.tts_text, "applied_rules": s.applied_rules,
             "translation_provenance": s.translation_provenance,
             "memory_context": s.memory_context,
             "text_translated": s.text_translated,
             "cue": cue_payload(s)}
            for s in job.segments
        ],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(".partial.json")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(p)
    return p


def load_script(job, work_dir: Path, force: bool = False) -> Path | None:
    """Restore segments+speakers from a cached script when fresh vs the input."""
    from ..models import Segment, Speaker

    p = script_path(job, work_dir)
    if cached(p, job.input_file, force) is None:
        return None
    payload = read_json(p)
    check_schema(payload.get("cue_schema"), "script cache")
    if payload.get("transcription_options", {}) != job.transcription_options:
        return None
    if payload.get("audio") != stamp(job.source_audio):
        return None
    identity = {"input": str(job.input_file.resolve()),
                "source_lang": job.source_lang, "target_lang": job.target_lang,
                "subtitles": str(job.subtitle_file.resolve()) if job.subtitle_file else None}
    if payload.get("identity") != identity:
        return None
    if job.subtitle_file and cached(p, job.subtitle_file) is None:
        return None
    job.segments = [Segment(index=s["index"], start=float(s["start"]),
                            end=float(s["end"]), text_src=s["text_src"],
                            speaker=s.get("speaker", "SPEAKER_00"),
                            words=s.get("words", []), issues=s.get("issues", []),
                            delivery=s.get("delivery", ""), voice=s.get("voice"),
                            revision=s.get("revision", 0),
                            source_start=s.get("source_start"),
                            tts_text=s.get("tts_text"),
                            applied_rules=s.get("applied_rules", []),
                            translation_provenance=s.get("translation_provenance", {}),
                            memory_context=s.get("memory_context", {}),
                            text_translated=s.get("text_translated"))
                    for s in payload["segments"]]
    job.speakers = {label: Speaker(label=label) for label in payload.get("speakers", [])}
    job.script_is_target = bool(payload.get("script_is_target"))
    job.script_lang = payload.get("script_lang")
    _restore_cues(job, payload)
    if payload.get("translation_options", {}) != job.translation_options:
        for seg in job.segments:
            if not job.script_is_target or job.translation_options.get("adapt_region"):
                seg.text_translated = None
                seg.translation_provenance = {}
    return p


def _restore_cues(job, payload: dict) -> None:
    """Restore typed cue records, migrating a pre-schema snapshot deterministically.

    A snapshot without cue records gets identities derived from the script it
    was made from, so repeating the migration always produces the same cue IDs.
    Its stored clip is registered as an `unknown` artifact: a saved clip may
    already be normalized or time-fitted, and labelling it the raw generation
    would let a later stage reprocess processed audio.
    """
    saved = payload.get("identity") or {}
    job.script_ref = payload.get("script_ref") or script_ref(
        saved.get("input") or job.input_file,
        saved.get("source_lang") or job.source_lang,
        saved.get("subtitles"),
    )
    reference = payload.get("source_reference")
    if reference:
        job.source_reference = SourceReference.from_dict(reference)
    job.cue_lineage = {str(k): [str(c) for c in v]
                       for k, v in (payload.get("cue_lineage") or {}).items()}
    job.nonverbal = list(payload.get("nonverbal") or [])
    job.dialogue_baseline = dict(payload.get("dialogue_baseline") or {})
    job.manual_gains = {str(k): float(v)
                        for k, v in (payload.get("manual_gains") or {}).items()}
    track = payload.get("source_track")
    if track and job.source_track is None:
        job.source_track = Path(track)
    for seg, row in zip(job.segments, payload["segments"], strict=True):
        record = row.get("cue")
        if record:
            apply_cue_payload(seg, record)
        else:
            adopt_legacy(seg, job.script_ref)
        current = seg.audio.current()
        if current and current.path:
            seg.audio_clip = Path(current.path)
    ensure_identity(job)
    validate_cues(job.segments, job.cue_lineage)


def stage(name: str):
    """Log a stage's `dry(...)` / `cached(...)` sentinel and swallow it."""
    log = logging.getLogger(f"doblarr.{name}")

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, DryRunPlan):
                log.info("  [dry-run] %s", result.detail)
                return None
            if isinstance(result, CachedPlan):
                job = args[0] if args else kwargs.get("job")
                if job is not None:
                    job.metrics["stage_cache_hits"] = job.metrics.get("stage_cache_hits", 0) + 1
                names = ", ".join(p.name for p in result.artifacts[:3])
                log.info("  skipping (cached): %s", names)
                return None
            return result
        return wrapper
    return deco
