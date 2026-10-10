"""Shots and frames, read with FFmpeg in bounded passes.

- `shots` finds camera cuts with FFmpeg's scene-change score on a small scaled
  copy of the picture (PySceneDetect's content detector is used instead when it
  is installed). A cut is a property of the picture, not of the story: shots are
  grouped into scenes elsewhere (doblarr.vision.scenes).
- `frames` streams frames at a fixed rate as raw BGR arrays through one FFmpeg
  process, so a whole episode is never held in memory: callers look at each
  frame and keep only what they found in it.
- `span_frames` reads one short stretch at a higher rate (for mouth motion).

Times are seconds on the source's own timeline, the same timeline the audio
analysis uses (both come from the same file, decoded from its start).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from ..ffmpeg import probe_json

SCENE_THRESHOLD = 0.32
MIN_SHOT = 0.4          # cuts closer than this are flashes, merged into one shot


def probe_video(path: Path) -> dict:
    data = probe_json(path, "-select_streams", "v:0", "-show_entries",
                      "stream=width,height,avg_frame_rate:format=duration")
    stream = (data.get("streams") or [{}])[0]
    try:
        num, den = str(stream.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or "")
    except (TypeError, ValueError):
        duration = 0.0
    return {"width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
            "fps": round(fps, 3), "duration": duration}


def scaled_size(info: dict, height: int) -> tuple[int, int]:
    if not info["width"] or not info["height"]:
        return 0, 0
    height = min(height, info["height"])
    width = int(round(info["width"] * height / info["height"] / 2)) * 2
    return width, height - height % 2


def _cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        from ..errors import JobCancelled

        raise JobCancelled("cancelled during visual analysis")


def shots(path: Path, threshold: float = SCENE_THRESHOLD, cancel=None) -> dict:
    """Camera cuts → shots [{id, start, end}] covering the whole video."""
    info = probe_video(path)
    method = "ffmpeg-scene/1"
    cuts: list[tuple[float, float]] = []
    try:
        import scenedetect  # noqa: F401
        have_pyscenedetect = True
    except ImportError:
        have_pyscenedetect = False
    if have_pyscenedetect:
        cuts = _pyscenedetect(path)
        method = "pyscenedetect-content/1"
    else:
        with tempfile.TemporaryDirectory() as tmp:
            marks = Path(tmp) / "scenes.txt"
            # The metadata filter writes to a file named relative to the cwd;
            # running from the temp folder keeps a Windows drive letter out of
            # the filter string.
            process = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-an", "-sn",
                 "-vf", f"scale=320:-2,select='gt(scene\\,{threshold})',"
                        "metadata=print:file=scenes.txt", "-f", "null", "-"],
                cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            while process.poll() is None:
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        process.kill()
                        _cancelled(cancel)
            if process.returncode:
                error = (process.stderr.read().decode(errors="replace")[-300:]
                         if process.stderr else "")
                raise OSError(f"ffmpeg could not read the video for shot detection: {error}")
            text = marks.read_text(encoding="utf-8", errors="replace") if marks.is_file() \
                else ""
        time = None
        for line in text.splitlines():
            found = re.search(r"pts_time:([\d.]+)", line)
            if found:
                time = float(found.group(1))
                continue
            score = re.search(r"lavfi\.scene_score=([\d.]+)", line)
            if score and time is not None:
                cuts.append((time, float(score.group(1))))
                time = None
    boundaries = [0.0]
    for t, _score in sorted(cuts):
        if t - boundaries[-1] >= MIN_SHOT:
            boundaries.append(round(t, 3))
    end = info["duration"] or (boundaries[-1] + 1.0)
    rows = [{"id": f"shot-{i:04d}", "start": a, "end": round(b, 3)}
            for i, (a, b) in enumerate(zip(boundaries, [*boundaries[1:], end], strict=True))
            if b > a]
    return {"method": method, "threshold": threshold, "shots": rows, "video": info,
            "cuts": len(rows) - 1}


def _pyscenedetect(path: Path) -> list[tuple[float, float]]:
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector())
    return [(start.get_seconds(), 1.0) for start, _end in scenes[1:]]


def frames(path: Path, fps: float, height: int, *, start: float = 0.0,
           duration: float | None = None, cancel=None) -> Iterator[tuple[float, object]]:
    """(time, BGR ndarray) pairs at `fps`, streamed from one FFmpeg process."""
    import numpy as np

    info = probe_video(path)
    width, height = scaled_size(info, height)
    if not width:
        return
    args = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += ["-an", "-sn", "-vf", f"fps={fps:g},scale={width}:{height}",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = width * height * 3
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = 0
    try:
        assert process.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                process.kill()
                _cancelled(cancel)
            raw = process.stdout.read(size)
            if len(raw) < size:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            yield round(start + n / fps, 3), frame
            n += 1
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def span_frames(path: Path, start: float, end: float, fps: float, height: int,
                cancel=None) -> list[tuple[float, object]]:
    return list(frames(path, fps, height, start=max(0.0, start), duration=max(0.05, end - start),
                       cancel=cancel))
