"""Stage 3 — get timed dialogue segments.

Two sources:
  - subtitles: parse an .srt/.ass file (fast, free, accurate timing/text)
  - whisper:   transcribe the vocals stem with WhisperX (word-level timing)
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import DubJob, Segment

log = logging.getLogger("doblarr.transcribe")


def run(job: DubJob, source: str = "subtitles", whisper_model: str = "large-v3",
        dry_run: bool = False) -> None:
    if source == "subtitles":
        _from_subtitles(job, dry_run=dry_run)
    elif source == "whisper":
        _from_whisper(job, whisper_model, dry_run=dry_run)
    else:
        raise ValueError(f"unknown transcribe source: {source}")
    log.info("transcribe (%s) -> %d segments", source, len(job.segments))


def _from_subtitles(job: DubJob, dry_run: bool = False) -> None:
    if not job.subtitle_file:
        if dry_run:
            log.info("  [dry-run] no --subs given; a real run needs a subtitle file "
                     "(or switch transcribe.source to 'whisper')")
            return
        raise ValueError("transcribe source=subtitles but no subtitle_file given")
    if dry_run:
        log.info("  [dry-run] would parse subtitles: %s", job.subtitle_file.name)
        return
    import pysubs2  # lazy import

    subs = pysubs2.load(str(job.subtitle_file))
    job.segments = [
        Segment(index=i, start=line.start / 1000.0, end=line.end / 1000.0,
                text_src=line.plaintext.replace("\n", " ").strip())
        for i, line in enumerate(subs)
        if line.plaintext.strip()
    ]


def _from_whisper(job: DubJob, whisper_model: str, dry_run: bool = False) -> None:
    if dry_run:
        log.info("  [dry-run] would run WhisperX (%s) on %s",
                 whisper_model, job.vocals or job.source_audio)
        return
    # TODO: implement with WhisperX for word-level timestamps.
    #   import whisperx
    #   model = whisperx.load_model(whisper_model, device="cuda")
    #   result = model.transcribe(str(job.vocals))
    #   ... align, then build Segment list.
    raise NotImplementedError("transcribe(whisper): wire up WhisperX (see TODO).")
