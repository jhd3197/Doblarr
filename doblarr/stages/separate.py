"""Stage 2 — separate dialogue (vocals) from music + effects (M&E).

Replacing only the vocals and keeping the original M&E is what makes a dub sound
like a real dub instead of a voiceover. Runs Demucs in two-stems mode
(vocals / no_vocals); without Demucs it degrades to mixing over the original
audio. Stems are written as 16-bit WAV at the model's sample rate (44.1 kHz for
htdemucs) — the mix stage resamples to 48 kHz with ffmpeg.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path

from .. import hardware
from ..artifacts import matches, record, stamp
from ..models import DubJob
from .common import Plan, cached, dry, stage, work_stem

log = logging.getLogger("doblarr.separate")


@stage("separate")
def run(job: DubJob, work_dir: Path, model: str = "htdemucs_ft",
        dry_run: bool = False, force: bool = False,
        compute: dict | None = None) -> Plan | None:
    job.vocals = work_dir / f"{work_stem(job)}.vocals.wav"
    job.background = work_dir / f"{work_stem(job)}.background.wav"
    if dry_run:
        return dry(f"would run Demucs on {job.source_audio}")
    hit = cached([job.vocals, job.background], job.source_audio or job.input_file, force)
    request = {"source": stamp(job.source_audio), "model": model}
    if hit and matches([job.vocals, job.background], request, force):
        return hit

    # If Demucs is available, isolate dialogue; otherwise fall back to mixing over
    # the original audio (the score/original speech stays under the dub).
    try:
        from demucs.separate import main as demucs_main  # lazy: pulls in torch
    except ImportError as exc:
        job.background = job.source_audio
        job.vocals = None
        log.warning("Demucs not available (%s) — mixing over the original audio "
                    "(install torch + demucs for clean dialogue separation)", exc)
        return None

    if job.source_audio is None:
        raise RuntimeError("separate: no source audio — the extract stage must run first")
    # The device is deliberately not in `request`: the stems are the same on
    # any device, so moving to a GPU must not throw the cached ones away.
    device = hardware.resolve_device("separate", compute)
    job.metrics.setdefault("devices", {})["separate"] = device.torch
    out_root = work_dir / "separated"
    out_root.mkdir(parents=True, exist_ok=True)
    log.info("separate (%s on %s): isolating dialogue from %s",
             model, device, job.source_audio.name)
    demucs_main(["--two-stems", "vocals", "-n", model, "-d", device.torch,
                 "-o", str(out_root), str(job.source_audio)])

    track_dir = out_root / model / job.source_audio.stem
    for src, dst in ((track_dir / "vocals.wav", job.vocals),
                     (track_dir / "no_vocals.wav", job.background)):
        if not src.exists():
            raise RuntimeError(f"demucs finished but {src} is missing")
        shutil.move(str(src), str(dst))
    shutil.rmtree(track_dir, ignore_errors=True)
    for parent in (track_dir.parent, out_root):
        with contextlib.suppress(OSError):
            parent.rmdir()  # only succeeds once empty

    log.info("separate -> %s + %s", job.vocals.name, job.background.name)
    record([job.vocals, job.background], request)
    return None
