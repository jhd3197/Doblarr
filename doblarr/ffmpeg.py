"""Thin wrappers around ffmpeg/ffprobe with uniform error reporting.

Every invocation captures output; a non-zero exit raises `FFmpegError` carrying
the tail of stderr so the failure is debuggable from the logs/UI without
re-running the command by hand.
"""

from __future__ import annotations

import logging
import subprocess

from .errors import DoblarrError

log = logging.getLogger("doblarr.ffmpeg")

STDERR_TAIL = 5  # stderr lines kept in error messages


class FFmpegError(DoblarrError):
    """An ffmpeg/ffprobe invocation failed; the message carries the stderr tail."""


def _run(binary: str, args: list[str]) -> subprocess.CompletedProcess:
    cmd = [binary, *args]
    log.debug("%s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        rc = proc.returncode
        if rc >= 2 ** 31:
            rc -= 2 ** 32  # Windows reports negative exits as unsigned
        tail = (proc.stderr or "").strip().splitlines()[-STDERR_TAIL:]
        raise FFmpegError(
            f"{binary} exited {rc}: {' | '.join(tail) or '(no stderr)'}")
    return proc


def run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with `args` (no binary name), raising FFmpegError on failure."""
    _run("ffmpeg", args)


def run_ffprobe(args: list[str]) -> str:
    """Run ffprobe with `args` (no binary name) and return its stdout."""
    return _run("ffprobe", args).stdout
