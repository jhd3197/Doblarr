"""Immutable per-run review snapshots and explicit line overrides."""

import math
import os
import shutil
from dataclasses import asdict

from .artifacts import digest
from .cues import (
    CUE_SCHEMA_VERSION,
    cue_payload,
    ensure_identity,
    register_unknown_clip,
    resolve_cue,
)
from .telemetry import write_json


def snapshot_revision(job) -> str:
    """Identity of the decisions a reviewer is looking at.

    An edit carries the revision it was made against, so a snapshot that has
    moved on (a rerun, another reviewer, a new take) rejects the stale edit
    instead of overwriting the newer decision.
    """
    return digest([
        {
            "cue": seg.cue_id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text_translated or seg.text_src,
            "delivery": seg.delivery,
            "voice": seg.voice,
            "revision": seg.revision,
            "issues": sorted(seg.issues),
            "take": seg.audio.selection.take_id if seg.audio.selection else None,
            "render": (seg.audio.current().fingerprint if seg.audio.current() else None),
        }
        for seg in job.segments
    ])[:16]


def apply_edits(job, edits, lineage=None):
    """Apply reviewed line overrides, addressed by cue ID or by legacy index.

    An edit naming a cue that a split or merge retired raises an actionable
    conflict (see `cues.resolve_cue`) rather than being redirected onto whatever
    line now occupies that position.
    """
    lineage = job.cue_lineage if lineage is None else lineage
    ensure_identity(job)
    targets = {}
    for key, edit in edits.items():
        # New edits carry the cue ID they were made against; legacy overrides
        # only have the index they were saved under.
        seg = resolve_cue(job.segments, (edit or {}).get("cue") or key, lineage)
        targets.setdefault(id(seg), []).append(key)
    for keys in targets.values():
        if len(keys) > 1:
            raise ValueError(f"line edits {sorted(keys)} address the same cue")
    kept = []
    for seg in job.segments:
        keys = targets.get(id(seg)) or []
        edit = edits[keys[0]] if keys else {}
        if edit.get("exclude"):
            continue
        if "text" in edit:
            if not str(edit["text"]).strip():
                raise ValueError("edited dialogue must not be empty")
            seg.text_translated = edit["text"].strip()
            seg.translation_provenance = {"method": "manual", "reason": "review-edit"}
        start, end = seg.start, seg.end
        seg.start = float(edit.get("start", seg.start))
        seg.end = float(edit.get("end", seg.end))
        if (
            not math.isfinite(seg.start)
            or not math.isfinite(seg.end)
            or seg.start < 0
            or seg.end <= seg.start
        ):
            raise ValueError(f"line {seg.index} needs a positive time window")
        if (seg.start, seg.end) != (start, end):
            # Target placement only. The recorded source interval is evidence of
            # what was actually spoken and must survive any timing edit.
            seg.placement.offset += seg.start - start
        for key in ("voice", "delivery", "revision"):
            if key in edit:
                setattr(seg, key, edit[key])
        kept.append(seg)
    if not kept:
        raise ValueError("review edits excluded every spoken line")
    job.segments = kept


def write_review(job, root):
    if not job.report_file:
        return
    ensure_identity(job)
    job.review_file = root / "reviews" / job.report_file.name
    rows = []
    for seg in job.segments:
        register_unknown_clip(seg)
        row = asdict(seg)
        clip = seg.audio_clip
        if clip and clip.is_file():
            snapshot = root / "reviews" / job.report_file.stem / f"line_{seg.index}.wav"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if not snapshot.exists():
                try:
                    os.link(clip, snapshot)
                except OSError:
                    shutil.copy2(clip, snapshot)
            clip = snapshot
        row["audio_clip"] = str(clip.resolve()) if clip else None
        speaker = job.speakers.get(seg.speaker)
        row["profile"] = seg.voice or (speaker.voicebox_profile_id if speaker else None)
        # One versioned codec: the review snapshot carries the same cue records
        # the script cache and the saved version manifest do. They live under
        # "cue" only, so the row never holds two copies that can disagree.
        for owned in ("lineage", "source", "placement", "audio", "findings"):
            row.pop(owned, None)
        row["cue"] = cue_payload(seg)
        rows.append(row)
    write_json(
        job.review_file,
        {
            "version": 1,
            "cue_schema": CUE_SCHEMA_VERSION,
            "revision": snapshot_revision(job),
            "language": job.target_lang,
            "source_language": job.script_lang or job.source_lang,
            "locale": job.target_locale or job.target_lang,
            "source_reference": (job.source_reference.as_dict()
                                 if job.source_reference else None),
            "cue_lineage": {k: list(v) for k, v in job.cue_lineage.items()},
            "nonverbal": job.nonverbal,
            "segments": rows,
            "metrics": job.metrics,
            "flagged": sum(bool(s.issues) for s in job.segments),
        },
    )
