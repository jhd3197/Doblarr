"""Stage 1 — extract the original audio track from the video (ffmpeg)."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..artifacts import read_json, stamp
from ..discovery import ISO3_TO_ISO2
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
