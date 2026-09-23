"""Stage 2 — separate dialogue (vocals) from music + effects (M&E).

Replacing only the vocals and keeping the original M&E is what makes a dub sound
like a real dub instead of a voiceover. Runs Demucs in two-stems mode
(vocals / no_vocals); without Demucs it degrades to mixing over the original
audio. Stems are written as 16-bit WAV at the model's sample rate (44.1 kHz for
htdemucs) — the mix stage resamples to 48 kHz with ffmpeg.

A source longer than `separate.chunk_seconds` is separated in overlapping
windows that are crossfaded back together, so a feature film needs the memory
of one window rather than of the whole film, and a cancelled run resumes at the
next window.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import wave
from pathlib import Path

from .. import hardware
from ..artifacts import digest, matches, record, stamp
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..models import DubJob
from .common import Plan, cached, dry, stage, work_stem

log = logging.getLogger("doblarr.separate")


@stage("separate")
def run(job: DubJob, work_dir: Path, model: str = "htdemucs_ft",
        dry_run: bool = False, force: bool = False,
        compute: dict | None = None, chunk_seconds: float = 600,
        overlap_seconds: float = 10, cancel=None) -> Plan | None:
    job.vocals = work_dir / f"{work_stem(job)}.vocals.wav"
    job.background = work_dir / f"{work_stem(job)}.background.wav"
    if dry_run:
        return dry(f"would run Demucs on {job.source_audio}")
    hit = cached([job.vocals, job.background], job.source_audio or job.input_file, force)
    whole = {"source": stamp(job.source_audio), "model": model}
    request: dict = dict(whole)
    chunked = (chunk_seconds > 0 and job.source_audio is not None
               and job.source_audio.exists() and _seconds(job.source_audio) > chunk_seconds)
    if chunked:
        request.update(chunk_seconds=float(chunk_seconds), overlap_seconds=float(overlap_seconds))
    # Stems separated in one piece are at least as good as windowed ones, so
    # turning chunking on does not throw away a film already separated whole.
    if hit and (matches([job.vocals, job.background], request, force)
                or (chunked and matches([job.vocals, job.background], whole, force))):
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
    if chunked:
        _separate_chunked(job, demucs_main, model, device, out_root, request,
                          chunk_seconds, overlap_seconds, compute, cancel)
        with contextlib.suppress(OSError):
            out_root.rmdir()
        log.info("separate -> %s + %s (in windows of %.0fs)",
                 job.vocals.name, job.background.name, chunk_seconds)
        record([job.vocals, job.background], request)
        return None
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


# -- long files, in overlapping windows ---------------------------------------

# Every Demucs model ships at 44.1 kHz. Windows are cut at that rate, so frame
# arithmetic on the cut is the frame arithmetic on the stems Demucs returns.
CHUNK_RATE = 44100
BLOCK_FRAMES = 1 << 16


def chunk_plan(total_frames: int, chunk_frames: int,
               overlap_frames: int) -> list[tuple[int, int, int]]:
    """(owned start, owned stop, read stop) windows covering the whole track.

    Each window reads `overlap_frames` past what it owns; that context is
    crossfaded into the head of the next window, so no join is a hard cut. A
    tail no longer than the overlap is folded into the window before it
    rather than separated on its own.
    """
    if total_frames <= 0:
        return []
    if chunk_frames <= 0:
        raise ValueError("chunk_frames must be positive")
    if overlap_frames < 0:
        raise ValueError("overlap_frames must not be negative")
    plan: list[tuple[int, int, int]] = []
    start = 0
    while start < total_frames:
        stop = min(start + chunk_frames, total_frames)
        if total_frames - stop <= overlap_frames:
            stop = total_frames
        plan.append((start, stop, min(stop + overlap_frames, total_frames)))
        start = stop
    return plan


def _seconds(path: Path) -> float:
    """The source's length; 0 when it cannot be measured (Demucs reads it whole)."""
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError, OSError):
        pass
    from ..ffmpeg import FFmpegError
    from .fit_timing import _duration

    try:
        return _duration(path)
    except (FFmpegError, OSError, ValueError):
        return 0.0


def _cut(source: Path, start: int, stop: int, dest: Path, cancel=None) -> None:
    """Frames [start, stop) of `source`, at CHUNK_RATE, as 16-bit PCM."""
    run_ffmpeg(["-y", "-i", str(source), "-af",
                f"aresample={CHUNK_RATE},atrim=start_sample={start}:end_sample={stop},"
                "asetpts=PTS-STARTPTS",
                "-c:a", "pcm_s16le", str(dest)], cancel=cancel)


