"""Orchestrates the dubbing pipeline end to end."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import (
    analysis,
    background,
    conversation,
    decisions,
    delivery,
    levels,
    onsets,
    phrases,
    prepass,
    reactions,
    retention,
    romaji,
    voice_models,
)
from .artifacts import digest, media_work
from .budget import RequestBudget
from .clients.translator import build_translator, translation_options
from .config import Config
from .cues import ensure_identity, validate_cues
from .errors import JobCancelled
from .ffmpeg import FFmpegError
from .hardware import gpu_stage, resolve_device
from .knowledge import KnowledgeSelection
from .knowledge import snapshot as freeze_knowledge
from .languages import base_language, display_name, resolve_target_locale
from .languages import parse as parse_language_tag
from .models import DubJob
from .presets import effective_config
from .review import apply_edits, policy_settings, write_review
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
from .voices import (
    assigned_cast,
    cast_key,
    character_cast,
    ensure_cast,
    merge_cast,
    save_characters,
)

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
    if job.kind == "analyze":
        # An analysis writes no media anyone could mistake for a dub, so the
        # dub's dry-run switch does not apply to it.
        dry_run = False
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
    unknown_source = (job.source_lang or "auto").lower() in ("auto", "und", "")
    if unknown_source and not dry_run and job.input_file and Path(job.input_file).is_file():
        # Resolved before the work folder is chosen: folders are keyed by the
        # source language, and an "auto" run must share the original's work.
        from . import original_language

        found = original_language.detect(original_language.probe(Path(job.input_file)))
        if found["lang"]:
            log.info("source language detected as %s: %s", found["lang"],
                     "; ".join(found["evidence"]))
            job.source_lang = found["lang"]
            job.metrics["source_detected"] = {k: found[k] for k in (
                "lang", "stream", "confidence", "evidence", "candidates")}
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
    vb = (services or Services(config)).speech
    # Frozen knowledge for this run: pinned to the job's snapshot revisions, so
    # edits made after queueing never change a resumed job. Without a db (CLI)
    # the legacy pronunciation map alone applies, exactly as before.
    spoken = dict(config["dub"].get("pronunciations", {}))
    if (config["dub"].get("romaji_names") and base_language(job.source_lang) == "ja"
            and base_language(job.target_lang) == "es"):
        # Explicit pronunciations win over the respelled glossary.
        spoken = {**romaji.respellings(config["translate"].get("glossary", {}).values()),
                  **spoken}
    knowledge = None
    if db is not None and not job.show_ref:
        # A job queued by hand carries no series; the file's recorded identity
        # still says which show it is, so the show's own terms apply.
        from . import identity

        media = identity.find_media_by_path(db, job.input_file) if job.input_file else None
        job.show_ref = identity.show_ref((media or {}).get("series_id"))
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
            legacy=spoken,
        )
    pronunciations = None if knowledge is not None else spoken
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
    # An aligned adaptation reference and the evaluation-only text this job
    # must never send (see doblarr.studio). The guard wraps the translator's
    # driver, so translation, retries, the prep pass and every shortening
    # repair are scanned by the same check before a request leaves.
    references = _load_references(config["translate"])
    holdout = _holdout_guard(config["translate"])
    if holdout is not None:
        from .studio.holdout import guard_translator

        if not dry_run:
            guard_translator(translator, holdout, "translation")
    seg_limit = config["dub"].get("segment_limit")
    teaser_s = int(config["dub"].get("teaser_minutes", 10)) * 60 if job.kind == "tease" else None
    # One allowance for every stage that may ask the provider for more audio —
    # quality retries, timing repairs and, later, extra candidate takes. 0 keeps
    # the historical behavior (counted, never capped).
    budget = RequestBudget(config["quality"].get("request_budget", 0), cancel_event)
    # Typed decisions about the script: rules first, the decision model only
    # where they are silent. Loaded on first use; without it the rules run alone.
    decision_options = decisions.settings(dict(config.get("decisions", {})))
    oracle = decisions.Oracle(decision_options, cache_dir=work)

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
        if not dry_run and job.segments and config["transcribe"]["source"] != "whisper":
            _subtitle_roles()

    def _subtitle_roles():
        """What each line is, from the subtitle track's styles: voice-over and
        preview lines are directed as such, and the songs are kept in the mix."""
        from . import subtitle_roles
        from .stages.common import script_path

        kept = subtitle_roles.ensure_styled(script_path(job, work), Path(job.input_file),
                                            job.script_lang or "")
        if kept is None:
            return
        found = subtitle_roles.events(kept)
        roles = subtitle_roles.annotate(
            [{"cue": s.cue_id, "start": s.start, "end": s.end} for s in job.segments], found)
        directed = subtitle_roles.direct(job.segments, roles)
        job.metrics["subtitle_roles"] = {
            "roles": {r: sum(1 for v in roles.values() if v["role"] == r)
                      for r in subtitle_roles.SPOKEN_ROLES},
            "directed": directed, "songs": subtitle_roles.song_spans(found)}

    def _ensure_cast():
        if db is None or dry_run:
            return None
        inherited = character_cast(job, db, character_group, character_map)
        ident = _identity_for_cast()
        if ident:
            # Characters an analysis identified bring their series casting
            # choice for this locale; an explicit title cast entry still wins.
            inherited = merge_cast(assigned_cast(job, db, ident.get("revision_id"),
                                                 ident.get("series_id")), inherited)
        cast_holder["cast"] = merge_cast(ensure_cast(job, db, events=events), inherited)

    def _identity_for_cast():
        from . import identity

        try:
            if job.input_file and Path(job.input_file).is_file():
                return identity.resolve(db, job.input_file, cache_dir=config.work_dir / "cache")
        except OSError:
            return None
        return None

    def _report(stage_name: str):
        if on_progress is None:
            return None
        return lambda done, total, detail: on_progress(
            stage_name, done / total if total else 0.0, detail
        )

    def _voices():
        picked = voice_models.resolve(voice_models.chosen(config), config)
        checks = dict(config.get("analysis", {}) or {})
        evaluation = set()
        if db is not None and not (config.get("speakers") or {}).get("evaluation_tracks", True):
            from . import speaker_memory

            evaluation = speaker_memory.evaluation_streams(db, job.input_file)
        return {"models": picked, "models_dir": voice_models.folder(config),
                "threshold": (config.get("speakers") or {}).get("threshold"),
                "tracks": (config.get("speakers") or {}).get("tracks", "all"),
                "verify": checks.get("verify_tracks", True),
                "min_correlation": checks.get("min_track_correlation", 0.45),
                "max_offset": checks.get("max_track_offset", 2.0),
                "evaluation_streams": evaluation}

    def _diarize():
        with gpu_stage("diarize", job, compute, retain=keep_models):
            diarize.run(job, enabled=config["transcribe"]["diarize"], dry_run=dry_run,
                        compute=compute,
                        method=config["transcribe"].get("diarizer", "auto"),
                        voices=_voices())
        if not dry_run and job.segments and prior_groups.get("segments"):
            _carry_names()
        if not dry_run and job.segments and job.kind == "analyze" and db is not None:
            from . import speaker_memory

            named = speaker_memory.name_from_dialogue(
                db, job.metrics.get("identity") or {},
                [{"speaker": seg.speaker, "start": seg.start, "end": seg.end,
                  "text": seg.text_src, "cue": seg.cue_id} for seg in job.segments])
            if named:
                job.metrics["named_by_dialogue"] = named
            from .stages.common import work_stem

            recognised = speaker_memory.name_from_memory(
                db, job.metrics.get("identity") or {},
                Path(shared_work) / f"{work_stem(job)}.speakers.json", job.source_lang or "")
            if recognised:
                job.metrics["named_by_memory"] = recognised
        if not dry_run and job.segments:
            prepare.run(job, enabled=config["transcribe"].get("clean_cues", True),
                        interjections=config["transcribe"].get(
                            "interjections_as_reactions", True))
            decisions.title_cards(job, oracle, decision_options)
            decisions.reactions(job, oracle, decision_options, interjections=config[
                "transcribe"].get("interjections_as_reactions", True))
            decisions.sound_tags(job, oracle, decision_options)
            save_script(job, work)  # transcript + speakers survive a crash now

    def _onsets():
        # Subtitle timing lags speech; start each cue where its voice starts.
        if dry_run or not job.segments or not timing_options.get("snap_onsets"):
            return
        onsets.snap(job)
        save_script(job, effective_work)

    def _translate():
        nonlocal references
        published = _published_dub(job, config, Path(shared_work), translator, dry_run)
        if published is not None:
            references = published
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
        knowledge_rows = _narrative().get("context") or None
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
            references=references,
            knowledge=knowledge_rows,
        )
        if not dry_run and job.segments:
            # How each line is delivered, where its own words say so; a
            # reviewer's edit applied afterwards still wins.
            decisions.delivery(job, oracle, decision_options)
            decisions.treatments(job, oracle, decision_options)
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
    narrative_state: dict = {}

    def _narrative() -> dict:
        """Accepted title knowledge for this run, from its frozen revision.

        Computed once. Translation gets the bounded claim list; acting gets each
        identified character's profile direction and accepted behaviour claims
        as their character layer, where the config does not already set one.
        """
        if narrative_state or db is None or dry_run or job.kind == "analyze":
            return narrative_state
        narrative_state["claims"] = []
        from . import identity, profiles, speaker_memory
        from .knowledge import narrative

        ident = None
        try:
            if job.input_file and Path(job.input_file).is_file():
                ident = identity.resolve(db, job.input_file, cache_dir=config.work_dir / "cache")
        except OSError:
            ident = None
        if ident is None:
            return narrative_state
        held = speaker_memory.held_out_revisions(db)
        claims = narrative.select(db, job.narrative_snapshot, media_id=ident["media_id"],
                                  held_out=held)
        narrative_state["claims"] = claims
        narrative_state["context"] = narrative.context(claims, db)
        labels = speaker_memory.characters_for_labels(db, ident["revision_id"], job.segments)
        layered = {}
        for label, character in labels.items():
            if character_notes.get(label):
                continue        # an explicit config note wins
            parts = [profiles.get(db, character["id"])["delivery"].get("direction") or "",
                     narrative.character_direction(claims, character["id"])]
            note = " ".join(p for p in parts if p).strip()
            if note:
                character_notes[label] = note[:500]
                layered[label] = character["name"]
        job.metrics["narrative"] = {
            "pin": job.narrative_snapshot or {}, "claims": len(claims),
            "context_rows": len(narrative_state["context"]),
            "characters_identified": len(labels), "direction_layers": layered}
        return narrative_state

    def _voicebox_sampling():
        return {k: config["voicebox"].get(k) for k in synthesize.SAMPLING_KEYS}

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
            borrow_voice=config["dub"].get("borrow_voice", True),
            sampling=_voicebox_sampling(),
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

    def _measure_all():
        # Every line's level against its speaker, whatever the levels mode:
        # an analysis exists to say how each line was performed.
        levels.measure_sources(job, {**level_options, "measure_source": True},
                               cancel=cancel_event, work_dir=work)
        if job.segments:
            save_script(job, effective_work)

    def _analyze():
        compute_device = resolve_device("transcribe", compute)
        analysis.run(job, work, whisper_model=config["transcribe"].get(
            "whisper_model", "large-v3"), device=compute_device)

    analysis_options = dict(config.get("analysis", {}) or {})

    def _identify():
        # What this file *is* (series, media, source revision), so everything
        # the analysis learns is keyed by content and not by a file name.
        if db is None or job.kind != "analyze":
            return
        from . import identity

        try:
            job.metrics["identity"] = identity.resolve(
                db, job.input_file, cache_dir=config.work_dir / "cache",
                hints={"tvdb_id": int(job.show_ref[7:])}
                if job.show_ref.startswith("series:") and job.show_ref[7:].isdigit() else None)
        except OSError as exc:
            log.warning("analysis: could not identify %s (%s); results stay keyed by the "
                        "script only", job.input_file.name, exc)
        _remember_groups()

    # The voice groups an earlier analysis of this revision had, read before
    # this run rewrites the script. Names are kept per group label, and a
    # fresh grouping numbers its groups anew: without this, SPEAKER_01's name
    # lands on whoever is SPEAKER_01 now.
    prior_groups: dict = {}

    def _remember_groups():
        from . import speaker_memory

        ident = job.metrics.get("identity") or {}
        if db is not None and ident.get("revision_id"):
            prior_groups["segments"] = speaker_memory.prior_segments(db, ident["revision_id"])

    def _carry_names():
        from . import speaker_memory

        carried = speaker_memory.carry_by_time(db, job.metrics.get("identity") or {},
                                               prior_groups["segments"], job.segments)
        if carried is not None:
            job.metrics["names_carried"] = carried

    force_features: dict[str, bool] = {"on": False}

    def _features():
        if not analysis_options.get("features", True) or not job.segments:
            analysis.record(db, job, "features", "skipped")
            return
        path, reused = analysis.run_features(job, work, cancel=cancel_event,
                                             progress=_report("features"),
                                             force=force or force_features["on"])
        job.metrics["features"] = {"path": str(path), "reused": reused}

    def _visual():
        wanted_visual = analysis_options.get("visual") or force_features.get("visual")
        if not wanted_visual or not job.segments:
            return
        from .vision import pipeline as vision_pipeline

        vision_pipeline.run(job, work, config, db=db, cancel=cancel_event,
                            progress=_report("visual"))

    def _emotion():
        """How each line is said, from the voice and the picture (doblarr.emotion)."""
        if not job.segments:
            return
        from . import emotion, identity, llm, speaker_memory

        ident = job.metrics.get("identity") or {}
        names: dict[str, str] = {}
        if db is not None and ident.get("revision_id") and ident.get("series_id"):
            spelled = {c["id"]: c["name"] for c in identity.characters(
                db, ident["series_id"], include_retired=True)}
            names = {label: spelled.get(cid, "") for label, cid in
                     speaker_memory.labels_to_characters(db, ident["revision_id"]).items()}
        model = str(analysis_options.get("emotion_model") or "ollama/qwen3-vl:8b")
        device = resolve_device("transcribe", compute).torch
        try:
            path, reused = emotion.run(job, work, model, names=names, device=device,
                                       cancel=cancel_event, progress=_report("emotion"))
        except llm.ModelUnavailable as exc:
            analysis.record(db, job, "emotion", "failed", error=str(exc))
            return
        job.metrics["emotion"] = {"path": str(path), "reused": reused}
        analysis.record(db, job, "emotion", "done", outputs={"emotion": str(path)},
                        version=emotion.READER)

    def _reader():
        """Who speaks each line as a strong model reads the script (evidence only)."""
        model = str(analysis_options.get("reader_model") or "")
        if not job.segments:
            return
        if not model:
            analysis.record(db, job, "reader", "unsupported",
                            error="no reader model is configured (analysis.reader_model)")
            return
        from . import dialogue_reader, identity, llm
        from .artifacts import read_json
        from .stages.common import work_stem

        ident = job.metrics.get("identity") or {}
        cast = [n for c in (identity.characters(db, ident["series_id"])
                            if db is not None and ident.get("series_id") else [])
                for n in [c["name"], *(c.get("aliases") or [])]]
        original = {r.get("cue"): r.get("original_text") or "" for r in read_json(
            Path(work) / f"{work_stem(job)}.analysis.json").get("lines") or []}
        try:
            path = dialogue_reader.run(job, work, model, cast, original=original,
                                       cancel=cancel_event, progress=_report("reader"))
        except (llm.ModelUnavailable, llm.InvalidReply) as exc:
            analysis.record(db, job, "reader", "failed", error=str(exc))
            return
        analysis.record(db, job, "reader", "done", outputs={"reader": str(path)},
                        version=dialogue_reader.READER)

    def _dub_text():
        """What the official dub in the target language says (doblarr.dub_text):
        the names and terms its translators chose, for a person to adopt."""
        from . import dub_text, speakers
        from .artifacts import read_json
        from .ffmpeg import FFmpegError
        from .stages.common import work_stem

        if not job.segments or not job.input_file or not job.input_file.is_file():
            return
        try:
            tracks = speakers.audio_tracks(job.input_file)
        except (OSError, ValueError, FFmpegError) as exc:
            analysis.record(db, job, "dub_text", "failed", error=str(exc))
            return
        track = dub_text.pick_track(tracks, job.target_locale or job.target_lang,
                                    job.source_lang or "")
        if track is None:
            analysis.record(db, job, "dub_text", "skipped",
                            error="no dub track in the target language")
            return
        stem = work_stem(job)
        evidence = {int(e["stream"]): e for e in read_json(
            Path(shared_work) / f"{stem}.speakers.json").get("track_evidence") or []}
        aligned = evidence.get(int(track["stream"])) or {}
        if aligned.get("state") not in ("verified", "unchecked"):
            analysis.record(db, job, "dub_text", "skipped",
                            error="the dub track is not verified to line up with the original")
            return
        found = speakers.other_tracks(job.input_file, job.source_lang, Path(shared_work), stem,
                                      [str(track["stream"])])
        if not found:
            analysis.record(db, job, "dub_text", "failed", error="the dub track could not be read")
            return
        try:
            path = dub_text.ensure(
                Path(shared_work), stem, track, found[0][1],
                offset=float(aligned.get("offset") or 0.0),
                rate=float(aligned.get("rate") or 1.0),
                model=config["transcribe"].get("whisper_model", "large-v3"),
                device=resolve_device("transcribe", compute))
        except Exception as exc:  # noqa: BLE001 - a proposal source; the analysis stands without it
            analysis.record(db, job, "dub_text", "failed", error=str(exc))
            return
        analysis.record(db, job, "dub_text", "done", outputs={"dub_text": str(path)},
                        version=dub_text.READER)

    def _knowledge():
        """Narrative extraction into a reviewable draft (never active knowledge)."""
        ident = job.metrics.get("identity") or {}
        model = str(analysis_options.get("knowledge_model") or "")
        if db is None or not ident.get("revision_id") or not job.segments:
            return
        if not model:
            analysis.record(db, job, "knowledge", "unsupported",
                            error="no knowledge extraction model is configured "
                                  "(analysis.knowledge_model)")
            return
        from . import llm
        from .knowledge import narrative
        from .stages.common import script_path

        client = llm.Client(model, endpoint=analysis_options.get("knowledge_endpoint"),
                            budget=budget, budget_kind="knowledge", guard=holdout)
        reference = None
        if analysis_options.get("use_reference_scripts"):
            import json as _json

            from .research import scripts as reference_scripts

            data = _json.loads(script_path(job, work).read_text(encoding="utf-8"))
            reference = reference_scripts.reference_for(
                db, ident, narrative.script_cues(data, {}))
        try:
            result = narrative.extract(db, ident, script_path(job, work), client,
                                       cancel=cancel_event, progress=_report("knowledge"),
                                       reference=reference)
        except (llm.ModelUnavailable, ValueError) as exc:
            analysis.record(db, job, "knowledge", "failed", error=str(exc))
            return
        job.metrics["knowledge"] = result
        analysis.record(db, job, "knowledge", "done" if result["state"] == "complete"
                        else "failed", version=narrative.EXTRACTOR,
                        metrics={k: result[k] for k in ("candidates", "windows", "reused",
                                                        "conflicts")},
                        outputs={"draft": result["draft_id"],
                                 "draft_revision": result["revision"]},
                        error="; ".join(result["failures"])[:400])

    def _speaker_memory():
        # Teach the series what this revision's identified voices sound like
        # and note the speaker baselines this grouping produced.
        if db is None:
            return
        from . import speaker_memory

        speaker_memory.after_analysis(db, job, work)

    adaptive_options = dict(config.get("adaptive", {}) or {})

    def _adaptive():
        """Envelope recommendations and selections (doblarr.adaptive): after
        timing, before the level owner. Reprocessing only, never speech."""
        if dry_run or not job.segments:
            return
        manual = bool(job.envelope_edits) or bool(adaptive_options.get("lines"))
        if adaptive_options.get("mode", "off") == "off" and not manual:
            return
        from . import adaptive, feedback, identity, speaker_memory

        ident = None
        if db is not None:
            try:
                if job.input_file and Path(job.input_file).is_file():
                    ident = identity.resolve(db, job.input_file,
                                             cache_dir=config.work_dir / "cache")
            except OSError:
                ident = None
        characters, visual, examples = {}, {}, []
        if ident:
            job.metrics.setdefault("identity", ident)
            characters = speaker_memory.characters_for_labels(db, ident["revision_id"],
                                                              job.segments)
            visual = _visual_by_cue(ident["revision_id"])
            examples = feedback.examples(db, ident["series_id"])
            job.metrics["held_out"] = sorted(feedback.held_out(db))
        job.metrics["adaptive"] = adaptive.recommend(
            job, config, db=db, work=work, cancel=cancel_event, progress=_report("adaptive"),
            budget=budget, guard=holdout, claims=_narrative().get("claims") or [],
            characters=characters, visual=visual, examples=examples)
        save_script(job, effective_work)

    def _visual_by_cue(revision_id: str) -> dict:
        """Who-speaks evidence an analysis left for this revision, by cue."""
        from .artifacts import read_json
        from .studio import records as studio_records

        for snap in studio_records.list_latest(db, "snapshot", scope=revision_id):
            found = ((snap.get("stages") or {}).get("association") or {}).get(
                "outputs", {}).get("visual")
            if found and Path(found).is_file():
                return {row["cue"]: row for row in read_json(Path(found)).get(
                    "associations") or []}
        return {}

    def _levels():
        # A reviewer's per-line gain is merged over the configured map here, so
        # the level owner sees one set of gains and a manual decision made in
        # review survives a resume without becoming a config edit.
        options = {**level_options,
                   "gains": {**dict(level_options.get("gains") or {}), **job.manual_gains}}
        if any(seg.envelope.active for seg in job.segments):
            from . import adaptive

            # Envelopes render inside this same pass, from the pinned catalogue.
            options.update(
                envelopes=adaptive_options.get("mode") == "apply"
                or bool(job.envelope_edits) or bool(adaptive_options.get("lines")),
                envelope_templates=adaptive.template_map(db, job),
                envelope_strength=adaptive_options.get("envelope_strength", 1.0),
                max_envelope_db=adaptive_options.get("max_envelope_db", 6.0))
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
            sampling=_voicebox_sampling(),
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
            accept_rewrite=decisions.rewrite_checker(job, oracle, decision_options),
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

    def _mux():
        if not dry_run and job.input_file.is_file():
            # The muxed copy is about the size of the input, and a saved version
            # copies it again; a full disk takes the database down with it.
            copies = 2 if config["dub"].get("preserve_versions", True) else 1
            retention.require_space(out, int(job.input_file.stat().st_size * 1.1 * copies))
        return mux.run(job, out, track_name_template=config["dub"]["track_name_template"],
                       dry_run=dry_run, cancel=cancel_event, force=force, duration=teaser_s,
                       audio_codec=config["dub"].get("output_codec", "aac"),
                       bitrate=config["dub"].get("output_bitrate", "192k"))

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

    def _bed_policy():
        """The rendered bed when a background policy is applied (else None:
        the legacy sidechain ducking runs exactly as before)."""
        policy = adaptive_options.get("background_policy")
        if dry_run or not policy or adaptive_options.get("mode") != "apply":
            return None
        from . import adaptive

        return adaptive.background(job, config, db=db, work=work, cancel=cancel_event)

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
            accept_rewrite=decisions.rewrite_checker(job, oracle, decision_options),
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
        ("onsets", _onsets),
        ("measure", _measure),
        ("synthesize", _synthesize),
        ("candidates", _candidates),
        ("quality", _quality),
        ("phrases", _phrases),
        ("fit", _fit),
        ("adaptive", _adaptive),
        ("levels", _levels),
        ("verify", _reverify),
        (
            "edges",
            lambda: boundaries.finish_edges(
                job,
                dict(config.get("boundaries", {})),
                cancel=cancel_event,
                dry_run=dry_run,
                endings=({} if dry_run else decisions.cutoffs(job, oracle, decision_options)),
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
                attack=config["dub"].get("duck_attack_ms", 100),
                release=config["dub"].get("duck_release_ms", 350),
                dry_run=dry_run,
                cancel=cancel_event,
                force=force,
                bed_policy=_bed_policy(),
                songs=(job.metrics.get("subtitle_roles") or {}).get("songs")
                if config["dub"].get("keep_songs", True) else None,
            ),
        ),
        (
            "mix_check",
            lambda: levels.check_mix(job, level_options, cancel=cancel_event,
                                     work_dir=work, dry_run=dry_run),
        ),
        ("background", _background),
        ("mux", _mux),
        ("validate", _validate),
    ]
    if job.kind == "analyze":
        # The first half of a dub and no more: who says what, and how. Each
        # stage is noted in the source revision's snapshot, so a page can show
        # what is done, stale, failed or unsupported, and a rerun can ask for
        # only the stages that need it (the earlier ones are checkpointed).
        keep = ("probe", "separate", "transcribe", "diarize")
        optional = [("analyze", _analyze), ("features", _features),
                    ("speaker_memory", _speaker_memory)]
        if analysis_options.get("visual"):
            optional.append(("visual", _visual))
        if analysis_options.get("knowledge"):
            optional.append(("knowledge", _knowledge))
        if analysis_options.get("emotion"):
            optional.append(("emotion", _emotion))
        if analysis_options.get("reader_model"):
            optional.append(("reader", _reader))
        if analysis_options.get("dub_text", True):
            optional.append(("dub_text", _dub_text))
        chosen = [str(x) for x in analysis_options.get("stages") or []]
        if chosen:
            # A targeted rerun: the earlier stages run from their checkpoints
            # (cheap), and only the asked-for optional stages run at all.
            visual = {"shots", "faces", "tracks", "active_speaker", "association", "scenes",
                      "visual"}
            wanted = {("visual" if c in visual else c) for c in chosen}
            available = {"analyze": _analyze, "features": _features,
                         "speaker_memory": _speaker_memory, "visual": _visual,
                         "knowledge": _knowledge, "emotion": _emotion, "reader": _reader,
                         "dub_text": _dub_text}
            optional = [(name, available[name]) for name in available if name in wanted]
            if "features" in wanted:
                force_features["on"] = True
            if "visual" in wanted:
                force_features["visual"] = True
        steps = ([step for step in steps if step[0] in keep] + [("measure", _measure_all)]
                 + optional)
        steps.insert(0, ("identify", _identify))
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
            if job.kind == "analyze" and name in _SNAPSHOT_STAGES:
                try:
                    with report.stage(name):
                        fn()
                except JobCancelled:
                    raise
                except Exception as exc:
                    analysis.record(db, job, _SNAPSHOT_STAGES[name][0], "failed",
                                    error=str(exc))
                    raise
                for stage_name in _SNAPSHOT_STAGES[name]:
                    if stage_name == "features" and not analysis_options.get("features",
                                                                             True):
                        continue
                    analysis.record(db, job, stage_name, metrics=_stage_metrics(job, name),
                                    outputs=_stage_outputs(job, stage_name, effective_work))
                continue
            with report.stage(name):
                fn()
        if not dry_run and job.kind != "analyze" and config["dub"].get(
                "preserve_versions", True):
            with report.stage("save_version"):
                # A candidate the export check rejected is kept on disk for
                # diagnosis, but it is not saved as a version: a saved version
                # is what "this render is a deliverable" means here, and a
                # truncated or mis-tagged track has not earned that word.
                if job.delivery.get("publishable", True):
                    saved = Path(preserve_version(job, config, cast=cast_holder["cast"])["output"])
                    retention.trim_versions(saved.parent.parent,
                                            int(config["dub"].get("versions_kept", 0)),
                                            current=saved.parent)
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
        oracle.close()
        if not dry_run and job.segments:
            # Best effort: a review that cannot be written must neither replace
            # the error a failed stage is raising nor fail a finished dub.
            try:
                ensure_identity(job)
                validate_cues(job.segments, job.cue_lineage)
                write_review(job, config.work_dir,
                             priorities=decisions.review_order(job, decision_options), settings={
                    # What this run actually used, not what is configured now.
                    **policy_settings(config),
                    "background": background.kind(job),
                    "delivery": delivery.describe(delivery.settings(delivery_options)),
                    "review_order": decisions.enabled(decision_options, "review_order"),
                })
            except Exception as exc:  # noqa: BLE001 - see above
                log.exception("could not write the review for %s", job.input_file)
                job.metrics["review_error"] = str(exc)
    report.finish()

    log.info("=== done -> %s ===", job.output_file)
    if holdout is not None:
        job.metrics["holdout_guard"] = holdout.summary()
    return job


# Analysis steps and the snapshot stages each one completes (doblarr.snapshots).
# The visual step records its own stages, one by one, as it goes.
_SNAPSHOT_STAGES: dict[str, tuple[str, ...]] = {
    "probe": ("probe",), "separate": ("separate",), "transcribe": ("transcribe",),
    "diarize": ("diarize",), "measure": ("measure", "baselines"), "analyze": ("analyze",),
    "features": ("features",), "speaker_memory": ("speaker_memory",),
}


def _stage_metrics(job: DubJob, step: str) -> dict:
    if step == "transcribe":
        return {"lines": len(job.segments)}
    if step == "diarize":
        return {"voices": len(job.speakers), **(job.metrics.get("diarize") or {})}
    if step == "measure":
        baseline = job.dialogue_baseline or {}
        return {"measured": sum(1 for s in job.segments if s.measurement.state == "measured"),
                "baseline": baseline.get("scope")}
    if step == "features":
        return dict(job.metrics.get("features") or {})
    return {}


def _stage_outputs(job: DubJob, stage: str, work: Path) -> dict:
    """Where a stage's result lives, so a reader finds it by revision, not by name."""
    from .stages.common import script_path, work_stem

    if stage == "transcribe":
        return {"script": str(script_path(job, work))}
    if stage == "separate":
        return {"vocals": str(job.vocals or ""), "background": str(job.background or "")}
    if stage == "diarize" and job.vocals:
        return {"speakers": str(Path(job.vocals).parent / f"{work_stem(job)}.speakers.json")}
    if stage == "analyze":
        return {"analysis": str(work / f"{work_stem(job)}.analysis.json")}
    if stage == "features":
        return {"features": str((job.metrics.get("features") or {}).get("path") or "")}
    return {}


