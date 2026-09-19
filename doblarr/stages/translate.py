"""Stage 5 — translate each segment into the target language, for dubbing.

We pass a per-line character budget derived from the original spoken duration so
the translator can keep lines short enough to fit the time slot.
"""

from __future__ import annotations

import logging

from ..clients.translator import Translator
from ..models import DubJob

log = logging.getLogger("doblarr.translate")

# Rough speaking rate used to turn a time slot into a character budget.
CHARS_PER_SECOND = 14


def run(job: DubJob, translator: Translator, dry_run: bool = False) -> None:
    log.info("translate %d segments -> %s", len(job.segments), job.target_lang)
    for seg in job.segments:
        budget = int(seg.duration * CHARS_PER_SECOND) or None
        if dry_run:
            seg.text_translated = seg.text_src  # passthrough placeholder
            continue
        seg.text_translated = translator.translate(
            seg.text_src, job.source_lang, job.target_lang, target_chars=budget,
        )
