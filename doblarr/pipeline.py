"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
from pathlib import Path

from .clients.translator import build_translator
from .clients.voicebox import VoiceboxClient
from .config import Config
from .models import DubJob
from .stages import (diarize, extract, fit_timing, mix, mux, separate,
                     synthesize, transcribe, translate)

log = logging.getLogger("doblarr.pipeline")


def run_job(job: DubJob, config: Config, dry_run: bool = False,
            on_stage=None) -> DubJob:
    """Run every stage in order, mutating and returning the job.

    `on_stage(name, index, total)` is called before each stage, so a caller (the
    job worker) can report progress.
    """
    work = config.work_dir
    out = config.output_dir
    vb = VoiceboxClient(config["voicebox"]["base_url"],
                        timeout=config["voicebox"]["timeout_seconds"])
    translator = build_translator(config["translate"]["provider"],
                                  config["translate"]["model"])

    log.info("=== Doblarr job: %s ===", job.summary())

    steps = [
        ("probe", lambda: extract.run(job, work, dry_run=dry_run)),
        ("separate", lambda: separate.run(job, work, model=config["separate"]["model"], dry_run=dry_run)),
        ("transcribe", lambda: transcribe.run(job, source=config["transcribe"]["source"],
                                              whisper_model=config["transcribe"]["whisper_model"], dry_run=dry_run)),
        ("diarize", lambda: diarize.run(job, enabled=config["transcribe"]["diarize"], dry_run=dry_run)),
        ("translate", lambda: translate.run(job, translator, dry_run=dry_run)),
        ("synthesize", lambda: synthesize.run(job, vb, work, voice_mode=config["dub"]["voice_mode"], dry_run=dry_run)),
        ("fit", lambda: fit_timing.run(job, enabled=config["dub"]["duration_match"], dry_run=dry_run)),
        ("mix", lambda: mix.run(job, work, ducking_ratio=config["dub"]["ducking_ratio"], dry_run=dry_run)),
        ("mux", lambda: mux.run(job, out, track_name_template=config["dub"]["track_name_template"], dry_run=dry_run)),
    ]
    total = len(steps)
    for i, (name, fn) in enumerate(steps):
        if on_stage:
            on_stage(name, i, total)
        fn()

    log.info("=== done -> %s ===", job.output_file)
    return job