def _published_dub(job, config, shared_work: Path, translator, dry_run: bool) -> dict | None:
    """Translate along the episode's own published dub, when it carries one.

    The voice grouping already extracted every dub track and verified its
    alignment (`<stem>.speakers.json`); a verified track in the target language
    is transcribed once (doblarr.dub_reference) and becomes this job's aligned
    reference. `translate.published_dub` picks how it is used: `follow` keeps
    the dub's names and phrasing where the source's meaning allows, `suggest`
    only borrows phrasing, `off` never reads it. An explicit
    `translate.reference_file` always wins.
    """
    import json

    from . import dub_reference
    from .artifacts import read_json
    from .stages.common import work_stem

    settings = config["translate"]
    policy = dub_reference.MODES.get(str(settings.get("published_dub", "follow") or "off"))
    if (policy is None or dry_run or not job.segments or settings.get("reference_file")
            or (job.script_is_target and not job.translation_options.get("adapt_region"))):
        return None
    stem = work_stem(job)
    evidence = read_json(shared_work / f"{stem}.speakers.json").get("track_evidence") or []
    track = dub_reference.published_track(evidence, job.target_lang)
    if track is None:
        return None
    lines = [{"index": s.index, "start": s.start, "end": s.end} for s in job.segments]
    try:
        found = dub_reference.ensure(shared_work, stem, track, lines, job.target_lang)
    except Exception as exc:  # noqa: BLE001 - a reference is help, never a requirement
        log.warning("published dub: could not read track %s (%s); translating without it",
                    track.get("stream"), exc)
        return None
    if found is None:
        return None
    ref, path = found
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("groups"):
        return None
    options = translation_options({**settings, "reference_policy": policy,
                                   "reference_file": str(path)})
    options["target_locale"] = job.target_locale
    if options != job.translation_options:
        # Translations made without the reference are not what it would say.
        for seg in job.segments:
            seg.text_translated = None
            seg.translation_provenance = {}
        job.translation_options = options
    translator.direction = {**getattr(translator, "direction", {}),
                            "reference_policy": policy, "reference_file": str(path)}
    heard = sum(1 for line in ref["lines"] if line["text"])
    job.metrics["published_dub"] = {"stream": ref["stream"], "title": track.get("title"),
                                    "policy": policy, "lines_heard": heard,
                                    "lines": len(ref["lines"])}
    log.info("published dub: following track %s (%s), %d/%d lines heard", ref["stream"],
             track.get("title"), heard, len(ref["lines"]))
    return payload


def _load_references(translate: dict) -> dict | None:
    """The studio's aligned reference for this job, when its policy uses one."""
    from .artifacts import read_json

    path = str(translate.get("reference_file") or "")
    if not path or translate.get("reference_policy") in (None, "", "original_only"):
        return None
    payload = read_json(Path(path))
    if not payload.get("groups"):
        raise ValueError(f"the aligned reference {Path(path).name} is missing or empty; "
                         "rebuild it in the studio or set the reference policy to "
                         "original only")
    return payload


def _holdout_guard(translate: dict):
    """A guard over every translation request, when evaluation-only text is declared."""
    files = [str(f) for f in (translate.get("holdout_files") or []) if f]
    if not files:
        return None
    from .studio.holdout import Fingerprints, HoldoutGuard
    from .studio.sources import read_utterances

    texts: list[str] = []
    for file in files:
        rows = read_utterances({"label": Path(file).name, "text": {"artifact": file}})
        texts.extend(r.get("text", "") for r in rows)
    return HoldoutGuard({"held-out adaptation": Fingerprints.of(texts)})
