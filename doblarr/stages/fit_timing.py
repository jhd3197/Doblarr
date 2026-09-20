"""Stage 7 — fit each generated clip into its original time slot (isochrony).

Strategy (cheapest first):
  1. If translation already fits, leave it (short clips too — the mix pads the
     rest of the slot with the background bed).
  2. If long, time-compress with ffmpeg atempo (no pitch change), clamped to
     MAX_STRETCH so the voice still sounds natural.
  3. If still long after max stretch, keep the clamped clip and log a warning —
     the mix places clips by start time, so the overlap is audible and must be
     visible in the logs.
This is the single biggest driver of perceived dub quality.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..ffmpeg import run_ffmpeg, run_ffprobe
from ..models import DubJob, Segment
from .common import Plan, cached, dry, stage

log = logging.getLogger("doblarr.fit_timing")

# Stretch beyond this factor sounds unnatural; prefer re-translation instead.
MAX_STRETCH = 1.3
# Overflow within this ratio is inaudible once mixed; don't resample for it.
FIT_SLACK = 1.02


def _duration(path: Path, cancel: threading.Event | None = None) -> float:
    out = run_ffprobe(["-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                      cancel=cancel)
    return float(out.strip())


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5-2.0; chain filters for larger corrections."""
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


@stage("fit_timing")
def run(job: DubJob, work_dir: Path, enabled: bool = True, dry_run: bool = False,
        cancel: threading.Event | None = None, force: bool = False) -> Plan | None:
    if not enabled:
        log.info("fit_timing disabled")
        return None
    log.info("fit_timing over %d clips (max stretch %.2fx)",
             len(job.segments), MAX_STRETCH)
    if dry_run:
        return dry("would measure each clip vs slot and time-stretch to fit")

    clips = [(s, Path(s.audio_clip)) for s in job.segments
             if s.audio_clip and Path(s.audio_clip).exists()]
    if not clips:
        log.info("fit_timing: no generated clips to fit")
        return None

    clips_dir = work_dir / ("clips-tease" if job.kind == "tease" else "clips")
    fit_dir = clips_dir.with_name(clips_dir.name + "-fit")

    plan: list[tuple[Segment, Path, float, float]] = []  # seg, dest, actual, factor
    for s, src in clips:
        if s.duration <= 0:
            continue
        actual = _duration(src, cancel=cancel)
        if actual <= s.duration * FIT_SLACK:
            continue
        factor = min(actual / s.duration, MAX_STRETCH)
        plan.append((s, fit_dir / src.name, actual, factor))

    if not plan:
        log.info("fit_timing: every clip fits its slot")
        return None

    hit = cached([dest for _, dest, _, _ in plan], job.input_file, force)
    if hit:
        for s, dest, _, _ in plan:
            s.audio_clip = dest
        return hit

    fit_dir.mkdir(parents=True, exist_ok=True)
    by_start = sorted(job.segments, key=lambda t: t.start)
    for s, dest, actual, factor in plan:
        run_ffmpeg(["-y", "-i", str(s.audio_clip), "-af", _atempo_chain(factor),
                    "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(dest)],
                   cancel=cancel)
        if actual > s.duration * MAX_STRETCH * FIT_SLACK:
            over = actual / MAX_STRETCH - s.duration
            nxt = next((t for t in by_start if t.start >= s.end), None)
            spill = (f"; overlaps line {nxt.index} at {nxt.start:.2f}s"
                     if nxt else "; runs past the end of its slot")
            log.warning("line %d: %.2fs clip in %.2fs slot — %.2fs over even at max "
                        "%.2fx stretch%s", s.index, actual, s.duration, over,
                        MAX_STRETCH, spill)
        else:
            log.info("  line %d: %.2fs -> %.2fs (atempo %.2f)",
                     s.index, actual, actual / factor, factor)
        s.audio_clip = dest
    return None
