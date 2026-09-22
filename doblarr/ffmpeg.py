"""Thin wrappers around ffmpeg/ffprobe with uniform error reporting.

Every invocation captures output; a non-zero exit raises `FFmpegError` carrying
the tail of stderr so the failure is debuggable from the logs/UI without
re-running the command by hand. Pass a `cancel` threading.Event to make a long
run interruptible: the process is terminated (then killed if it ignores the
terminate) and `JobCancelled` is raised instead of `FFmpegError`.
"""

from __future__ import annotations

import logging
import subprocess
import threading

from .errors import DoblarrError, JobCancelled

log = logging.getLogger("doblarr.ffmpeg")

STDERR_TAIL = 5    # stderr lines kept in error messages
POLL_SECONDS = 0.2  # cancel-check cadence while a process runs
TERMINATE_GRACE = 5.0


class FFmpegError(DoblarrError):
    """An ffmpeg/ffprobe invocation failed; the message carries the stderr tail."""


def _run(binary: str, args: list[str],
         cancel: threading.Event | None = None) -> subprocess.CompletedProcess:
    cmd = [binary, *args]
    log.debug("%s", " ".join(cmd))
    # UTF-8, explicitly. ffmpeg and ffprobe write UTF-8 whatever the console
    # code page is, and `text=True` alone would decode them with the locale
    # encoding — which on a Windows machine turns every non-ASCII track title,
    # language name and file path in a probe into mojibake. The container is
    # fine; only the reading of it was wrong, which is the worst kind of bug to
    # have underneath a check that exists to verify track metadata.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    while True:
        try:
            out, err = proc.communicate(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelled(f"{binary} cancelled") from None
    if proc.returncode != 0:
        rc = proc.returncode
        if rc >= 2 ** 31:
            rc -= 2 ** 32  # Windows reports negative exits as unsigned
        tail = (err or "").strip().splitlines()[-STDERR_TAIL:]
        raise FFmpegError(
            f"{binary} exited {rc}: {' | '.join(tail) or '(no stderr)'}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_ffmpeg(args: list[str], cancel: threading.Event | None = None) -> None:
    """Run ffmpeg with `args` (no binary name), raising FFmpegError on failure."""
    _run("ffmpeg", args, cancel=cancel)


def run_ffmpeg_stderr(args: list[str], cancel: threading.Event | None = None) -> str:
    """Run ffmpeg with `args` and return its stderr.

    FFmpeg's measurement filters — `ebur128`, `astats`, `silencedetect` — all
    report on stderr rather than producing a file, so a caller that wants a
    measurement needs the text and not just the exit code.
    """
    return _run("ffmpeg", ["-hide_banner", *args], cancel=cancel).stderr


def run_ffprobe(args: list[str], cancel: threading.Event | None = None) -> str:
    """Run ffprobe with `args` (no binary name) and return its stdout."""
    return _run("ffprobe", args, cancel=cancel).stdout
