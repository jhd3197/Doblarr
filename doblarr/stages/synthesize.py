"""Stage 6 — synthesize dubbed audio per segment via voicebox.

v1: single cloned voice. A clean-ish reference clip is pulled from the original
audio (the longest dialogue segment), a voicebox profile is cloned from it, and
every line is generated with that profile. Multi-speaker cloning arrives with
diarization.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..clients.voicebox import VoiceboxError
from ..ffmpeg import run_ffmpeg
from ..models import DubJob, Speaker
from .common import dry, stage

log = logging.getLogger("doblarr.synthesize")


def _extract_ref(source_audio: Path, start: float, end: float, dest: Path) -> Path:
    """Pull a normalized mono reference clip from the original audio."""
    dur = max(4.0, min(15.0, end - start))
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-ss", str(start), "-i", str(source_audio), "-t", str(dur),
                "-vn", "-ac", "1", "-ar", "16000", "-af", "loudnorm=I=-14",
                "-c:a", "pcm_s16le", str(dest)])
    return dest


def _safe_transcribe(vb, clip: Path, lang: str) -> str:
    try:
        data = vb.transcribe(clip, language=None if lang in ("auto", "", None) else lang)
        return (data.get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("reference transcribe failed: %s", exc)
        return ""


@stage("synthesize")
def run(job: DubJob, vb, work_dir: Path, voice_mode: str = "clone",
        dry_run: bool = False) -> None:
    clips_dir = work_dir / "clips"
    log.info("synthesize %d lines (voice_mode=%s)", len(job.segments), voice_mode)

    if dry_run:
        for seg in job.segments:
            seg.audio_clip = clips_dir / f"line_{seg.index:04d}.wav"
        return dry(f"would clone a voice + generate {len(job.segments)} clips "
                   "via voicebox")

    if not job.segments:
        raise RuntimeError("nothing to synthesize (no segments)")
    if not job.speakers:
        job.speakers = {"SPEAKER_00": Speaker(label="SPEAKER_00")}
    spk = next(iter(job.speakers.values()))

    # Clone one voice from the longest segment's original audio.
    if voice_mode == "clone" and not spk.voicebox_profile_id:
        candidates = sorted(job.segments, key=lambda s: s.duration, reverse=True)[:3]
        pid = vb.create_profile(name=f"{job.input_file.stem}-{spk.label}",
                                language=job.target_lang)
        added = False
        for c in candidates:
            ref = _extract_ref(job.source_audio, c.start, c.end,
                               clips_dir / "reference.wav")
            ref_text = _safe_transcribe(vb, ref, job.source_lang) or "reference"
            try:
                vb.add_sample(pid, ref, ref_text)
                added = True
                log.info("  cloned voice from %.1fs segment", c.duration)
                break
            except VoiceboxError as exc:
                log.warning("  reference at %.0fs rejected (%s), trying another", c.start, exc)
        if not added:
            raise RuntimeError("could not build a usable voice reference from the audio")
        spk.voicebox_profile_id = pid

    # Generate every line.
    for seg in job.segments:
        text = seg.text_translated or seg.text_src
        dest = clips_dir / f"line_{seg.index:04d}.wav"
        vb.synthesize_to_file(spk.voicebox_profile_id, text, job.target_lang, dest)
        seg.audio_clip = dest
        log.info("  line %d/%d done", seg.index + 1, len(job.segments))
