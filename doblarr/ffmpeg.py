"""Thin wrappers around ffmpeg/ffprobe with uniform error reporting.

Every invocation captures output; a non-zero exit raises `FFmpegError` carrying
the tail of stderr so the failure is debuggable from the logs/UI without
re-running the command by hand. Pass a `cancel` threading.Event to make a long
run interruptible: the process is terminated (then killed if it ignores the
terminate) and `JobCancelled` is raised instead of `FFmpegError`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import wave

from .errors import DoblarrError, JobCancelled

log = logging.getLogger("doblarr.ffmpeg")

STDERR_TAIL = 5    # stderr lines kept in error messages
POLL_SECONDS = 0.2  # cancel-check cadence while a process runs
TERMINATE_GRACE = 5.0
PROBE_TIMEOUT = 120.0  # seconds; reading a file's header never takes this long


class FFmpegError(DoblarrError):
    """An ffmpeg/ffprobe invocation failed; the message carries the stderr tail."""


def _run(binary: str, args: list[str], cancel: threading.Event | None = None,
         timeout: float | None = None, binary_stdout: bool = False,
         ) -> subprocess.CompletedProcess:
    """Run one ffmpeg/ffprobe process.

    `timeout` is a wall-clock limit in seconds: a process still running after
    it (a stalled network share, a hung decoder) is stopped and FFmpegError is
    raised, so a single file cannot hold the job worker forever.
    `binary_stdout` returns stdout as bytes (decoded audio, an image); stderr
    is always text.
    """
    cmd = [binary, *args]
    log.debug("%s", " ".join(cmd))
    # UTF-8, explicitly. ffmpeg and ffprobe write UTF-8 whatever the console
    # code page is, and `text=True` alone would decode them with the locale
    # encoding — which on a Windows machine turns every non-ASCII track title,
    # language name and file path in a probe into mojibake.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            raw_out, raw_err = proc.communicate(timeout=POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            expired = deadline is not None and time.monotonic() > deadline
            if not (cancelled or expired):
                continue
            proc.terminate()
            try:
                proc.wait(timeout=TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
            if cancelled:
                raise JobCancelled(f"{binary} cancelled") from None
            raise FFmpegError(f"{binary} gave no result within {timeout:.0f}s: "
                              f"{' '.join(cmd[-3:])}") from None
    err = raw_err.decode("utf-8", errors="replace")
    out = raw_out if binary_stdout else raw_out.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        rc = proc.returncode
        if rc >= 2 ** 31:
            rc -= 2 ** 32  # Windows reports negative exits as unsigned
        tail = err.strip().splitlines()[-STDERR_TAIL:]
        raise FFmpegError(
            f"{binary} exited {rc}: {' | '.join(tail) or '(no stderr)'}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_ffmpeg(args: list[str], cancel: threading.Event | None = None,
               timeout: float | None = None) -> None:
    """Run ffmpeg with `args` (no binary name), raising FFmpegError on failure."""
    _run("ffmpeg", args, cancel=cancel, timeout=timeout)


def run_ffmpeg_bytes(args: list[str], cancel: threading.Event | None = None,
                     timeout: float | None = None) -> bytes:
    """Run ffmpeg writing to stdout (`-`) and return those bytes: PCM, an image."""
    return _run("ffmpeg", args, cancel=cancel, timeout=timeout, binary_stdout=True).stdout


def run_ffmpeg_stderr(args: list[str], cancel: threading.Event | None = None,
                      timeout: float | None = None) -> str:
    """Run ffmpeg with `args` and return its stderr.

    FFmpeg's measurement filters — `ebur128`, `astats`, `silencedetect` — all
    report on stderr rather than producing a file, so a caller that wants a
    measurement needs the text and not just the exit code.
    """
    return _run("ffmpeg", ["-hide_banner", *args], cancel=cancel, timeout=timeout).stderr


def run_ffprobe(args: list[str], cancel: threading.Event | None = None,
                timeout: float | None = PROBE_TIMEOUT) -> str:
    """Run ffprobe with `args` (no binary name) and return its stdout."""
    return _run("ffprobe", args, cancel=cancel, timeout=timeout).stdout


def probe_json(path, *args: str, timeout: float | None = PROBE_TIMEOUT) -> dict:
    """ffprobe's JSON for `path`, with `args` choosing what to show."""
    return json.loads(run_ffprobe(["-v", "error", *args, "-of", "json", str(path)],
                                  timeout=timeout) or "{}")


def media_duration(path, cancel: threading.Event | None = None) -> float:
    """Seconds of audio in `path`: read from a WAV header, else asked of ffprobe."""
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError, OSError):
        pass
    out = run_ffprobe(["-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(path)], cancel=cancel)
    return float(out.strip())
