"""Stage 3 — get timed dialogue segments.

Sources, in order of preference:
  - an external subtitle file (job.subtitle_file)
  - an embedded subtitle track (target language preferred → already the script)
  - whisper on the audio (stub; no timestamps from voicebox transcribe)
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import subtitles
from ..models import DubJob, Segment

log = logging.getLogger("doblarr.transcribe")


def run(job: DubJob, work_dir: Path, source: str = "subtitles",
        whisper_model: str = "large-v3", vb=None,
        segment_limit: int | None = None, dry_run: bool = False) -> None:
    if dry_run:
        log.info("  [dry-run] would build timed segments (%s)", source)
        return

    sub_path = job.subtitle_file
    used_lang = None
    if not sub_path:
        # Try an embedded subtitle track — prefer the target language (already the script).
        streams = subtitles.sub_streams(job.input_file)
        chosen = subtitles.pick_stream(streams, job.target_lang) \
            or subtitles.pick_stream(streams, job.source_lang) \
            or (next((s for s in streams if s["codec"] in subtitles._TEXT_CODECS), None))
        if not chosen:
            raise RuntimeError(
                "no subtitle track found (give --subs or add WhisperX for timing); "
                f"streams: {streams}")
        used_lang = chosen["lang"]
        sub_path = subtitles.extract_srt(
            job.input_file, chosen["index"],
            work_dir / f"{job.input_file.stem}.{used_lang}.srt")
        log.info("extracted embedded %s subtitles -> %s", used_lang, sub_path.name)

    import pysubs2  # lazy
    subs = pysubs2.load(str(sub_path))
    segs = [
        Segment(index=i, start=line.start / 1000.0, end=line.end / 1000.0,
                text_src=line.plaintext.replace("\n", " ").strip())
        for i, line in enumerate(subs) if line.plaintext.strip()
    ]
    if segment_limit:
        segs = segs[:segment_limit]
    # Re-index after limiting so clip names stay 0..N.
    for n, s in enumerate(segs):
        s.index = n
    job.segments = segs

    # If we used the target-language track, the text is already the translation.
    if used_lang and used_lang.lower().startswith(job.target_lang.strip().lower()[:2]):
        job.script_is_target = True
        for s in job.segments:
            s.text_translated = s.text_src

    log.info("transcribe -> %d segments%s", len(job.segments),
             " (already target language)" if job.script_is_target else "")
