"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
import threading

from .clients.translator import build_translator
from .config import Config
from .errors import JobCancelled
from .models import DubJob
from .services import Services
from .stages import (
    diarize,
    extract,
    fit_timing,
    mix,
    mux,
    separate,
    synthesize,
    transcribe,
    translate,
)

log = logging.getLogger("doblarr.pipeline")


def run_job(job: DubJob, config: Config, dry_run: bool = False,
            on_stage=None, cancel_event: threading.Event | None = None,
            services: Services | None = None) -> DubJob:
    """Run every stage in order, mutating and returning the job.

    `on_stage(name, index, total)` is called before each stage, so a caller (the
    job worker) can report progress. `cancel_event` is checked between stages —
    raise JobCancelled when set — and handed to the ffmpeg-bound stages, so a
    cancel kills an in-flight ffmpeg run. Note: a cancel while waiting on a
    voicebox *remote* generation aborts the wait but leaves the server-side
    generation running (voicebox has no cancel endpoint). `services` supplies
    the voicebox client; one is built from config when not given (CLI path).
    """
    work = config.work_dir
    out = config.output_dir
    vb = (services or Services(config)).voicebox
    translator = build_translator(config["translate"]["provider"],
                                  config["translate"]["model"], voicebox_client=vb)
    seg_limit = config["dub"].get("segment_limit")

    log.info("=== Doblarr job: %s ===", job.summary())

    steps = [
        ("probe", lambda: extract.run(job, work, dry_run=dry_run, cancel=cancel_event)),
        ("separate", lambda: separate.run(job, work, model=config["separate"]["model"],
                                          dry_run=dry_run)),
        ("transcribe", lambda: transcribe.run(job, work, source=config["transcribe"]["source"],
                                              whisper_model=config["transcribe"]["whisper_model"],
                                              vb=vb, segment_limit=seg_limit, dry_run=dry_run)),
        ("diarize", lambda: diarize.run(job, enabled=config["transcribe"]["diarize"],
                                        dry_run=dry_run)),
        ("translate", lambda: translate.run(job, translator, dry_run=dry_run)),
        ("synthesize", lambda: synthesize.run(job, vb, work,
                                              voice_mode=config["dub"]["voice_mode"],
                                              dry_run=dry_run, cancel=cancel_event)),
        ("fit", lambda: fit_timing.run(job, enabled=config["dub"]["duration_match"],
                                       dry_run=dry_run)),
        ("mix", lambda: mix.run(job, work, ducking_ratio=config["dub"]["ducking_ratio"],
                                dry_run=dry_run, cancel=cancel_event)),
        ("mux", lambda: mux.run(job, out,
                                track_name_template=config["dub"]["track_name_template"],
                                dry_run=dry_run, cancel=cancel_event)),
    ]
    total = len(steps)
    for i, (name, fn) in enumerate(steps):
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled(f"cancelled before stage {name}")
        if on_stage:
            on_stage(name, i, total)
        fn()

    log.info("=== done -> %s ===", job.output_file)
    return job
