"""Immutable per-run review snapshots and explicit line overrides."""

import math
import os
import shutil
from dataclasses import asdict

from .telemetry import write_json


def apply_edits(job, edits):
    known = {str(s.index) for s in job.segments}
    unknown = set(edits) - known
    if unknown:
        raise ValueError(f"line edits refer to unknown segment IDs: {sorted(unknown)}")
    kept = []
    for seg in job.segments:
        edit = edits.get(str(seg.index), {})
        if edit.get("exclude"):
            continue
        if "text" in edit:
            if not str(edit["text"]).strip():
                raise ValueError("edited dialogue must not be empty")
            seg.text_translated = edit["text"].strip()
        seg.start = float(edit.get("start", seg.start))
        seg.end = float(edit.get("end", seg.end))
        if (
            not math.isfinite(seg.start)
            or not math.isfinite(seg.end)
            or seg.start < 0
            or seg.end <= seg.start
        ):
            raise ValueError(f"line {seg.index} needs a positive time window")
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
    job.review_file = root / "reviews" / job.report_file.name
    rows = []
    for seg in job.segments:
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
        rows.append(row)
    write_json(
        job.review_file,
        {
            "version": 1,
            "language": job.target_lang,
            "locale": job.target_locale or job.target_lang,
            "segments": rows,
            "metrics": job.metrics,
            "flagged": sum(bool(s.issues) for s in job.segments),
        },
    )
