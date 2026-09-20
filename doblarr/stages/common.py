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
    return f"{stem}.tease" if job.kind == "tease" else stem


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
    payload = {
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
             "text_translated": s.text_translated}
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
                            text_translated=s.get("text_translated"))
                    for s in payload["segments"]]
    job.speakers = {label: Speaker(label=label) for label in payload.get("speakers", [])}
    job.script_is_target = bool(payload.get("script_is_target"))
    job.script_lang = payload.get("script_lang")
    if payload.get("translation_options", {}) != job.translation_options:
        for seg in job.segments:
            if not job.script_is_target:
                seg.text_translated = None
    return p


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