def _read(audio, frames: int, channels: int):
    """`frames` frames as float32 [frames, channels]; zero-padded past the end."""
    import numpy as np

    raw = audio.readframes(frames) if frames > 0 else b""
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
    if len(data) < frames:
        data = np.concatenate([data, np.zeros((frames - len(data), channels), np.float32)])
    return data


def _write(out, block) -> None:
    import numpy as np

    out.writeframes(np.clip(np.round(block), -32768, 32767).astype("<i2").tobytes())


def _crossfade(tail, head):
    """Blend the previous window's context into this window's head, linearly."""
    import numpy as np

    length = min(len(tail), len(head))
    ramp = np.linspace(0.0, 1.0, length, endpoint=False, dtype=np.float32)[:, None]
    blended = head.copy()
    blended[:length] = tail[:length] * (1.0 - ramp) + head[:length] * ramp
    return blended


def stitch(plan: list[tuple[int, int, int]], parts: list[Path], dest: Path) -> None:
    """Join per-window stems into one file, streamed block by block.

    A two-hour film never has to fit in memory: only one block and one
    overlap's worth of context are held at a time. The result is written to a
    temporary name and renamed, so a crash never leaves a half-written stem
    that a later run would take for finished.
    """
    temp = dest.with_name(dest.stem + ".partial.wav")
    tail = None
    with wave.open(str(temp), "wb") as out:
        for index, ((start, stop, read_stop), part) in enumerate(zip(plan, parts, strict=True)):
            with wave.open(str(part), "rb") as audio:
                if audio.getsampwidth() != 2:
                    raise RuntimeError(f"{part.name}: expected 16-bit stems from Demucs")
                channels = audio.getnchannels()
                if index == 0:
                    out.setnchannels(channels)
                    out.setsampwidth(2)
                    out.setframerate(audio.getframerate())
                owned, done = stop - start, 0
                if tail is not None and len(tail):
                    head = _read(audio, min(len(tail), owned), channels)
                    _write(out, _crossfade(tail, head))
                    done = len(head)
                while done < owned:
                    block = min(BLOCK_FRAMES, owned - done)
                    _write(out, _read(audio, block, channels))
                    done += block
                tail = _read(audio, read_stop - stop, channels)
    temp.replace(dest)


def _chunk_dir(out_root: Path, request: dict) -> Path:
    return out_root / "chunks" / digest(request)[:16]


def _separate_chunked(job: DubJob, demucs_main, model: str, device, out_root: Path,
                      request: dict, chunk_seconds: float, overlap_seconds: float,
                      compute: dict | None, cancel) -> None:
    """Run Demucs window by window and stitch the stems.

    A finished window's stems stay on disk until the stitch completes, so a
    cancelled or crashed run resumes at the next window instead of redoing a
    film from the start.
    """
    from ..model_pool import release_models

    assert job.source_audio is not None and job.vocals and job.background
    total = int(round(_seconds(job.source_audio) * CHUNK_RATE))
    plan = chunk_plan(total, int(chunk_seconds * CHUNK_RATE), int(overlap_seconds * CHUNK_RATE))
    chunks = _chunk_dir(out_root, request)
    chunks.mkdir(parents=True, exist_ok=True)
    stems: list[tuple[Path, Path]] = []
    for index, (start, _stop, read_stop) in enumerate(plan):
        vocals = chunks / f"{index:04d}.vocals.wav"
        bed = chunks / f"{index:04d}.no_vocals.wav"
        stems.append((vocals, bed))
        if vocals.exists() and bed.exists():
            continue  # finished by an earlier, interrupted run
        if cancel is not None and cancel.is_set():
            raise JobCancelled(f"cancelled during separation (window {index + 1}/{len(plan)})")
        piece = chunks / f"{index:04d}.wav"
        _cut(job.source_audio, start, read_stop, piece, cancel)
        log.info("separate: window %d/%d (%.0fs-%.0fs)", index + 1, len(plan),
                 start / CHUNK_RATE, read_stop / CHUNK_RATE)
        demucs_main(["--two-stems", "vocals", "-n", model, "-d", device.torch,
                     "-o", str(chunks / "demucs"), str(piece)])
        track = chunks / "demucs" / model / piece.stem
        for name, dest in (("no_vocals.wav", bed), ("vocals.wav", vocals)):
            src = track / name
            if not src.exists():
                raise RuntimeError(f"demucs finished but {src} is missing")
            shutil.move(str(src), str(dest))  # vocals last: both present = finished
        piece.unlink(missing_ok=True)
        shutil.rmtree(track, ignore_errors=True)
        if (compute or {}).get("release_after_stage", True):
            release_models()
    stitch(plan, [v for v, _b in stems], job.vocals)
    stitch(plan, [b for _v, b in stems], job.background)
    shutil.rmtree(chunks, ignore_errors=True)
    with contextlib.suppress(OSError):
        chunks.parent.rmdir()
