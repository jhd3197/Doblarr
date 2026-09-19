"""Stage 5 — translate each segment into the target language, for dubbing.

We pass a per-line character budget derived from the original spoken duration so
the translator can keep lines short enough to fit the time slot.

Unlike the other stages this one does NOT use the @stage/dry() short-circuit:
its dry-run is real per-line work (passthrough placeholders must still populate
text_translated so downstream stages have something to plan with).
"""

from __future__ import annotations

import logging

from ..clients.translator import Translator
from ..models import DubJob

log = logging.getLogger("doblarr.translate")

# Rough speaking rate used to turn a time slot into a character budget.
CHARS_PER_SECOND = 14


def run(job: DubJob, translator: Translator, dry_run: bool = False) -> None:
    if job.script_is_target:
        log.info("translate: script already in %s — skipping", job.target_lang)
        for seg in job.segments:
            seg.text_translated = seg.text_translated or seg.text_src
        return
    log.info("translate %d segments -> %s", len(job.segments), job.target_lang)
    for seg in job.segments:
        if seg.text_translated:
            continue
        budget = int(seg.duration * CHARS_PER_SECOND) or None
        if dry_run:
            seg.text_translated = seg.text_src  # passthrough placeholder
            continue
        seg.text_translated = translator.translate(
            seg.text_src, job.source_lang, job.target_lang, target_chars=budget,
        )
