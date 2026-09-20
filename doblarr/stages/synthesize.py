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

from ..artifacts import digest, read_json, stamp
from ..clients.voicebox import GenerationFailed, VoiceboxClient
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..models import DubJob, Speaker
from ..telemetry import write_json
from .common import Plan, dry, stage, work_stem

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


def _resolve_profile(job, spk, vb, clips_dir, voice_mode, cast, cancel):
    assigned = cast.get(spk.label, {}).get("voice")
    if assigned:
        spk.voicebox_profile_id = assigned
    if spk.voicebox_profile_id:
        return
    if voice_mode != "clone":
        raise ValueError(f"assign a preset voice to {spk.label} before synthesis")
    candidates = [
        s
        for s in job.segments
        if s.speaker == spk.label
        and s.duration >= 1.5
        and not any(
            t.speaker != spk.label and t.start < s.end and t.end > s.start for t in job.segments
        )
    ]
    candidates.sort(key=lambda s: (4 <= s.duration <= 15, -abs(s.duration - 8)), reverse=True)
    source = job.vocals if job.vocals and job.vocals.exists() else job.source_audio
    key = digest(
        {
            "source": stamp(source),
            "speaker": spk.label,
            "language": job.target_lang,
            "source_language": job.source_lang,
            "script_language": job.script_lang,
            "references": [(s.start, s.end, s.text_src) for s in candidates[:5]],
        }
    )
    profile_receipt = clips_dir / f"profile-{key[:16]}.json"
    saved = read_json(profile_receipt)
    if saved.get("id") and any(v["id"] == saved["id"] for v in vb.list_voices()):
        spk.voicebox_profile_id = saved["id"]
        return
    # Do not reuse name-only profiles: creation may have crashed before a sample was added.
    for candidate in candidates[:5]:
        ref = _extract_ref(
            source, candidate.start, candidate.end, clips_dir / f"reference-{key[:16]}.wav", cancel
        )
        text = candidate.text_src.strip()
        if job.script_lang != job.source_lang or candidate.duration > 15:
            text = vb.transcribe(ref, language=job.source_lang).get("text", "").strip()
        if not text or _bad_ref_text(text):
            continue
        pid = vb.create_profile(name=f"{job.input_file.stem}-{key[:16]}", language=job.target_lang)
        vb.add_sample(pid, ref, text)
        write_json(profile_receipt, {"id": pid, "reference": key})
        spk.reference_clip = ref
        spk.voicebox_profile_id = pid
        return
    raise RuntimeError(f"no clean single-speaker reference for {spk.label}; assign a preset voice")


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
) -> Plan | None:
    clips_dir = work_dir / ("clips-tease" if job.kind == "tease" else "clips")
    identity = hashlib.sha256(str(job.input_file.resolve()).encode()).hexdigest()[:12]
    clips_dir = clips_dir / f"{work_stem(job)}-{identity}" / job.target_lang
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
    if voice_mode == "preset" and preset_voices:
        for i, speaker in enumerate(job.speakers.values()):
            if not cast.get(speaker.label, {}).get("voice"):
                cast[speaker.label] = {"voice": preset_voices[i % len(preset_voices)]}
    for speaker in job.speakers.values():
        _resolve_profile(job, speaker, vb, clips_dir, voice_mode, cast or {}, cancel)

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
        spk = job.speakers[seg.speaker]
        dest = clips_dir / f"line_{seg.index:04d}.wav"
        text = seg.text_translated or seg.text_src
        signature = {
            "text": text,
            "language": job.target_lang,
            "profile": seg.voice or spk.voicebox_profile_id,
            "engine": engine,
            "model_size": model_size,
            "seed": seed,
            "delivery": seg.delivery,
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
            seg.audio_clip = dest
            count("tts_cache_hits")
            log.info("  line %d/%d kept (already synthesized)", position, total)
            return
        kwargs = {"engine": engine} if engine else {}
        if model_size:
            kwargs["model_size"] = model_size
        if seed is not None:
            kwargs["seed"] = seed + seg.revision
        if seg.delivery:
            kwargs["instruct"] = seg.delivery
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
                {"request": signature, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}
            )
        )
        temp.replace(receipt)
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
