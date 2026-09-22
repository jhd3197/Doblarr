"""Stage 6 — synthesize dubbed audio per segment via voicebox.

Each speaker has a separately verified profile and reference. Individual clips
are resumable and keyed by their effective generation request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ..artifacts import digest, matches, read_json, record, stamp
from ..clients.voicebox import GenerationFailed, VoiceboxClient
from ..cues import (
    RAW,
    Artifact,
    Selection,
    Take,
    now,
    take_id,
)
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..fingerprints import generation as generation_fingerprint
from ..models import DubJob, Speaker
from ..performance import compose, requested_direction
from ..telemetry import write_json
from .common import Plan, dry, stage, work_stem
from .quality import spoken_form

log = logging.getLogger("doblarr.synthesize")


def _extract_ref(
    source_audio: Path, start: float, end: float, dest: Path, cancel: threading.Event | None = None
) -> Path:
    """Pull a normalized mono reference clip from the original audio."""
    dur = min(15.0, end - start)
    if dur <= 0:
        raise ValueError("voice reference must have positive duration")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-y",
            "-ss",
            str(start),
            "-i",
            str(source_audio),
            "-t",
            str(dur),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            "loudnorm=I=-14",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        cancel=cancel,
    )
    return dest


_MAX_REF_CHARS = 300


def _bad_ref_text(text: str) -> bool:
    """Detect a hallucinated reference transcript (whisper repetition loop).

    A runaway "このように、このように、…" style transcript wedges the TTS
    engine server-side (observed: generations never leave 'generating').
    """
    if len(text) > _MAX_REF_CHARS:
        return True
    words = text.split()
    if len(words) >= 12 and len(set(words)) / len(words) < 0.3:
        return True
    # character-level loop for languages without spaces (ja/zh)
    if " " not in text and len(text) >= 40:
        chunk = text[:10]
        if chunk and text.count(chunk) > 3:
            return True
    return False


# A restrained cleanup for a clone reference: a high-pass below the voice and a
# conservative denoise. Deliberately not an EQ match, a de-esser or a
# restoration chain — the point is to stop a hum or a hiss being cloned along
# with the voice, not to redesign it.
CLEANUP = "reference-cleanup/1"
CLEANUP_FILTER = "highpass=f=70,afftdn=nr=8:nf=-30"


def reference_score(seg, job) -> tuple:
    """How good a clone reference this cue is, most important criterion first.

    Ranked on evidence, not on length alone: a long line with another actor
    talking across it teaches the clone two voices. Where the source
    measurement ran, its overlap and contamination flags are used directly;
    where it did not, the timeline overlap check still applies.
    """
    measurement = seg.measurement
    overlapped = measurement.overlapped or any(
        t.speaker != seg.speaker and t.start < seg.end and t.end > seg.start
        for t in job.segments)
    contaminated = measurement.contaminated
    measured = measurement.measured_seconds or seg.duration
    return (
        not overlapped,                       # isolated speech first
        not contaminated,                     # then a clean reference
        4.0 <= measured <= 15.0,              # then a usable length
        -abs(measured - 8.0),                 # then closest to eight seconds
    )


def clean_reference(source: Path, dest: Path, cancel=None) -> Path:
    """A cleaned copy of a reference sample. The original is never replaced.

    Fingerprinted separately from the sample it came from, so turning cleanup
    on or off changes the profile key and therefore invalidates the profile and
    everything generated from it — which is correct, because it is a different
    voice reference.
    """
    request = {"source": stamp(source), "filter": CLEANUP_FILTER, "version": 1}
    if matches([dest], request):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    run_ffmpeg(["-y", "-i", str(source), "-af", CLEANUP_FILTER, "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
    temp.replace(dest)
    record([dest], request)
    return dest


def _resolve_profile(job, spk, vb, clips_dir, voice_mode, cast, cancel, cleanup=False):
    assigned = cast.get(spk.label, {}).get("voice")
    if assigned:
        spk.voicebox_profile_id = assigned
    if spk.voicebox_profile_id:
        # A preset voice bypasses clone preparation entirely: no reference is
        # extracted, nothing is cleaned, and no profile is created.
        return
    if voice_mode != "clone":
        raise ValueError(f"assign a preset voice to {spk.label} before synthesis")
    candidates = [s for s in job.segments if s.speaker == spk.label and s.duration >= 1.5]
    candidates.sort(key=lambda s: reference_score(s, job), reverse=True)
    # An overlapped or contaminated cue is a last resort, not a silent choice.
    usable = [s for s in candidates if reference_score(s, job)[0]]
    candidates = usable or candidates
    source = job.vocals if job.vocals and job.vocals.exists() else job.source_audio
    key = digest(
        {
            "source": stamp(source),
            "speaker": spk.label,
            "language": job.target_lang,
            "source_language": job.source_lang,
            "script_language": job.script_lang,
            # The cleanup revision is part of the profile's identity: a cleaned
            # sample is a different reference, and a profile built from the
            # other one must not be reused for it.
            "cleanup": CLEANUP if cleanup else "",
            "references": [(s.start, s.end, s.text_src) for s in candidates[:5]],
        }
    )
    profile_receipt = clips_dir / f"profile-{key[:16]}.json"
    saved = read_json(profile_receipt)
    if saved.get("id") and any(v["id"] == saved["id"] for v in vb.list_voices()):
        spk.voicebox_profile_id = saved["id"]
        kept = saved.get("reference_clip")
        if kept and Path(kept).is_file():
            spk.reference_clip = Path(kept)
        return
    # Do not reuse name-only profiles: creation may have crashed before a sample was added.
    for candidate in candidates[:5]:
        original = _extract_ref(
            source, candidate.start, candidate.end, clips_dir / f"reference-{key[:16]}.wav", cancel
        )
        ref = original
        if cleanup:
            ref = clean_reference(
                original, clips_dir / f"reference-{key[:16]}.clean.wav", cancel)
        text = candidate.text_src.strip()
        if job.script_lang != job.source_lang or candidate.duration > 15:
            text = vb.transcribe(ref, language=job.source_lang).get("text", "").strip()
        if not text or _bad_ref_text(text):
            continue
        pid = vb.create_profile(name=f"{job.input_file.stem}-{key[:16]}", language=job.target_lang)
        vb.add_sample(pid, ref, text)
        write_json(profile_receipt, {
            "id": pid, "reference": key,
            # The original sample is kept whether or not cleanup ran, so the
            # choice is reversible and the source evidence is never overwritten.
            "original_clip": str(original), "reference_clip": str(ref),
            "cleanup": CLEANUP if cleanup else "",
            "cue": candidate.cue_id, "seconds": round(candidate.duration, 3),
            "overlapped": candidate.measurement.overlapped,
            "contaminated": candidate.measurement.contaminated,
        })
        spk.reference_clip = ref
        spk.voicebox_profile_id = pid
        job.metrics.setdefault("clone_references", {})[spk.label] = {
            "cue": candidate.cue_id, "seconds": round(candidate.duration, 3),
            "cleanup": bool(cleanup), "clip": str(ref), "original": str(original)}
        return
    raise RuntimeError(f"no clean single-speaker reference for {spk.label}; assign a preset voice")


def _register_take(seg, signature: dict, dest: Path, state: str, *, origin: str = "auto",
                   attempt: int = 0, select: bool = True) -> Take:
    """Record the raw generation for this cue and select it.

    The raw file is the one immutable input every later stage reprocesses from;
    normalization and timing register their own derivatives instead of
    overwriting it. Selecting a different take invalidates those derivatives so
    a resume cannot mix a new take with an old cue's processed audio.
    """
    fingerprint = generation_fingerprint(signature)
    identifier = take_id(fingerprint, attempt)
    take = seg.audio.take(identifier)
    raw = Artifact(
        role=RAW,
        path=str(dest),
        fingerprint=fingerprint,
        duration=None,
        bytes=dest.stat().st_size if dest.exists() else None,
    )
    if take is None:
        take = Take(
            take_id=identifier,
            fingerprint=fingerprint,
            engine=signature.get("engine") or "",
            model=signature.get("model_size"),
            profile=signature.get("profile"),
            voice_revision=str(signature.get("revision") or ""),
            text=signature.get("text") or "",
            direction=signature.get("delivery") or "",
            seed=signature.get("seed"),
            line_revision=int(signature.get("line_revision") or 0),
            created_at=now(),
            origin=origin,
            attempt=attempt,
            intent=seg.intent,
        )
        seg.audio.takes.append(take)
    take.state = state
    take.raw = raw
    if not select:
        # A candidate is generated *next to* the selection, never over it: a
        # reviewer asked to hear an alternative, not to have one chosen.
        return take
    previous = seg.audio.selection.take_id if seg.audio.selection else None
    if previous != identifier:
        seg.audio.selection = Selection(take_id=identifier, reason="auto",
                                        previous=previous, at=now())
        # Every derivative belonged to the previous take; the raw takes stay.
        seg.audio.invalidate_after(RAW)
    return take


def reused_selection(seg) -> bool:
    """Whether this cue already has the take a person chose, on disk.

    A reviewed selection is a decision, not a cache entry: regenerating over it
    would silently replace the take someone listened to and picked. Changing
    the wording or the direction clears the selection (see `review.apply_edits`),
    which is what lets a real edit produce new audio.
    """
    selection = seg.audio.selection
    if selection is None or selection.reason not in ("review", "restored", "candidate"):
        return False
    take = seg.audio.take(selection.take_id)
    if take is None or take.raw is None or not take.raw.exists():
        return False
    seg.audio_clip = Path(take.raw.path)
    return True


def candidates(job, vb, work_dir: Path, requests: dict, *, cast=None, engine=None,
               model_size=None, seed=None, budget=None, cancel=None, limit: int = 4,
               knowledge=None, pronunciations=None, locale_direction="",
               character_notes=None, narrator_delivery="", narrator_speakers=None) -> int:
    """Generate extra takes for the cues a reviewer asked to hear alternatives for.

    Bounded three ways: `limit` per cue, the shared request budget, and only
    for cues actually named in `requests`. Existing candidate takes whose audio
    is on disk are adopted rather than regenerated, so generate -> interrupt ->
    resume costs nothing the first run already paid for.

    The selection is never touched. Candidates sit next to the current take
    until a person chooses one.
    """
    if not requests:
        return 0
    clips_dir = _clips_dir(job, work_dir)
    cast = dict(cast or {})
    made = 0
    for seg in job.segments:
        wanted = requests.get(seg.cue_id, requests.get(str(seg.index), 0))
        try:
            wanted = max(0, min(int(limit), int(wanted)))
        except (TypeError, ValueError):
            continue
        if not wanted:
            continue
        spk = job.speakers.get(seg.speaker)
        voice_engine = cast.get(seg.speaker, {}).get("engine") or engine
        seg.intent = compose(
            seg, engine=voice_engine or "", client=vb,
            cast_delivery=cast.get(seg.speaker, {}).get("delivery", ""),
            locale_direction=locale_direction,
            character_note=(character_notes or {}).get(seg.speaker, ""),
            narrator_delivery=(narrator_delivery
                               if (narrator_speakers is not None
                                   and seg.speaker in narrator_speakers) else ""))
        delivery = requested_direction(seg.intent)
        text = seg.tts_text or spoken_form(seg.text_translated or seg.text_src,
                                           pronunciations or {})
        for attempt in range(1, wanted + 1):
            if cancel is not None and cancel.is_set():
                raise JobCancelled("cancelled during candidate generation")
            signature = {
                "text": text,
                "language": job.target_lang,
                "profile": seg.voice or (spk.voicebox_profile_id if spk else None),
                "engine": voice_engine,
                "model_size": model_size,
                # A seed is the only honest way to ask for a *different* take
                # from an engine that supports one. Where it does not, the
                # candidate is still a distinct take with its own identity even
                # if the backend happens to return identical audio.
                "seed": (seed + attempt) if seed is not None else None,
                "delivery": delivery,
                "line_revision": seg.revision,
                "candidate": attempt,
                "revision": cast.get(seg.speaker, {}).get("revision", ""),
            }
            dest = clips_dir / f"line_{seg.index:04d}.candidate{attempt}.wav"
            existing = seg.audio.take(take_id(generation_fingerprint(signature), attempt))
            if existing is not None and existing.raw is not None and existing.raw.exists():
                job.metrics["candidate_reused"] = job.metrics.get("candidate_reused", 0) + 1
                continue
            if budget is not None and not budget.charge("candidate"):
                job.metrics["candidates_refused"] = (
                    job.metrics.get("candidates_refused", 0) + 1)
                break
            kwargs = {"engine": voice_engine} if voice_engine else {}
            if model_size:
                kwargs["model_size"] = model_size
            if signature["seed"] is not None:
                kwargs["seed"] = signature["seed"]
            if delivery:
                kwargs["instruct"] = delivery
            take = None
            try:
                vb.synthesize_to_file(signature["profile"], text, job.target_lang, dest,
                                      cancel_event=cancel, **kwargs)
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed candidate is evidence
                take = _register_take(seg, signature, dest, "failed", origin="candidate",
                                      attempt=attempt, select=False)
                take.error = str(exc)
                job.metrics["candidates_failed"] = (
                    job.metrics.get("candidates_failed", 0) + 1)
                continue
            take = _register_take(seg, signature, dest, "generated", origin="candidate",
                                  attempt=attempt, select=False)
            take.checks = _candidate_checks(dest, seg)
            made += 1
    job.metrics["candidates_generated"] = job.metrics.get("candidates_generated", 0) + made
    return made


def _candidate_checks(path: Path, seg) -> dict:
    """Technical measurements for one candidate.

    These explain a defect — silence, clipping, a length that cannot fit — and
    nothing more. There is no score here that ranks one performance above
    another, because no number in this file knows what good acting sounds like.
    """
    from .quality import inspect_pcm

    try:
        stats = inspect_pcm(Path(path))
    except Exception as exc:  # noqa: BLE001 - an unreadable candidate is a finding
        return {"state": "unreadable", "reason": str(exc)}
    defects = []
    if stats["rms_db"] < -55:
        defects.append("silence")
    if stats["clipped_fraction"] > 0.001:
        defects.append("clipping")
    if seg.duration and stats["duration"] > max(2.0, seg.duration * 2):
        defects.append("unexpected_duration")
    return {"state": "usable" if not defects else "defective", "defects": defects,
            "duration": round(stats["duration"], 3),
            "rms_db": round(stats["rms_db"], 2), "peak": round(stats["peak"], 4)}


def _clips_dir(job, work_dir: Path) -> Path:
    clips_dir = work_dir / ("clips-tease" if job.kind == "tease" else "clips")
    identity = hashlib.sha256(str(job.input_file.resolve()).encode()).hexdigest()[:12]
    return clips_dir / f"{work_stem(job)}-{identity}" / job.target_lang


@stage("synthesize")
def run(
    job: DubJob,
    vb,
    work_dir: Path,
    voice_mode: str = "clone",
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    force: bool = False,
    cast: dict | None = None,
    progress=None,
    engine: str | None = None,
    concurrency=1,
    model_size=None,
    seed=None,
    preset_voices=None,
    pronunciations=None,
    knowledge=None,
    narrator_voice="",
    narrator_delivery="",
    narrator_speakers=None,
    locale_direction="",
    character_notes=None,
    clone_cleanup=False,
) -> Plan | None:
    clips_dir = _clips_dir(job, work_dir)
    log.info("synthesize %d lines (voice_mode=%s)", len(job.segments), voice_mode)

    if dry_run:
        for seg in job.segments:
            seg.audio_clip = clips_dir / f"line_{seg.index:04d}.wav"
        return dry(f"would clone a voice + generate {len(job.segments)} clips via voicebox")

    if not job.segments:
        raise RuntimeError("nothing to synthesize (no segments)")
    if job.source_audio is None:
        raise RuntimeError("synthesize needs source audio (extract stage must run first)")
    if not job.speakers:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
    # A single narrator fallback may come from older script caches.
    if len(job.speakers) == 1:
        only = next(iter(job.speakers))
        for seg in job.segments:
            seg.speaker = only
    for seg in job.segments:
        if seg.speaker not in job.speakers:
            raise ValueError(f"unknown speaker {seg.speaker} on line {seg.index}")
    cast = dict(cast or {})
    for speaker in job.speakers.values():
        entry = dict(cast.get(speaker.label, {}))
        is_narrator = (
            speaker.label in narrator_speakers
            if narrator_speakers is not None
            else len(job.speakers) == 1
            or speaker.label == "NARRATOR"
            or entry.get("category") == "narrator"
        )
        if is_narrator:
            if narrator_voice and not entry.get("voice"):
                entry["voice"] = narrator_voice
            if narrator_delivery and not entry.get("delivery"):
                entry["delivery"] = narrator_delivery
        cast[speaker.label] = entry
    if voice_mode == "preset" and preset_voices:
        for i, speaker in enumerate(job.speakers.values()):
            if not cast.get(speaker.label, {}).get("voice"):
                cast[speaker.label] = {"voice": preset_voices[i % len(preset_voices)]}
    for speaker in job.speakers.values():
        _resolve_profile(job, speaker, vb, clips_dir, voice_mode, cast or {}, cancel,
                         cleanup=bool(clone_cleanup))

    # Generate every line. Per-line resume: clips already on disk from a
    # previous (failed/interrupted) run are kept, not regenerated.
    total = len(job.segments)
    metrics_lock = threading.Lock()
    local = threading.local()
    clients = []
    stop = cancel or threading.Event()

    def count(key, value=1):
        with metrics_lock:
            job.metrics[key] = job.metrics.get(key, 0) + value

    def render(item):
        position, seg = item
        client = vb
        if isinstance(vb, VoiceboxClient) and int(concurrency) > 1:
            if not hasattr(local, "client"):
                local.client = VoiceboxClient(vb.base_url, timeout=vb.timeout)
                clients.append(local.client)
            client = local.client
        if isinstance(client, VoiceboxClient):
            client.observer = count
        if stop.is_set():
            raise JobCancelled("cancelled before speech generation")
        if reused_selection(seg):
            count("tts_selection_reused")
            log.info("  line %d/%d kept (reviewer selected take %s)", position, total,
                     seg.audio.selection.take_id)
            return
        spk = job.speakers[seg.speaker]
        voice_engine = cast.get(seg.speaker, {}).get("engine") or engine
        # One composed instruction per line, built from every layer that
        # applies and recorded with what the engine could actually honor.
        seg.intent = compose(
            seg,
            engine=voice_engine or "",
            client=client,
            cast_delivery=cast.get(seg.speaker, {}).get("delivery", ""),
            locale_direction=locale_direction,
            character_note=(character_notes or {}).get(seg.speaker, ""),
            narrator_delivery=(narrator_delivery
                               if (narrator_speakers is not None
                                   and seg.speaker in narrator_speakers) else ""),
        )
        delivery = requested_direction(seg.intent)
        dest = clips_dir / f"line_{seg.index:04d}.wav"
        applied: list[dict] = []
        if knowledge is not None:
            text, applied = knowledge.spoken(
                seg.text_translated or seg.text_src,
                engine=voice_engine or "",
                model=model_size,
                voice=seg.voice or spk.voicebox_profile_id,
                line=knowledge.line_ref_for(seg.index),
            )
            seg.tts_text = text
            seg.applied_rules = applied
        else:
            text = spoken_form(seg.text_translated or seg.text_src, pronunciations or {})
        signature = {
            "text": text,
            "language": job.target_lang,
            "profile": seg.voice or spk.voicebox_profile_id,
            "engine": voice_engine,
            "model_size": model_size,
            "seed": seed,
            "delivery": delivery,
            "line_revision": seg.revision,
            "revision": (cast or {}).get(seg.speaker, {}).get("revision", ""),
        }
        receipt = dest.with_suffix(".json")
        try:
            saved = json.loads(receipt.read_text()) if receipt.exists() else {}
        except (OSError, ValueError):
            saved = {}  # interrupted/corrupt receipt: regenerate this line only
        if (
            not force
            and dest.exists()
            and dest.stat().st_size > 0
            and saved.get("request") == signature
            and saved.get("sha256") == hashlib.sha256(dest.read_bytes()).hexdigest()
        ):
            _register_take(seg, signature, dest, "reused")
            seg.audio_clip = dest
            count("tts_cache_hits")
            log.info("  line %d/%d kept (already synthesized)", position, total)
            return
        kwargs = {"engine": voice_engine} if voice_engine else {}
        if model_size:
            kwargs["model_size"] = model_size
        if seed is not None:
            kwargs["seed"] = seed + seg.revision
        if delivery:
            kwargs["instruct"] = delivery
        try:
            client.synthesize_to_file(
                seg.voice or spk.voicebox_profile_id,
                text,
                job.target_lang,
                dest,
                cancel_event=stop,
                **kwargs,
            )
        except GenerationFailed:
            # One fresh retry: a wedged server-side generation shouldn't kill
            # the whole job. The stuck remote generation is cancelled first so
            # it can't block the queue behind the retry.
            count("tts_retries")
            log.warning("  line %d/%d generation failed — retrying once", position, total)
            client.synthesize_to_file(
                seg.voice or spk.voicebox_profile_id,
                text,
                job.target_lang,
                dest,
                cancel_event=stop,
                **kwargs,
            )
        count("tts_generated")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        temp = receipt.with_suffix(".partial.json")
        temp.write_text(
            json.dumps(
                {
                    "request": signature,
                    "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                    "rules": applied,
                }
            )
        )
        temp.replace(receipt)
        _register_take(seg, signature, dest, "generated")
        seg.audio_clip = dest
        log.info("  line %d/%d done", position, total)

    items = iter(enumerate(job.segments, 1))
    limit = max(1, min(8, int(concurrency)))
    completed = 0

    def done():
        nonlocal completed
        completed += 1
        if progress:
            progress(completed, total, f"line {completed}/{total}")

    if limit == 1:
        for item in items:
            render(item)
            done()
    else:
        try:
            with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="tts") as pool:
                pending = {
                    pool.submit(render, item) for _, item in zip(range(limit), items, strict=False)
                }
                try:
                    while pending:
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in finished:
                            future.result()
                            done()
                            next_item = next(items, None)
                            if next_item is not None:
                                pending.add(pool.submit(render, next_item))
                except BaseException:
                    stop.set()
                    for future in pending:
                        future.cancel()
                    raise
        finally:
            for client in clients:
                client.session.close()
    return None
