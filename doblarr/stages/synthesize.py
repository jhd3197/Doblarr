"""Stage 6 — synthesize dubbed audio per segment via voicebox.

v1: single cloned voice. A clean-ish reference clip is pulled from the original
audio (the longest dialogue segment), a voicebox profile is cloned from it, and
every line is generated with that profile. Multi-speaker cloning arrives with
diarization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

from ..clients.voicebox import VoiceboxError
from ..ffmpeg import run_ffmpeg
from ..models import DubJob, Speaker
from .common import Plan, dry, stage, work_stem

log = logging.getLogger("doblarr.synthesize")


def _extract_ref(source_audio: Path, start: float, end: float, dest: Path,
                 cancel: threading.Event | None = None) -> Path:
    """Pull a normalized mono reference clip from the original audio."""
    dur = max(4.0, min(15.0, end - start))
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-ss", str(start), "-i", str(source_audio), "-t", str(dur),
                "-vn", "-ac", "1", "-ar", "16000", "-af", "loudnorm=I=-14",
                "-c:a", "pcm_s16le", str(dest)], cancel=cancel)
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


@stage("synthesize")
def run(job: DubJob, vb, work_dir: Path, voice_mode: str = "clone",
        dry_run: bool = False, cancel: threading.Event | None = None,
        force: bool = False, cast: dict | None = None,
        progress=None, engine: str | None = None) -> Plan | None:
    clips_dir = work_dir / ("clips-tease" if job.kind == "tease" else "clips")
    identity = hashlib.sha256(str(job.input_file.resolve()).encode()).hexdigest()[:12]
    clips_dir = clips_dir / f"{work_stem(job)}-{identity}" / job.target_lang
    log.info("synthesize %d lines (voice_mode=%s)", len(job.segments), voice_mode)

    if dry_run:
        for seg in job.segments:
            seg.audio_clip = clips_dir / f"line_{seg.index:04d}.wav"
        return dry(f"would clone a voice + generate {len(job.segments)} clips "
                   "via voicebox")

    if not job.segments:
        raise RuntimeError("nothing to synthesize (no segments)")
    if job.source_audio is None:
        raise RuntimeError("synthesize needs source audio (extract stage must run first)")
    if not job.speakers:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
    spk = next(iter(job.speakers.values()))

    # A saved voice cast wins over cloning: the assigned voicebox profile is used
    # directly, keeping the voice consistent with the teaser / previous runs.
    assigned = ((cast or {}).get(spk.label) or {}).get("voice")
    if assigned and not spk.voicebox_profile_id:
        spk.voicebox_profile_id = assigned
        log.info("  using cast voice %s for %s", assigned, spk.label)

    # Clone one voice from the longest segment's original audio. On a resume
    # (previous run died mid-generation), reuse the profile we already created
    # instead of cloning a duplicate onto the voicebox server.
    if voice_mode == "clone" and not spk.voicebox_profile_id:
        profile_name = f"{job.input_file.stem}-{spk.label}"
        existing = next((v for v in vb.list_voices() if v.get("name") == profile_name),
                        None)
        if existing:
            spk.voicebox_profile_id = existing["id"]
            log.info("  reusing existing voice profile %s", profile_name)
    if voice_mode == "clone" and not spk.voicebox_profile_id:
        candidates = sorted(job.segments, key=lambda s: s.duration, reverse=True)[:5]
        # The vocals stem (no music/FX) makes a cleaner cloning reference than
        # the full mix — and a cleaner reference transcribes without loops.
        ref_source = (job.vocals if job.vocals and job.vocals.exists()
                      else job.source_audio)
        pid = vb.create_profile(name=profile_name, language=job.target_lang)
        added = False
        for c in candidates:
            ref = _extract_ref(ref_source, c.start, c.end,
                               clips_dir / "reference.wav", cancel=cancel)
            # The segment's own transcript is the reference text — far more
            # reliable than re-transcribing the clip (voicebox's transcribe
            # loops into "X、X、X…" on quiet dialogue, and a runaway reference
            # text wedges the TTS engine server-side).
            ref_text = c.text_src.strip()
            if ((job.script_lang and job.script_lang != job.source_lang)
                    or not 4.0 <= c.duration <= 15.0):
                # A translated subtitle is not a transcript of the source voice.
                # Likewise a cropped/padded sample needs its own matching text.
                ref_text = vb.transcribe(ref, language=job.source_lang).get("text", "").strip()
            if not ref_text:
                continue
            if _bad_ref_text(ref_text):
                log.warning("  segment at %.0fs looks like a transcription loop, "
                            "trying another", c.start)
                continue
            try:
                vb.add_sample(pid, ref, ref_text or "reference")
                added = True
                log.info("  cloned voice from %.1fs segment", c.duration)
                break
            except VoiceboxError as exc:
                log.warning("  reference at %.0fs rejected (%s), trying another", c.start, exc)
        if not added:
            raise RuntimeError("could not build a usable voice reference from the audio")
        spk.voicebox_profile_id = pid

    # Generate every line. Per-line resume: clips already on disk from a
    # previous (failed/interrupted) run are kept, not regenerated.
    total = len(job.segments)
    for position, seg in enumerate(job.segments, 1):
        dest = clips_dir / f"line_{seg.index:04d}.wav"
        text = seg.text_translated or seg.text_src
        signature = {"text": text, "language": job.target_lang,
                     "profile": spk.voicebox_profile_id, "engine": engine}
        receipt = dest.with_suffix(".json")
        try:
            saved = json.loads(receipt.read_text()) if receipt.exists() else {}
        except (OSError, ValueError):
            saved = {}  # interrupted/corrupt receipt: regenerate this line only
        if (not force and dest.exists() and dest.stat().st_size > 0
                and saved.get("request") == signature
                and saved.get("sha256") == hashlib.sha256(dest.read_bytes()).hexdigest()):
            seg.audio_clip = dest
            log.info("  line %d/%d kept (already synthesized)", position, total)
            if progress:
                progress(position, total, f"line {position}/{total} (cached)")
            continue
        kwargs = {"engine": engine} if engine else {}
        try:
            vb.synthesize_to_file(spk.voicebox_profile_id, text, job.target_lang,
                                  dest, cancel_event=cancel, **kwargs)
        except VoiceboxError:
            # One fresh retry: a wedged server-side generation shouldn't kill
            # the whole job. The stuck remote generation is cancelled first so
            # it can't block the queue behind the retry.
            log.warning("  line %d/%d generation failed — retrying once",
                        position, total)
            vb.synthesize_to_file(spk.voicebox_profile_id, text, job.target_lang,
                                  dest, cancel_event=cancel, **kwargs)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        temp = receipt.with_suffix(".partial.json")
        temp.write_text(json.dumps({"request": signature,
                                   "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}))
        temp.replace(receipt)
        seg.audio_clip = dest
        log.info("  line %d/%d done", position, total)
        if progress:
            progress(position, total, f"line {position}/{total}")
    return None
