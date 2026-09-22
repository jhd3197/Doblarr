"""Stage 1 — extract the original audio track from the video (ffmpeg)."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..artifacts import digest, read_json, stamp
from ..cues import SOURCE, SourceReference, Span
from ..discovery import ISO3_TO_ISO2
from ..errors import DoblarrError
from ..ffmpeg import run_ffmpeg, run_ffprobe
from ..models import DubJob
from ..telemetry import write_json
from .common import Plan, cached, dry, stage, work_stem

log = logging.getLogger("doblarr.extract")


def _select_audio_stream(job: DubJob, cancel=None) -> int:
    data = json.loads(
        run_ffprobe(
            [
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index:stream_tags=language",
                "-of",
                "json",
                str(job.input_file),
            ],
            cancel=cancel,
        )
    )
    streams = data.get("streams", [])
    wanted = ISO3_TO_ISO2.get(job.source_lang.lower(), job.source_lang.lower())
    for stream in streams:
        tag = stream.get("tags", {}).get("language", "und").lower()
        if ISO3_TO_ISO2.get(tag, tag) == wanted:
            return stream["index"]
    if len(streams) == 1 and streams[0].get("tags", {}).get("language", "und") == "und":
        log.warning("using the only untagged audio track as %s", wanted)
        return streams[0]["index"]
    raise RuntimeError(f"no unambiguous {wanted} audio track in {job.input_file.name}")


def _stream_layout(job: DubJob, index: int, cancel=None) -> tuple[str, int | None]:
    """Channel layout of the selected stream; unknown rather than guessed."""
    try:
        data = json.loads(
            run_ffprobe(
                [
                    "-v",
                    "error",
                    "-select_streams",
                    "a",
                    "-show_entries",
                    "stream=index,channels,channel_layout",
                    "-of",
                    "json",
                    str(job.input_file),
                ],
                cancel=cancel,
            )
        )
    except (DoblarrError, OSError, ValueError):
        return "", None
    for stream in data.get("streams", []):
        if stream.get("index") == index:
            channels = stream.get("channels")
            return str(stream.get("channel_layout") or ""), (
                int(channels) if isinstance(channels, int) else None)
    return "", None


def _reference(job: DubJob, index: int, duration: int | None, identity: dict,
               cancel=None) -> SourceReference:
    layout, channels = _stream_layout(job, index, cancel)
    return SourceReference(
        media_key=digest(identity)[:16],
        media_path=str(job.input_file.resolve()),
        stream_index=index,
        language=job.source_lang,
        channel_layout=layout,
        channels=channels,
        extraction_revision=digest({"identity": identity, "version": 1})[:16],
        time_base=SOURCE,
        cut=Span(0.0, float(duration), SOURCE) if duration else None,
    )


@stage("extract")
def run(
    job: DubJob,
    work_dir: Path,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    force: bool = False,
    duration: int | None = None,
) -> Plan | None:
    out = work_dir / f"{work_stem(job)}.source.wav"
    job.source_audio = out
    args = ["-y", "-i", str(job.input_file)]
    if duration:
        args += ["-t", str(duration)]  # tease: only the first `duration` seconds
    args += ["-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)]
    log.info("extract audio -> %s", out.name)
    if dry_run:
        return dry("ffmpeg " + " ".join(args))
    index = _select_audio_stream(job, cancel)
    receipt = out.with_suffix(".json")
    identity = {"input": stamp(job.input_file), "stream": index, "duration": duration}
    # Recorded on every run, cached or not: later stages must be able to say
    # which track the source evidence came from, not only that a file exists.
    job.source_reference = _reference(job, index, duration, identity, cancel)
    hit = cached(out, job.input_file, force)
    if hit and read_json(receipt) == identity:
        return hit
    args[args.index("-vn") : args.index("-vn")] = ["-map", f"0:{index}"]
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".partial.wav")
    args[-1] = str(temp)
    run_ffmpeg(args, cancel=cancel)
    temp.replace(out)
    write_json(receipt, identity)
    return None
