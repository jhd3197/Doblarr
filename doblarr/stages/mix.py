"""Stage 8 — mix the dubbed lines over the background bed (ffmpeg).

Each generated clip is delayed to its segment start and mixed into a dialogue
bus, which doubles as the sidechain key ducking the bed (the separated M&E,
or — in v1 without Demucs — the original audio at reduced volume) by the
configured ducking ratio. Produces one dubbed track spanning the segments.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path

from ..ffmpeg import FFmpegError, run_ffmpeg
from ..models import DubJob
from .common import Plan, cached, dry, stage, work_stem
from .fit_timing import _duration

log = logging.getLogger("doblarr.mix")

BED_VOLUME = 0.35      # base bed level under the dubbed dialogue
DUCK_THRESHOLD = 0.02  # linear key amplitude at which the bed starts ducking
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 250
DUCK_MAKEUP = 1        # unity — ducking should only ever reduce the bed
DEFAULT_RATIO = 12.0


def _parse_ratio(text: str) -> float:
    """'12:1' -> 12.0; anything unparseable falls back to the default."""
    try:
        num = float(str(text).split(":")[0])
        if num > 1:
            return num
    except (ValueError, IndexError):
        pass
    log.warning("mix: unparseable ducking_ratio %r, using %g:1", text, DEFAULT_RATIO)
    return DEFAULT_RATIO


def _filter_graph(segs: list, win_start: float, dur: float,
                  ratio: float | None) -> str:
    """Build the mix graph. With a ratio, the bed is sidechain-ducked by the
    dialogue bus; ratio=None is the flat fallback (no sidechaincompress).
    The bus is padded to the window length — sidechaincompress ends with its
    key input, so an unpadded key would truncate the output."""
    parts = [f"[0:a]volume={BED_VOLUME},aformat=channel_layouts=stereo[bed]"]
    for i, s in enumerate(segs):
        d = max(0, int((s.start - win_start) * 1000))
        parts.append(f"[{i+1}:a]adelay={d}|{d},aformat=channel_layouts=stereo[c{i}]")
    clip_inputs = "".join(f"[c{i}]" for i in range(len(segs)))
    if ratio is None:
        parts.append(f"[bed]{clip_inputs}amix=inputs={len(segs)+1}:normalize=0[out]")
        return ";".join(parts)
    pad = f"apad=whole_dur={dur:g}"
    if len(segs) == 1:
        parts.append(f"[c0]{pad},asplit=2[dlg][key]")
    else:
        parts.append(f"{clip_inputs}amix=inputs={len(segs)}:normalize=0,"
                     f"{pad},asplit=2[dlg][key]")
    parts.append(f"[bed][key]sidechaincompress=threshold={DUCK_THRESHOLD}"
                 f":ratio={ratio:g}:attack={DUCK_ATTACK_MS}"
                 f":release={DUCK_RELEASE_MS}:makeup={DUCK_MAKEUP}[ducked]")
    parts.append("[ducked][dlg]amix=inputs=2:normalize=0[out]")
    return ";".join(parts)


@stage("mix")
def run(job: DubJob, work_dir: Path, ducking_ratio: str = "12:1",
        dry_run: bool = False, cancel: threading.Event | None = None,
        force: bool = False) -> Plan | None:
    out = work_dir / f"{work_stem(job)}.{job.target_lang}.dub.wav"
    job.dubbed_track = out
    log.info("mix dialogue over bed -> %s", out.name)
    if dry_run:
        return dry(f"would place {len(job.segments)} clips and duck the bed")
    hit = cached(out, job.input_file, force)
    if hit:
        return hit

    segs = [s for s in job.segments if s.audio_clip and Path(s.audio_clip).exists()]
    if not segs:
        raise RuntimeError("mix: no generated clips to place")
    bed = job.background or job.source_audio
    if len(segs) != len(job.segments):
        raise RuntimeError("mix: missing generated dialogue clips")
    if bed is None:
        raise RuntimeError("mix: no background audio")
    # Keep the video timeline, including the intro and closing credits.
    win_start = 0.0
    dur = _duration(job.source_audio or bed, cancel=cancel)

    # Bound the number of inputs and command length (especially on Windows).
    # Each partial bus starts at its first line; the final mix places it back
    # on the absolute timeline. Original line clips remain resumable.
    if len(segs) > 24:
        bus_dir = work_dir / f"{work_stem(job)}.{job.target_lang}.buses"
        bus_dir.mkdir(parents=True, exist_ok=True)
        buses = []
        for offset in range(0, len(segs), 24):
            group = segs[offset:offset + 24]
            start = min(s.start for s in group)
            bus = bus_dir / f"bus_{offset:04d}.wav"
            bus_args = ["-y"]
            filters = []
            for i, s in enumerate(group):
                bus_args += ["-i", str(s.audio_clip)]
                delay = max(0, round((s.start - start) * 1000))
                filters.append(f"[{i}:a]adelay={delay}:all=1,"
                               f"aformat=channel_layouts=stereo[c{i}]")
            inputs = "".join(f"[c{i}]" for i in range(len(group)))
            filters.append(f"{inputs}amix=inputs={len(group)}:normalize=0[out]")
            run_ffmpeg(bus_args + ["-filter_complex", ";".join(filters),
                       "-map", "[out]", "-ar", "48000", "-c:a", "pcm_s16le", str(bus)],
                       cancel=cancel)
            buses.append(replace(group[0], start=start, audio_clip=bus))
        segs = buses

    args = ["-y", "-ss", str(win_start), "-t", str(dur), "-i", str(bed)]
    for s in segs:
        args += ["-i", str(s.audio_clip)]
    args += ["-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le"]
    out.parent.mkdir(parents=True, exist_ok=True)

    ratio = _parse_ratio(ducking_ratio)
    temp = out.with_suffix(".partial.wav")
    try:
        run_ffmpeg(args + ["-filter_complex", _filter_graph(segs, win_start, dur, ratio),
                           "-map", "[out]", "-t", str(dur), str(temp)], cancel=cancel)
    except FFmpegError as e:
        if "No such filter" not in str(e):
            raise
        log.warning("mix: ffmpeg lacks sidechaincompress; falling back to flat mix")
        run_ffmpeg(args + ["-filter_complex", _filter_graph(segs, win_start, dur, None),
                           "-map", "[out]", "-t", str(dur), str(temp)], cancel=cancel)
    temp.replace(out)
    log.info("mix -> %s (%.0fs window, %d lines)", out.name, dur, len(segs))
    return None
