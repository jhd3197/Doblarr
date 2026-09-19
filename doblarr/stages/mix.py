"""Stage 8 — mix the dubbed lines over the background bed (ffmpeg).

Each generated clip is delayed to its segment start and mixed over the bed
(the separated M&E, or — in v1 without Demucs — the original audio at reduced
volume). Produces one dubbed track spanning the segments.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..ffmpeg import run_ffmpeg
from ..models import DubJob
from .common import DryRunPlan, dry, stage

log = logging.getLogger("doblarr.mix")

BED_VOLUME = 0.35   # duck the original bed under the dubbed dialogue


@stage("mix")
def run(job: DubJob, work_dir: Path, ducking_ratio: str = "12:1",
        dry_run: bool = False, cancel: threading.Event | None = None) -> DryRunPlan | None:
    out = work_dir / f"{job.input_file.stem}.{job.target_lang}.dub.wav"
    job.dubbed_track = out
    log.info("mix dialogue over bed -> %s", out.name)
    if dry_run:
        return dry(f"would place {len(job.segments)} clips and duck the bed")

    segs = [s for s in job.segments if s.audio_clip and Path(s.audio_clip).exists()]
    if not segs:
        raise RuntimeError("mix: no generated clips to place")
    bed = job.background or job.source_audio
    win_start = min(s.start for s in segs)
    win_end = max(s.end for s in segs) + 3.0
    dur = win_end - win_start

    args = ["-y", "-ss", str(win_start), "-t", str(dur), "-i", str(bed)]
    for s in segs:
        args += ["-i", str(s.audio_clip)]

    parts = [f"[0:a]volume={BED_VOLUME},aformat=channel_layouts=stereo[bed]"]
    for i, s in enumerate(segs):
        d = max(0, int((s.start - win_start) * 1000))
        parts.append(f"[{i+1}:a]adelay={d}|{d},aformat=channel_layouts=stereo[c{i}]")
    mix_inputs = "[bed]" + "".join(f"[c{i}]" for i in range(len(segs)))
    parts.append(f"{mix_inputs}amix=inputs={len(segs)+1}:normalize=0[out]")

    args += ["-filter_complex", ";".join(parts), "-map", "[out]",
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args, cancel=cancel)
    log.info("mix -> %s (%.0fs window, %d lines)", out.name, dur, len(segs))
    return None
