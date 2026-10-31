"""Studio work that runs through the existing job queue.

Experiments, auditions and reference transcription are queued as ordinary
jobs with a studio `kind`, so they share the one worker, its cancellation,
its progress events and its restart behaviour. There is no second scheduler:
the worker hands a studio job here instead of to the dubbing pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..artifacts import read_json
from ..errors import DoblarrError, JobCancelled
from . import records

log = logging.getLogger("doblarr.studio.runner")

KINDS = ("studio_experiment", "studio_audition", "studio_transcribe", "studio_research")


def run(job, config, *, db, services, cancel, on_progress=None) -> str:
    """Run one studio job; returns the message the queue row should show."""
    task = dict(job.task or {})
    root = config.work_dir

    def progress(done, total, detail):
        if on_progress:
            on_progress(job.kind, done / max(1, total), detail)

    if job.kind == "studio_experiment":
        return _experiment(db, root, config, services, task, cancel, progress)
    if job.kind == "studio_audition":
        return _audition(db, root, config, services, task, cancel, progress)
    if job.kind == "studio_transcribe":
        return _transcribe(db, root, config, services, task, cancel, progress)
    if job.kind == "studio_research":
        return _research(db, config, task, cancel, progress)
    raise DoblarrError(f"unknown studio job kind {job.kind}")


def _research(db, config, task, cancel, progress) -> str:
    """One title-research question (doblarr.research.agent); costs provider calls."""
    from ..research import agent

    if cancel.is_set():
        raise JobCancelled("cancelled before the research started")
    steps = {"plan": 1, "search": 2, "fetch": 3, "synthesize": 4}

    def on_event(event) -> None:
        kind = str(getattr(event, "event_type", "") or "")
        stage = next((k for k in steps if kind.startswith(k)), None)
        if stage:
            progress(steps[stage], 5, getattr(event, "message", "") or stage)

    if task.get("action") == "scripts":
        from ..research import probe

        progress(1, 2, "looking for reference scripts")
        found = probe.probe_all(db, config, task["series_id"], list(task.get("sources") or []),
                                wiki=task.get("wiki") or "", lang=task.get("lang") or "",
                                languages=task.get("languages") or "en",
                                season=task.get("season"), episode=task.get("episode"))
        skipped = "; ".join(f"{k}: {v}" for k, v in found["skipped"].items())
        return f"kept {found['saved']} script(s)" + (f" · skipped {skipped}" if skipped else "")
    if task.get("depth"):
        config = config.with_overrides({"research.depth": task["depth"]})
    run = agent.research_title(db, task["series_id"], task["question"], config=config,
                               target_locale=task.get("target_locale") or "",
                               on_event=on_event)
    cited = sum(1 for s in run["sources"] if s["cited"])
    return (f"answered from {cited} cited source(s); {len(run['terms'])} term(s) and "
            f"{len(run['leads'])} cast lead(s) for review")


def translator_for(experiment: dict, services):
    from ..clients.translator import build_translator

    provider = experiment.get("provider") or "voicebox"
    client = services.speech if provider == "voicebox" else None
    options = ({"max_tokens": 2048, "temperature": experiment.get("temperature")}
               if provider == "voicebox" else {})
    return build_translator(provider, experiment.get("model") or "", voicebox_client=client,
                            endpoint=experiment.get("endpoint"), llm_options=options)


def _experiment(db, root, config, services, task, cancel, progress) -> str:
    from .experiments import run_condition

    experiment = records.get(db, "experiment", task["experiment"])
    if experiment is None:
        raise DoblarrError("that experiment no longer exists")
    conditions = [c for c in task.get("conditions") or experiment["conditions"]
                  if c in experiment["conditions"]]
    repeats = max(1, min(int(task.get("repeats") or experiment.get("repeats") or 1),
                         int(experiment.get("repeats") or 1)))
    results = []
    total = len(conditions) * repeats
    step = 0
    for repeat in range(1, repeats + 1):
        for condition in conditions:
            if cancel is not None and cancel.is_set():
                raise JobCancelled("cancelled between experiment conditions")
            translator = translator_for(experiment, services)
            variant = run_condition(
                db, root, experiment, condition, repeat, translator, cancel=cancel,
                progress=lambda d, t, detail, s=step: progress(s + d / max(1, t), total,
                                                               detail))
            step += 1
            results.append(f"{condition}{repeat}:{variant.get('status')}")
    return "experiment " + ", ".join(results)


def snapshot_for(store_job) -> dict:
    """The raw (unredacted) review snapshot of a finished run; server-side only."""
    if store_job is None or not store_job.review_file:
        raise DoblarrError("that run has no review snapshot yet; render a draft first")
    data = read_json(Path(store_job.review_file))
    if not data.get("segments"):
        raise DoblarrError("that run's review snapshot is missing or empty")
    return data


def _audition(db, root, config, services, task, cancel, progress) -> str:
    from ..jobs import JobStore
    from . import auditions

    audition = records.get(db, "audition", task["audition"])
    if audition is None:
        raise DoblarrError("that audition no longer exists")
    store = JobStore(db)
    snapshot = snapshot_for(store.get(audition["job_id"]))
    result = auditions.run(db, root, audition, snapshot, services.speech, cancel=cancel,
                           progress=progress,
                           pronunciations=dict(config["dub"].get("pronunciations") or {}))
    return f"audition {result.get('status')}"


def _transcribe(db, root, config, services, task, cancel, progress) -> str:
    """Timed text for a reference track, in bounded windows (no dub is made)."""
    from ..languages import base_language
    from .media import cut_audio
    from .sources import write_utterances

    reference = records.get(db, "reference", task["reference"])
    if reference is None:
        raise DoblarrError("that reference no longer exists")
    track = reference.get("track") or {}
    media = Path(track.get("media_path") or "")
    if not media.is_file():
        raise DoblarrError("the reference's media is not on disk")
    windows = [(float(a), float(b)) for a, b in task.get("windows") or []]
    if not windows:
        raise DoblarrError("transcription needs bounded windows")
    language = base_language(reference["language"])
    rows = []
    engine = task.get("engine") or "whisper"
    for n, (start, end) in enumerate(windows):
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during reference transcription")
        clip = cut_audio(media, start, end, root, stream=track.get("audio_index"), rate=16000,
                         cancel=cancel)
        for m, piece in enumerate(_recognize(clip, language, config, services, engine)):
            rows.append({"utt_id": f"{reference['id'][-6:]}-{n:03d}-{m:03d}",
                         "start": round(start + piece["start"], 3),
                         "end": round(start + piece["end"], 3),
                         "text": piece["text"], "speaker": ""})
        progress(n + 1, len(windows), f"window {n + 1}/{len(windows)}")
    work = Path(root) / "studio" / "references" / reference["scope"]
    folder = "evaluation" if "evaluation" in (reference.get("roles") or []) else "text"
    identity = write_utterances(work / folder / f"{reference['id']}.json", rows)
    text = {**(reference.get("text") or {}), **identity,
            "provenance": f"asr:{engine}", "uncertain": True,
            "kind": (reference.get("text") or {}).get("kind") or "dub_transcript",
            "windows": windows}
    records.update(db, "reference", reference["id"], {"text": text})
    return f"transcribed {len(rows)} utterance(s) in {len(windows)} window(s)"


def _recognize(clip: Path, language: str, config, services, engine: str) -> list[dict]:
    """Timed pieces of one window: faster-whisper when present, else the speech service."""
    if engine == "whisper":
        import importlib.util

        from ..hardware import resolve_device

        if importlib.util.find_spec("faster_whisper") is None:
            engine = "voicebox"
        else:
            from .. import whisper

            device = resolve_device("transcribe", dict(config.get("compute", {})))
            # Kept loaded between clips; the pool bounds how many models stay resident.
            with whisper.loaded(config["transcribe"].get("whisper_model", "large-v3"), device,
                                keep=True) as model:
                segments, _info = model.transcribe(str(clip), language=language,
                                                   vad_filter=True)
                return [{"start": s.start, "end": s.end, "text": s.text.strip()}
                        for s in segments if s.text.strip() and s.end > s.start]
    heard = services.speech.transcribe(clip, language=language) or {}
    text = str(heard.get("text") or "").strip()
    if not text:
        return []
    from .media import duration

    return [{"start": 0.0, "end": duration(clip), "text": text}]


