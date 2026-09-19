"""Stage 7 — fit each generated clip into its original time slot (isochrony).

Strategy (cheapest first):
  1. If translation already fits, leave it.
  2. If slightly long, time-stretch without pitch change (rubberband / atempo).
  3. If very long, ask upstream to re-translate shorter (handled via max attempts).
This is the single biggest driver of perceived dub quality.
"""

from __future__ import annotations

import logging

from ..models import DubJob
from .common import DryRunPlan, dry, stage

log = logging.getLogger("doblarr.fit_timing")

# Stretch beyond this factor sounds unnatural; prefer re-translation instead.
MAX_STRETCH = 1.3


@stage("fit_timing")
def run(job: DubJob, enabled: bool = True, dry_run: bool = False) -> DryRunPlan | None:
    if not enabled:
        log.info("fit_timing disabled")
        return None
    log.info("fit_timing over %d clips (max stretch %.2fx)",
             len(job.segments), MAX_STRETCH)
    if dry_run:
        return dry("would measure each clip vs slot and time-stretch to fit")
    # v1: pass-through (clips play at natural length; mix places them by start time).
    # TODO: measure clip vs seg.duration and atempo/rubberband within MAX_STRETCH.
    log.info("fit_timing: v1 pass-through (no stretch yet)")
    return None
