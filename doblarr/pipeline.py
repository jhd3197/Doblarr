"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace

from .artifacts import media_work
from .clients.translator import build_translator
from .config import Config
from .errors import JobCancelled
from .models import DubJob
from .presets import effective_config
from .services import Services
from .stages import (
    diarize,
    extract,
    fit_timing,
    mix,
    mux,
    prepare,
    quality,
    separate,
    synthesize,
    transcribe,
    translate,
)
from .stages.common import save_script
from .telemetry import RunReport
from .voices import ensure_cast

log = logging.getLogger("doblarr.pipeline")


def run_job(
    job: DubJob,
    config: Config,
    dry_run: bool = False,
    on_stage=None,
    cancel_event: threading.Event | None = None,
    services: Services | None = None,
    force: bool = False,
    db=None,
    events=None,
    on_progress=None,
) -> DubJob:
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
    config = effective_config(config)
    shared_work = media_work(config.work_dir, job)
    work = shared_work / job.target_lang
    job.artifacts_dir = work
    out = config.output_dir / shared_work.name / job.target_lang
    job.translation_options = dict(config["translate"])
    vb = (services or Services(config)).voicebox
    translator = build_translator(
        config["translate"]["provider"],
        config["translate"]["model"],
        voicebox_client=vb,
        endpoint=config["translate"].get("endpoint"),
    )
    seg_limit = config["dub"].get("segment_limit")
    teaser_s = int(config["dub"].get("teaser_minutes", 10)) * 60 if job.kind == "tease" else None

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
            stage_name, done / total if total else 0.0, detail
        )

    def _diarize():
        diarize.run(job, enabled=config["transcribe"]["diarize"], dry_run=dry_run)
        if not dry_run and job.segments:
            prepare.run(job, enabled=config["transcribe"].get("clean_cues", True))
            save_script(job, work)  # transcript + speakers survive a crash now

    def _translate():
        translate.run(
            job,
            translator,
            dry_run=dry_run,
            progress=_report("translate"),
            batch_size=config["translate"].get("batch_size", 12),
            glossary=config["translate"].get("glossary", {}),
            chars_per_second=config["translate"].get("chars_per_second", 14),
            checkpoint=lambda: save_script(job, work),
            cancel=cancel_event,
        )
        if not dry_run and job.segments:
            save_script(job, work)  # + translations

    def _synthesize(segments=None):
        target = (
            job
            if segments is None
            else replace(
                job,
                segments=segments,
                speakers={s.speaker: job.speakers[s.speaker] for s in segments},
            )
        )
        synthesize.run(
            target,
            vb,
            work,
            voice_mode=config["dub"]["voice_mode"],
            dry_run=dry_run,
            cancel=cancel_event,
            force=force,
            cast={e["speaker_id"]: e for e in (cast_holder["cast"] or [])},
            progress=_report("synthesize") if segments is None else None,
            engine=config["voicebox"].get("default_engine"),
            concurrency=config["voicebox"].get("concurrency", 1),
            model_size=config["voicebox"].get("model_size"),
            seed=config["voicebox"].get("seed"),
            preset_voices=config["dub"].get("preset_voices", []),
            pronunciations=config["dub"].get("pronunciations", {}),
        )

    def _quality(segments=None, retry=True):
        target = job if segments is None else replace(job, segments=segments)
        options = dict(config.get("quality", {}))
        options["max_retries"] = options.get("max_retries", 1) if retry else 0
        quality.run(
            target,
            vb,
            dry_run=dry_run,
            cancel=cancel_event,
            regenerate=lambda seg: _synthesize([seg]),
            checkpoint=lambda: save_script(job, work),
            pronunciations=config["dub"].get("pronunciations", {}),
            **options,
        )

    def _regenerate(seg):
        _synthesize([seg])
        _quality([seg], retry=False)

    def _fit():
        fit_timing.run(
            job,
            work,
            enabled=config["dub"]["duration_match"],
            dry_run=dry_run,
            cancel=cancel_event,
            force=force,
            translator=translator,
            regenerate=_regenerate,
            checkpoint=lambda: save_script(job, work),
            max_attempts=config["dub"].get("max_fit_attempts", 2),
        )
        if not dry_run:
            save_script(job, work)

    steps: list[tuple[str, Callable[[], object]]] = [
        (
            "probe",
            lambda: extract.run(
                job,
                shared_work,
                dry_run=dry_run,
                cancel=cancel_event,
                force=force,
                duration=teaser_s,
            ),
        ),
        (
            "separate",
            lambda: separate.run(
                job, shared_work, model=config["separate"]["model"], dry_run=dry_run, force=force
            ),
        ),
        (
            "transcribe",
            lambda: transcribe.run(
                job,
                work,
                source=config["transcribe"]["source"],
                whisper_model=config["transcribe"]["whisper_model"],
                vb=vb,
                segment_limit=seg_limit,
                max_seconds=teaser_s,
                dry_run=dry_run,
                options=dict(config["transcribe"]),
                force=force,
            ),
        ),
        ("diarize", _diarize),
        ("cast", _ensure_cast),
        ("translate", _translate),
        ("synthesize", _synthesize),
        ("quality", _quality),
        ("fit", _fit),
        (
            "mix",
            lambda: mix.run(
                job,
                work,
                ducking_ratio=config["dub"]["ducking_ratio"],
                background_volume=config["dub"].get("background_volume", 1),
                fallback_volume=config["dub"].get("fallback_volume", 0.2),
                threshold=config["dub"].get("duck_threshold", 0.05),
                attack=config["dub"].get("duck_attack_ms", 30),
                release=config["dub"].get("duck_release_ms", 350),
                dry_run=dry_run,
                cancel=cancel_event,
                force=force,
            ),
        ),
        (
            "mux",
            lambda: mux.run(
                job,
                out,
                track_name_template=config["dub"]["track_name_template"],
                dry_run=dry_run,
                cancel=cancel_event,
                force=force,
                duration=teaser_s,
                audio_codec=config["dub"].get("output_codec", "aac"),
                bitrate=config["dub"].get("output_bitrate", "192k"),
            ),
        ),
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
