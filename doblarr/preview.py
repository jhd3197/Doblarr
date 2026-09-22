"""Bounded scene previews for review: hear a line inside its exchange.

D04's server half. A reviewer needs several things playable at the same
position: the original scene, the final dubbed scene, the dry generated line on
its own, whichever candidate take they are considering, and the sample a cloned
voice was built from. This module defines the window, resolves which file each
of those is, and renders the two scene excerpts as small browser-playable WAVs.

Three rules it does not bend:

- **A window is a window, not a scene cut.** Neighbouring cues are gathered by
  a silence gap, which is a heuristic. The result says `heuristic` so nobody
  reads it as a proven scene boundary.
- **Source time is source time.** The original excerpt is cut from the
  preserved source track over the cues' *source* spans. The dubbed excerpt is
  cut from the mixed track over their *target* spans. An audition montage
  moves target time and never touches the source record, so the two stay
  correct even there.
- **Nothing outside the configured directories is ever served.** Every path
  handed back is one this module built inside the job's own work directory, or
  one the caller re-checks against its own guard.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .artifacts import digest, matches, record, stamp
from .cues import RAW
from .ffmpeg import FFmpegError, run_ffmpeg

log = logging.getLogger("doblarr.preview")

# A gap at least this long between two cues is treated as the edge of the
# exchange. It is a heuristic about pacing, not a detected scene change.
SCENE_GAP_SECONDS = 2.5
# Never cut more than this for one preview, whatever the window asks for.
MAX_WINDOW_SECONDS = 90.0
# Context kept outside the first and last cue so a line does not start abruptly.
PAD_SECONDS = 0.6
# Plan 04 adds the three the coverage review needs: the separated dialogue
# stem on its own, the bed the dub sits over, and one reaction's own audio.
KINDS = ("source", "dub", "line", "take", "reference", "vocals", "bed", "event")


class PreviewError(RuntimeError):
    """A preview could not be produced, with a reason worth showing."""


def window(segments, index: int, context: int = 2, bounds=None) -> dict:
    """The cues around `index` that make up one exchange, and its two spans.

    `context` is how many neighbours to include on each side; the window also
    stops at a silence gap, whichever is tighter. `bounds` overrides the target
    span outright, which is how an editable window is expressed.
    """
    ordered = sorted(segments, key=lambda s: (s.start, s.index))
    position = next((i for i, s in enumerate(ordered) if s.index == index), None)
    if position is None:
        raise PreviewError(f"line {index} is not in this review")
    context = max(0, min(8, int(context)))
    first = last = position
    while first > 0 and position - first < context:
        if ordered[first].start - ordered[first - 1].end > SCENE_GAP_SECONDS:
            break
        first -= 1
    while last < len(ordered) - 1 and last - position < context:
        if ordered[last + 1].start - ordered[last].end > SCENE_GAP_SECONDS:
            break
        last += 1
    chosen = ordered[first:last + 1]
    target = _span(chosen, source=False)
    if bounds:
        start, end = float(bounds[0]), float(bounds[1])
        if not (end > start >= 0):
            raise PreviewError("a preview window needs a positive time range")
        target = (start, min(end, start + MAX_WINDOW_SECONDS))
    return {
        "index": index,
        "cues": [s.index for s in chosen],
        "cue_ids": [s.cue_id for s in chosen],
        "target": {"start": round(target[0], 3), "end": round(target[1], 3),
                   "domain": "target"},
        "source": _source_span(chosen),
        "boundary": "heuristic",
        "boundary_note": (f"cues grouped by a {SCENE_GAP_SECONDS:g}s silence gap; "
                          "this is a listening window, not a detected scene cut"),
        "editable": True,
    }


def _span(chosen, source: bool) -> tuple[float, float]:
    start = min(s.start for s in chosen)
    end = max(s.end for s in chosen)
    start = max(0.0, start - PAD_SECONDS)
    end = min(end + PAD_SECONDS, start + MAX_WINDOW_SECONDS)
    return start, end


def _source_span(chosen) -> dict | None:
    """The original interval this exchange was spoken over, when it is recorded.

    Deliberately built from `source.spans` and not from the cue window: a
    review edit or an audition montage moves target time, and the source record
    is what says where the performance actually is.
    """
    spans = [s for seg in chosen for s in seg.source.spans]
    if not spans:
        return None
    start = max(0.0, min(s.start for s in spans) - PAD_SECONDS)
    end = min(max(s.end for s in spans) + PAD_SECONDS, start + MAX_WINDOW_SECONDS)
    return {"start": round(start, 3), "end": round(end, 3), "domain": "source"}


def events_in(job, frame: dict) -> list:
    """The nonverbal events that fall inside one preview window.

    Matched on *target* time, because the window is what the reviewer is about
    to hear. An event with no recorded placement is left out of the window
    rather than pinned to its start: an unplaced event is not at a time yet.
    """
    span = frame.get("target") or {}
    start, end = float(span.get("start", 0.0)), float(span.get("end", 0.0))
    found = []
    for event in job.nonverbal:
        target = event.target
        if target is None:
            continue
        if min(target.end, end) - max(target.start, start) <= 0:
            continue
        found.append(event)
    return sorted(found, key=lambda e: e.target.start)


def excerpt(source: Path, start: float, end: float, dest: Path, cancel=None) -> Path:
    """Cut one bounded, browser-playable excerpt, reusing an identical one.

    Always 16-bit PCM stereo at 48 kHz: the original container may be a codec
    no browser will decode, and a review that cannot play its evidence is not
    a review.
    """
    source = Path(source)
    if not source.is_file():
        raise PreviewError("the media this preview needs is not on disk")
    length = max(0.05, min(float(end) - float(start), MAX_WINDOW_SECONDS))
    request = {"source": stamp(source), "start": round(float(start), 3),
               "length": round(length, 3), "version": 1}
    dest = dest.parent / f"{dest.stem}.{digest(request)[:12]}.wav"
    if matches([dest], request):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    try:
        run_ffmpeg(["-y", "-ss", f"{max(0.0, float(start)):g}", "-i", str(source),
                    "-t", f"{length:g}", "-vn", "-ac", "2", "-ar", "48000",
                    "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
    except FFmpegError as exc:
        temp.unlink(missing_ok=True)
        raise PreviewError(f"this excerpt could not be decoded: {exc}") from exc
    temp.replace(dest)
    record([dest], request)
    return dest


def resolve(job, segments, kind: str, index: int, *, context: int = 2,
            bounds=None, take: str = "", event: str = "", root: Path | None = None,
            cancel=None) -> dict:
    """Produce one preview and say exactly what it is.

    Returns the file to serve plus the window it covers, so the browser can
    line the original and the dub up at the same position instead of guessing.
    """
    if kind not in KINDS:
        raise PreviewError(f"unknown preview kind {kind!r}")
    frame = window(segments, index, context, bounds)
    seg = next(s for s in segments if s.index == index)
    root = Path(root or (job.artifacts_dir or Path("."))) / "previews"

    if kind == "line":
        current = seg.audio.current()
        if current is None or not current.exists():
            raise PreviewError("this line has no rendered audio yet")
        return {**frame, "kind": kind, "path": Path(current.path),
                "role": current.role, "label": f"processed line ({current.role})",
                "whole_file": True}
    if kind == "take":
        chosen = seg.audio.take(take) if take else seg.audio.selected()
        if chosen is None or chosen.raw is None or not chosen.raw.exists():
            raise PreviewError("that take has no audio on disk")
        return {**frame, "kind": kind, "path": Path(chosen.raw.path), "role": RAW,
                "take": chosen.take_id, "label": f"dry take {chosen.take_id}",
                "whole_file": True}
    if kind == "reference":
        # The sample a cloned voice was built from. Kept separate from the
        # takes: a clone reference is conditioning, not a performance, and it
        # is never the evidence a source level was measured on.
        speaker = job.speakers.get(seg.speaker) if job.speakers else None
        clip = getattr(speaker, "reference_clip", None)
        if not clip or not Path(clip).is_file():
            raise PreviewError("no clone reference was kept for this character")
        return {**frame, "kind": kind, "path": Path(clip), "role": "reference",
                "label": f"clone reference for {seg.speaker}", "whole_file": True}
    if kind == "event":
        # One reaction's own audio, so "does this play once, at the right
        # moment, at the right level" can be answered without hunting for it
        # inside the finished mix.
        found = next((e for e in job.nonverbal if e.event_id == event), None)
        if found is None:
            raise PreviewError("that event is not part of this review")
        if not found.rendered:
            raise PreviewError(
                f"this event has no audio: {found.reason or 'nothing was placed for it'}")
        return {**frame, "kind": kind, "path": Path(found.artifact.path),
                "role": found.coverage, "event": found.event_id,
                "label": f"{found.type} ({found.coverage})", "whole_file": True}
    if kind in ("vocals", "bed"):
        # The two halves of the separation, over the *target* window, so the
        # bed under the dub and the voices taken out of it can be heard against
        # the finished mix at the same position.
        track = job.vocals if kind == "vocals" else job.background
        if kind == "bed" and track is not None and track == job.source_audio:
            raise PreviewError(
                "separation did not run for this job, so there is no separated bed — "
                "the original mix is playing under the dub")
        if not track or not Path(track).is_file():
            raise PreviewError("that separated stem is not on disk for this job")
        span = frame["target"]
        path = excerpt(Path(track), span["start"], span["end"],
                       root / f"{kind}_{index:04d}", cancel)
        return {**frame, "kind": kind, "path": path, "domain": "target",
                "label": ("separated dialogue stem" if kind == "vocals"
                          else "separated background (estimated)"),
                "whole_file": False}
    if kind == "source":
        track = job.source_track or job.source_audio
        if not track:
            raise PreviewError("the original audio for this job is not available")
        span = frame["source"]
        if span is None:
            raise PreviewError("this exchange has no recorded source interval")
        path = excerpt(Path(track), span["start"], span["end"],
                       root / f"source_{index:04d}", cancel)
        return {**frame, "kind": kind, "path": path, "label": "original scene",
                "domain": "source", "whole_file": False}
    # kind == "dub"
    track = job.dubbed_track or job.output_file
    if not track:
        raise PreviewError("this job has no mixed dub to play yet")
    span = frame["target"]
    path = excerpt(Path(track), span["start"], span["end"],
                   root / f"dub_{index:04d}", cancel)
    return {**frame, "kind": kind, "path": path, "label": "dubbed scene",
            "domain": "target", "whole_file": False}
