"""Stage 6 — synthesize dubbed audio per segment via voicebox.

For each speaker we create a voicebox profile and clone their voice from a
reference clip pulled out of the original vocals. Then every line is generated
with that speaker's profile.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..clients.voicebox import VoiceboxClient
from ..models import DubJob

log = logging.getLogger("doblarr.synthesize")


def run(job: DubJob, vb: VoiceboxClient, work_dir: Path,
        voice_mode: str = "clone", dry_run: bool = False) -> None:
    clips_dir = work_dir / "clips"
    log.info("synthesize %d lines (voice_mode=%s)", len(job.segments), voice_mode)

    if dry_run:
        for seg in job.segments:
            seg.audio_clip = clips_dir / f"line_{seg.index:04d}.wav"
        log.info("  [dry-run] would clone %d voices + generate %d clips via voicebox",
                 len(job.speakers), len(job.segments))
        return

    # 1) Ensure every speaker has a cloned voicebox profile.
    if voice_mode == "clone":
        for spk in job.speakers.values():
            if spk.voicebox_profile_id:
                continue
            if not spk.reference_clip:
                raise RuntimeError(f"speaker {spk.label} has no reference clip to clone")
            pid = vb.create_profile(name=f"{job.input_file.stem}-{spk.label}",
                                    language=job.target_lang)
            # reference_text should be the transcript of the reference clip.
            vb.add_sample(pid, spk.reference_clip, reference_text="")
            spk.voicebox_profile_id = pid
            log.info("  cloned %s -> profile %s", spk.label, pid)

    # 2) Generate each line with its speaker's profile.
    for seg in job.segments:
        spk = job.speakers.get(seg.speaker) or next(iter(job.speakers.values()))
        if not spk.voicebox_profile_id:
            raise RuntimeError(f"no profile for speaker {seg.speaker}")
        dest = clips_dir / f"line_{seg.index:04d}.wav"
        vb.synthesize_to_file(spk.voicebox_profile_id,
                              seg.text_translated or seg.text_src,
                              job.target_lang, dest)
        seg.audio_clip = dest
