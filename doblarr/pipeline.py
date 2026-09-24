"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import (
    background,
    conversation,
    delivery,
    levels,
    phrases,
    prepass,
    reactions,
    treatments,
)
from .artifacts import digest, media_work
from .budget import RequestBudget
from .clients.translator import build_translator, translation_options
from .config import Config
from .cues import ensure_identity, validate_cues
from .errors import JobCancelled
from .ffmpeg import FFmpegError
from .hardware import gpu_stage
from .knowledge import KnowledgeSelection
from .knowledge import snapshot as freeze_knowledge
from .languages import base_language, display_name, resolve_target_locale
from .languages import parse as parse_language_tag
from .models import DubJob
from .presets import effective_config
from .review import apply_edits, write_review
from .services import Services
from .stages import (
    audition,
    boundaries,
    diarize,
    extract,
    fit_timing,
    mix,
    mux,
    phrase_timing,
    prepare,
    quality,
    separate,
    synthesize,
    transcribe,
    translate,
    treatment,
)
from .stages.common import load_script, save_script
from .telemetry import RunReport
from .versions import preserve_version
from .voices import cast_key, character_cast, ensure_cast, save_characters

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
    generation running if the best-effort remote cancellation fails. `services` supplies
    the voicebox client; one is built from config when not given (CLI path).
    `force` re-runs every stage, ignoring cached work-dir artifacts.

    A `kind="tease"` job only dubs the first `dub.teaser_minutes` minutes: the
    extract/mux stages cut with ffmpeg `-t`, transcribe drops segments past the
    window, and all artifacts live in a separate `.tease` namespace. With `db`
    given, a tease creates/merges the title's voice cast after diarization (and
    publishes a `cast` event); full dubs read the saved cast into synthesize.
    """
    config = effective_config(config)
    canonical = parse_language_tag(job.target_lang)
    if canonical is None:
        raise ValueError("target language must be a language code")
    job.target_lang = canonical
    if not job.target_locale:
        job.target_locale = resolve_target_locale(config.as_dict(), job.target_lang)
    elif parse_language_tag(job.target_locale) is None:
        raise ValueError("target locale must be a language tag")
    base = base_language(job.target_lang)
    if base != job.target_lang:
        # engines and media tags keep the base language; the locale holds the region
        job.target_lang = base
    shared_work = media_work(config.work_dir, job)
    locale_ns = job.target_locale or job.target_lang  # regional targets get their own namespace
    work = shared_work / locale_ns
    job.artifacts_dir = work
    out = config.output_dir / shared_work.name / locale_ns
    job.translation_options = translation_options(config["translate"])
    job.translation_options["target_locale"] = job.target_locale
    edits = config["dub"].get("line_edits", {})
    if edits or job.kind == "audition":
        # A prep pass that is off leaves the key what it was before it existed.
        translate_key = {k: v for k, v in dict(config["translate"]).items()
                         if k != "prepass" or v != "off"}
        effective_work = work / "effective" / digest([edits, translate_key, job.kind])[:16]
    else:
        effective_work = work
    character_group = config["dub"].get("cast_group", "")
    character_map = config["dub"].get("character_map", {})
    vb = (services or Services(config)).voicebox
    # Frozen knowledge for this run: pinned to the job's snapshot revisions, so
    # edits made after queueing never change a resumed job. Without a db (CLI)
    # the legacy pronunciation map alone applies, exactly as before.
    knowledge = None
    if db is not None:
        if job.knowledge_snapshot is None:
            job.knowledge_snapshot = freeze_knowledge(db)
        knowledge = KnowledgeSelection.load(
            db,
            snapshot=job.knowledge_snapshot,
            locale=job.target_locale or job.target_lang,
            title_ref=cast_key(path=str(job.input_file)),
            show_ref=character_group,
            show_refs=(job.show_ref,),
            legacy=dict(config["dub"].get("pronunciations", {})),
        )
    pronunciations = (
        None if knowledge is not None else dict(config["dub"].get("pronunciations", {}))
    )
    direction = dict(config["translate"])
    if base_language(job.target_locale) != job.target_locale:
        direction["locale"] = job.target_locale  # regional target wins over translate.locale
    translator = build_translator(
        config["translate"]["provider"],
        config["translate"]["model"],
        voicebox_client=vb,
        endpoint=config["translate"].get("endpoint"),
        direction=direction,
    )
    seg_limit = config["dub"].get("segment_limit")
    teaser_s = int(config["dub"].get("teaser_minutes", 10)) * 60 if job.kind == "tease" else None
    # One allowance for every stage that may ask the provider for more audio —
    # quality retries, timing repairs and, later, extra candidate takes. 0 keeps
    # the historical behavior (counted, never capped).
    budget = RequestBudget(config["quality"].get("request_budget", 0), cancel_event)

    log.info("=== Doblarr %s job: %s ===", job.kind, job.summary())

    cast_holder: dict = {"cast": None}
    # Which device each local model stage runs on (hardware.resolve_device).
    compute = dict(config.get("compute", {}))
    keep_models = bool(config["transcribe"].get("keep_models_loaded", False))

    def _separate():
        # Demucs runs in-process and voicebox synthesizes next on the same
        # GPU: its memory has to be handed back, not left in torch's cache.
        with gpu_stage("separate", job, compute):
            separate.run(job, shared_work, model=config["separate"]["model"],
                         dry_run=dry_run, force=force, compute=compute,
                         chunk_seconds=float(config["separate"].get("chunk_seconds", 600)),
                         overlap_seconds=float(config["separate"].get("overlap_seconds", 10)),
                         cancel=cancel_event)

    def _transcribe():
        with gpu_stage("transcribe", job, compute, retain=keep_models):
            transcribe.run(
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
                compute=compute,
            )

    def _ensure_cast():
        if db is None or dry_run:
            return None
        cast_holder["cast"] = ensure_cast(job, db, events=events)
        inherited = character_cast(job, db, character_group, character_map)
        assigned = {e["speaker_id"]: e for e in inherited}
        assigned.update({e["speaker_id"]: e for e in (cast_holder["cast"] or []) if e.get("voice")})
        cast_holder["cast"] = list(assigned.values())

    def _report(stage_name: str):
        if on_progress is None:
            return None
        return lambda done, total, detail: on_progress(
            stage_name, done / total if total else 0.0, detail
        )

    def _diarize():
        with gpu_stage("diarize", job, compute, retain=keep_models):
            diarize.run(job, enabled=config["transcribe"]["diarize"], dry_run=dry_run,
                        compute=compute)
        if not dry_run and job.segments:
            prepare.run(job, enabled=config["transcribe"].get("clean_cues", True),
                        interjections=config["transcribe"].get(
                            "interjections_as_reactions", True))
            save_script(job, work)  # transcript + speakers survive a crash now

    def _translate():
        translation_work = (
            (work / "audition-base" if edits else effective_work)
            if job.kind == "audition"
            else work
        )
        if job.kind == "audition" and not edits and not dry_run:
            load_script(job, translation_work, force)
        glossary = {
            # Resolved terminology relevant to these segments; the explicit
            # translate.glossary config always wins on a conflict.
            **(
                knowledge.glossary_terms([s.text_src for s in job.segments],
                                         job.script_lang or job.source_lang)
                if knowledge is not None
                else {}
            ),
            **config["translate"].get("glossary", {}),
        }
        reactions_on = bool(config["transcribe"].get("interjections_as_reactions", True))
        if hasattr(translator, "flag_reactions"):
            translator.flag_reactions = reactions_on
        synopsis = None
        if not dry_run and (not job.script_is_target
                            or job.translation_options.get("adapt_region")):
            synopsis = prepass.apply(job, prepass.analyze(
                job, translator, config["translate"].get("prepass", "off"),
                work_dir=work, budget=budget, cancel=cancel_event,
                corrections=config["transcribe"].get("source") == "whisper",
                title=job.input_file.stem), glossary)
        translate.run(
            job,
            translator,
            dry_run=dry_run,
            progress=_report("translate"),
            batch_size=config["translate"].get("batch_size", 12),
            glossary=glossary,
            chars_per_second=config["translate"].get("chars_per_second", 14),
            checkpoint=lambda: save_script(job, translation_work),
            cancel=cancel_event,
            memory_db=db,
            synopsis=synopsis,
            flag_reactions=reactions_on,
        )
        if not dry_run and job.segments:
            save_script(job, translation_work)  # + translations

    def _recent_effective_scripts():
        """Effective script caches for this media, newest first.

        Each distinct set of review edits gets its own directory so two
        concurrent edits cannot collide. That isolation would also throw away
        every take the previous round generated, so a new edit set starts from
        the newest existing one and applies its edits on top.
        """
        root = work / "effective"
        if not root.is_dir():
            return []
        found = []
        for directory in root.iterdir():
            if not directory.is_dir() or directory == effective_work:
                continue
            script = next(directory.glob("*.script.json"), None)
            if script is not None:
                found.append((script.stat().st_mtime, directory))
        return [directory for _stamp, directory in sorted(found, reverse=True)]

    def _edits():
        if dry_run or not edits:
            return
        if load_script(job, effective_work, force):
            return
        for previous in _recent_effective_scripts():
            # Takes, candidates and the current selection come forward; the
            # new edit set is applied over them below.
            if load_script(job, previous, force):
                break
        apply_edits(job, edits, lineage=job.cue_lineage)
        save_script(job, effective_work)

    # The locale layer of a line's delivery direction. Explicit config wins;
    # otherwise a regional target contributes its own accent guidance, which a
    # per-line direction can override without discarding the rest.
    locale_direction = str(config["dub"].get("locale_direction", "") or "")
    if not locale_direction and job.target_locale and (
            base_language(job.target_locale) != job.target_locale):
        locale_direction = f"speak in {display_name(job.target_locale)}"
    character_notes = dict(config["translate"].get("character_notes", {}) or {})
    candidate_requests = dict(config["dub"].get("candidates", {}) or {})

    def _narrator_speakers():
        return {
            label
            for label in job.speakers
            if len(job.speakers) == 1
            or label == "NARRATOR"
            or any(
                e["speaker_id"] == label and e.get("category") == "narrator"
                for e in (cast_holder["cast"] or [])
            )
        }

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
            pronunciations=pronunciations,
            knowledge=knowledge,
            narrator_voice=config["dub"].get("narrator_voice", ""),
            narrator_delivery=config["dub"].get("narrator_delivery", ""),
            narrator_speakers=_narrator_speakers(),
            locale_direction=locale_direction,
            character_notes=character_notes,
            clone_cleanup=config["dub"].get("clone_cleanup", False),
        )
        if db is not None and not dry_run and segments is None:
            save_characters(job, db, character_group, character_map)

    level_options = dict(config.get("levels", {}))
    owns_levels = levels.owns_processing(level_options)
    timing_options = dict(config.get("timing", {}))
    owns_phrases = phrases.owns_timing(timing_options)
    coverage_options = dict(config.get("coverage", {}))
    treatment_options = dict(config.get("treatments", {}))
    delivery_options = dict(config.get("delivery", {}))

    def _quality(segments=None, retry=True):
        target = job if segments is None else replace(job, segments=segments)
        options = dict(config.get("quality", {}))
        options.pop("request_budget", None)  # owned by the shared budget above
        sample = float(options.pop("asr_sample", 0.0) or 0.0)
        options["max_retries"] = options.get("max_retries", 1) if retry else 0
        quality.run(
            target,
            vb,
            dry_run=dry_run,
            cancel=cancel_event,
            regenerate=lambda seg: _synthesize([seg]),
            checkpoint=lambda: save_script(job, effective_work),
            pronunciations=pronunciations,
            budget=budget,
            boundary_options=dict(config.get("boundaries", {})),
            # Loudness has exactly one owner per run. With the post-fit owner
            # active the pre-fit loudnorm pass stands down, so a performance
            # gain can never be erased by a second normalization.
            own_levels=owns_levels,
            sample=sample,
            **options,
        )

    def _measure():
        levels.measure_sources(job, level_options, cancel=cancel_event, work_dir=work)
        if not dry_run and job.segments:
            save_script(job, effective_work)

    def _levels():
        # A reviewer's per-line gain is merged over the configured map here, so
        # the level owner sees one set of gains and a manual decision made in
        # review survives a resume without becoming a config edit.
        options = {**level_options,
                   "gains": {**dict(level_options.get("gains") or {}), **job.manual_gains}}
        levels.process(job, options, cancel=cancel_event, dry_run=dry_run)
        if not dry_run and job.segments:
            save_script(job, effective_work)

    def _candidates():
        """Extra takes for the cues review asked to hear alternatives for."""
        if dry_run or not candidate_requests or not job.segments:
            return
        synthesize.candidates(
            job, vb, work, candidate_requests,
            cast={e["speaker_id"]: e for e in (cast_holder["cast"] or [])},
            engine=config["voicebox"].get("default_engine"),
            model_size=config["voicebox"].get("model_size"),
            seed=config["voicebox"].get("seed"),
            budget=budget, cancel=cancel_event,
            limit=int(config["dub"].get("candidate_limit", 4)),
            pronunciations=pronunciations,
            locale_direction=locale_direction,
            character_notes=character_notes,
            narrator_delivery=config["dub"].get("narrator_delivery", ""),
            narrator_speakers=_narrator_speakers(),
        )
        save_script(job, effective_work)

    def _reverify():
        """Re-check words on audio that timing or levels actually changed.

        Time-stretching and gain are exactly the processing that can introduce
        an artifact a recognizer will hear, so evidence gathered on the raw
        take is not automatically still valid. Unchanged cues reuse it.
        """
        options = dict(config.get("quality", {}))
        if dry_run or not options.get("enabled", True):
            return
        policy = options.get("asr", "off")
        if policy == "off" or not job.segments:
            return
        rechecked = 0
        for seg in job.segments:
            current = seg.audio.current()
            if current is None or not current.exists():
                continue
            expected = seg.verification.expected or ""
            if not expected or not seg.verification.checked:
                continue
            result, reused = quality.verify_clip(
                seg, Path(current.path), job.target_lang, vb, policy,
                "re-checked after timing and level processing", expected,
                budget=budget, audio_fingerprint=current.fingerprint,
                target=current.role)
            seg.verification = result
            quality.verification_findings(seg)
            seg.issues = [i for i in seg.issues if i not in quality._VERIFY_ISSUES]
            seg.issues += quality.legacy_issues(result)
            rechecked += 0 if reused else 1
        job.metrics["verification_rechecked"] = rechecked
        job.metrics["verification"] = quality.coverage(job)
        save_script(job, effective_work)

    def _regenerate(seg):
        _synthesize([seg])
        _quality([seg], retry=False)

    def _phrases():
        # A reviewer's anchors and protected pauses are merged over the
        # configured map here, exactly as the manual gains are, so the planner
        # sees one set and a decision made in review survives a resume.
        options = {**timing_options,
                   "phrases": {**dict(timing_options.get("phrases") or {}),
                               **job.timing_edits}}
        phrase_timing.run(
            job,
            work,
            options=options,
            dry_run=dry_run,
            cancel=cancel_event,
            force=force,
            translator=translator,
            regenerate=_regenerate,
            checkpoint=lambda: save_script(job, effective_work),
            max_attempts=config["dub"].get("max_fit_attempts", 2),
            budget=budget,
        )
        if not dry_run:
            save_script(job, effective_work)

    def _conversation():
        options = {**timing_options,
                   "phrases": {**dict(timing_options.get("phrases") or {}),
                               **job.timing_edits}}
        conversation.check(job, options, cancel=cancel_event, dry_run=dry_run)
        if not dry_run and job.segments:
            save_script(job, effective_work)

    def _coverage():
        reactions.process(job, coverage_options, cancel=cancel_event, dry_run=dry_run,
                          vb=vb, budget=budget,
                          engine=config["voicebox"].get("default_engine", ""),
                          work_dir=work)
        if not dry_run and job.segments:
            save_script(job, effective_work)

    def _background():
        background.check(job, coverage_options, cancel=cancel_event, work_dir=work,
                         vb=vb, budget=budget, dry_run=dry_run)

    def _treatments():
        # A reviewer's per-line preset is merged over the configured map here,
        # exactly as the manual gains and timing anchors are, so a decision
        # made in review survives a resume without becoming a config edit.
        options = {**treatment_options,
                   "lines": {**dict(treatment_options.get("lines") or {}),
                             **job.treatment_edits}}
        treatment.run(job, options=options, cancel=cancel_event, dry_run=dry_run,
                      work_dir=work, program_seconds=_program_seconds())
        if not dry_run and job.segments:
            save_script(job, effective_work)

    program = {"seconds": None}

    def _program_seconds():
        """How long the delivered programme is, so a tail can be told it ran past it."""
        if program["seconds"] is None:
            track = job.source_audio or job.background
            try:
                program["seconds"] = (fit_timing._duration(track, cancel=cancel_event)
                                      if track else 0.0)
            except (OSError, ValueError, FFmpegError):
                program["seconds"] = 0.0
        return program["seconds"] or None

    def _validate():
        if job.kind == "audition":
            # An audition is a montage for casting, not a delivery. Checking it
            # against a programme's expectations would report a truth about a
            # file nobody is delivering.
            job.delivery = {"state": "skipped", "reason":
                            "an audition montage is not a delivered programme"}
            return
        delivery.validate(job, delivery_options, cancel=cancel_event, work_dir=work)

    def _fit():
        if owns_phrases:
            # Two timing owners for one line would compound into a warble; the
            # phrase owner has already produced this run's timed derivative.
            if not dry_run:
                fit_timing.stand_down(job, "phrase timing owns this run")
            return
        fit_timing.run(
            job,
            work,
            enabled=config["dub"]["duration_match"],
            dry_run=dry_run,
            cancel=cancel_event,
            force=force,
            translator=translator,
            regenerate=_regenerate,
            checkpoint=lambda: save_script(job, effective_work),
            max_attempts=config["dub"].get("max_fit_attempts", 2),
            budget=budget,
            options=timing_options,
        )
        if not dry_run:
            save_script(job, effective_work)

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
        ("separate", _separate),
        ("transcribe", _transcribe),
        ("diarize", _diarize),
        ("cast", _ensure_cast),
        ("translate", _translate),
        ("edits", _edits),
        ("measure", _measure),
        ("synthesize", _synthesize),
        ("candidates", _candidates),
        ("quality", _quality),
        ("phrases", _phrases),
        ("fit", _fit),
        ("levels", _levels),
        ("verify", _reverify),
        (
            "edges",
            lambda: boundaries.finish_edges(
                job,
                dict(config.get("boundaries", {})),
                cancel=cancel_event,
                dry_run=dry_run,
            ),
        ),
        ("treatments", _treatments),
        ("conversation", _conversation),
        ("coverage", _coverage),
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
            "mix_check",
            lambda: levels.check_mix(job, level_options, cancel=cancel_event,
                                     work_dir=work, dry_run=dry_run),
        ),
        ("background", _background),
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
        ("validate", _validate),
    ]
    if job.kind == "audition":
        separation = next(step for step in steps if step[0] == "separate")
        steps = [step for step in steps if step[0] != "separate"]
        position = next(i for i, step in enumerate(steps) if step[0] == "diarize") + 1
        steps[position:position] = [
            (
                "select_audition",
                lambda: audition.run(
                    job,
                    shared_work,
                    count=config["dub"].get("audition_lines", 8),
                    cancel=cancel_event,
                    force=force,
                    dry_run=dry_run,
                ),
            ),
            separation,
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
        if not dry_run and config["dub"].get("preserve_versions", True):
            with report.stage("save_version"):
                # A candidate the export check rejected is kept on disk for
                # diagnosis, but it is not saved as a version: a saved version
                # is what "this render is a deliverable" means here, and a
                # truncated or mis-tagged track has not earned that word.
                if job.delivery.get("publishable", True):
                    preserve_version(job, config, cast=cast_holder["cast"])
                else:
                    log.warning(
                        "not saving a version: export validation failed (%s). The "
                        "rendered file is left in place for diagnosis.",
                        job.delivery.get("summary") or "see the delivery report")
    except JobCancelled:
        job.metrics["request_budget"] = budget.snapshot()
        report.finish("cancelled")
        raise
    except BaseException:
        report.finish("failed")
        raise
    finally:
        job.metrics["request_budget"] = budget.snapshot()
        if not dry_run and job.segments:
            ensure_identity(job)
            validate_cues(job.segments, job.cue_lineage)
            write_review(job, config.work_dir, settings={
                # What this run actually used, not what is configured now.
                "levels": {k: level_options.get(k, default) for k, default in (
                    ("mode", "legacy"), ("target_db", -20.0), ("strength", 0.7),
                    ("max_boost_db", 4.0), ("max_cut_db", 8.0))},
                "verification_policy": config["quality"].get("asr", "off"),
                "candidate_limit": int(config["dub"].get("candidate_limit", 4)),
                "clone_cleanup": bool(config["dub"].get("clone_cleanup", False)),
                # Phrase timing and coverage policy, frozen the same way: an old
                # review must show the policy that produced it, not today's.
                "timing": {k: timing_options.get(k, default) for k, default in (
                    ("mode", "whole"), ("pacing", "speaker"),
                    ("max_stretch", 1.3), ("min_stretch", 1.0),
                    ("protect_pause", 0.45), ("anchor_tolerance", 0.12),
                    ("repair", True))},
                "coverage": {k: coverage_options.get(k, default) for k, default in (
                    ("mode", "off"), ("gain_db", 0.0), ("handle_ms", 80),
                    ("fade_ms", 25), ("max_seconds", 4.0),
                    ("leakage_check", False), ("generate", False))},
                "background": background.kind(job),
                # Plan 05. Which acoustic space this run put around the lines,
                # and what the delivered file was graded against. Frozen the
                # same way: an old review shows the profile that produced it.
                "treatments": {
                    **{k: treatment_options.get(k, default) for k, default in (
                        ("mode", "off"), ("default", "dry"), ("intensity", 1.0))},
                    "scenes": treatments.settings(treatment_options)["scenes"],
                    "catalogue": treatments.catalogue(),
                },
                "delivery": delivery.describe(delivery.settings(delivery_options)),
            })
    report.finish()

    log.info("=== done -> %s ===", job.output_file)
    return job
