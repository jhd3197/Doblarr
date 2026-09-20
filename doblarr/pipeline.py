"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
import threading

from .artifacts import media_work
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
from .stages.common import save_script
from .telemetry import RunReport
from .voices import ensure_cast

log = logging.getLogger("doblarr.pipeline")


def run_job(job: DubJob, config: Config, dry_run: bool = False,
            on_stage=None, cancel_event: threading.Event | None = None,
            services: Services | None = None, force: bool = False,
            db=None, events=None, on_progress=None) -> DubJob:
    """Run every stage in order, mutating and returning the job.

    `on_stage(name, index, total)` is called before each stage, so a caller (the
    job worker) can report progress. `on_progress(stage, frac, detail)` is called
    by long-running stages with in-stage progress (e.g. "line 12/52"), frac in
    0..1. `cancel_event` is checked between stages —
    raise JobCancelled when set — and handed to the ffmpeg-bound stages, so a
    cancel kills an in-flight ffmpeg run. Note: a cancel while waiting on a
    voicebox *remote* generation aborts the wait but leaves the server-side
    generation running (voicebox has no cancel endpoint). `services` supplies
    the voicebox client; one is built from config when not given (CLI path).
    `force` re-runs every stage, ignoring cached work-dir artifacts.

    A `kind="tease"` job only dubs the first `dub.teaser_minutes` minutes: the
    extract/mux stages cut with ffmpeg `-t`, transcribe drops segments past the
    window, and all artifacts live in a separate `.tease` namespace. With `db`
    given, a tease creates/merges the title's voice cast after diarization (and
    publishes a `cast` event); full dubs read the saved cast into synthesize.
    """
    shared_work = media_work(config.work_dir, job)
    work = shared_work / job.target_lang
    job.artifacts_dir = work
    out = config.output_dir / shared_work.name / job.target_lang
    job.translation_options = dict(config["translate"])
    vb = (services or Services(config)).voicebox
    translator = build_translator(config["translate"]["provider"],
                                  config["translate"]["model"], voicebox_client=vb,
                                  endpoint=config["translate"].get("endpoint"))
    seg_limit = config["dub"].get("segment_limit")
    teaser_s = (int(config["dub"].get("teaser_minutes", 10)) * 60
                if job.kind == "tease" else None)

    log.info("=== Doblarr %s job: %s ===", job.kind, job.summary())

    cast_holder: dict = {"cast": None}

    def _ensure_cast():
        if db is None or dry_run:
            return None
        cast_holder["cast"] = ensure_cast(job, db, events=events)

    def _report(stage_name: str):
        if on_progress is None:
            return None
        return lambda done, total, detail: on_progress(
            stage_name, done / total if total else 0.0, detail)

    def _diarize():
        diarize.run(job, enabled=config["transcribe"]["diarize"], dry_run=dry_run)
        if not dry_run and job.segments:
            save_script(job, work)  # transcript + speakers survive a crash now

    def _translate():
        translate.run(job, translator, dry_run=dry_run, progress=_report("translate"))
        if not dry_run and job.segments:
            save_script(job, work)  # + translations

    steps = [
        ("probe", lambda: extract.run(job, shared_work, dry_run=dry_run,
                                      cancel=cancel_event, force=force,
                                      duration=teaser_s)),
        ("separate", lambda: separate.run(job, shared_work, model=config["separate"]["model"],
                                          dry_run=dry_run, force=force)),
        ("transcribe", lambda: transcribe.run(job, work, source=config["transcribe"]["source"],
                                              whisper_model=config["transcribe"]["whisper_model"],
                                              vb=vb, segment_limit=seg_limit,
                                              max_seconds=teaser_s, dry_run=dry_run,
                                              options={"diarize": config["transcribe"]["diarize"]},
                                              force=force)),
        ("diarize", _diarize),
        ("cast", _ensure_cast),
        ("translate", _translate),
        ("synthesize", lambda: synthesize.run(
            job, vb, work, voice_mode=config["dub"]["voice_mode"],
            dry_run=dry_run, cancel=cancel_event, force=force,
            cast={e["speaker_id"]: e for e in (cast_holder["cast"] or [])},
            progress=_report("synthesize"))),
        ("fit", lambda: fit_timing.run(job, work,
                                       enabled=config["dub"]["duration_match"],
                                       dry_run=dry_run, cancel=cancel_event,
                                       force=force)),
        ("mix", lambda: mix.run(job, work, ducking_ratio=config["dub"]["ducking_ratio"],
                                dry_run=dry_run, cancel=cancel_event, force=force)),
        ("mux", lambda: mux.run(job, out,
                                track_name_template=config["dub"]["track_name_template"],
                                dry_run=dry_run, cancel=cancel_event, force=force,
                                duration=teaser_s)),
    ]
    total = len(steps)
    report = RunReport(job, config.work_dir, dry_run)
    try:
        for i, (name, fn) in enumerate(steps):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled(f"cancelled before stage {name}")
            if on_stage:
                on_stage(name, i, total)
            with report.stage(name):
                fn()
    except JobCancelled:
        report.finish("cancelled")
        raise
    except BaseException:
        report.finish("failed")
        raise
    report.finish()

    log.info("=== done -> %s ===", job.output_file)
    return job
