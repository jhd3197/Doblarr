"""Does this exchange still work as a conversation once every line is rendered?

Fitting decides one line at a time. Whether the result is a conversation is a
question about pairs of lines, and it can only be asked of the audio that was
actually produced — a plan that says a line ends at 2.40s and a render that
ends at 2.58s are different facts, and the second one is the one a listener
hears over the next speaker.

The distinction this module exists to make is between an overlap that was
always there and one the dub introduced. People talk over each other; a dub
that removes every interruption is not more correct, it is flatter. So an
overlap whose *source* intervals also overlapped is reported as intended and
left alone, an overlap between two lines that did not overlap in the original
is a collision worth someone's attention, and a reviewer can mark any of them
as deliberate — bound to the exact render they heard, so the acceptance goes
stale when the audio changes rather than silently certifying a new one.

Nothing here changes audio. The answer to "these two lines collide" is a
timing edit or a human decision, not an automatic nudge.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from .cues import TARGET, Span
from .stages import boundaries
from .stages.quality import apply_findings

log = logging.getLogger("doblarr.conversation")

DETECTOR = "conversation/1"
# Overlaps shorter than this are the natural ragged edge of two adjacent turns,
# not a collision. Below it the finding would be noise.
MIN_OVERLAP = 0.05


def _rendered_span(seg, threshold_db: float, cancel=None) -> tuple[Span | None, str]:
    """Where this line's speech is actually audible, on the target timeline.

    Measured from the rendered artifact rather than taken from the recipe: a
    requested stretch is not a delivered one, and the neighbouring line is
    overlapped by what came out, not by what was planned.
    """
    current = seg.audio.current()
    if current is None or not current.exists():
        return None, "no rendered audio"
    try:
        bounds = boundaries.inspect(Path(current.path), threshold_db, cancel)
    except (wave.Error, EOFError, OSError) as exc:
        return None, f"the render could not be measured: {exc}"
    if bounds.first is None or bounds.last is None or bounds.last <= bounds.first:
        return None, "no speech-active region in the render"
    start = seg.start + bounds.first
    end = seg.start + bounds.last
    return Span(round(max(0.0, start), 4), round(end, 4), TARGET), current.role


def _source_overlap(first, second) -> float:
    """Seconds the two lines overlapped when they were originally spoken."""
    best = 0.0
    for left in first.source.spans:
        for right in second.source.spans:
            best = max(best, min(left.end, right.end) - max(left.start, right.start))
    return max(0.0, best)


def _accepted(seg, edits: dict) -> dict | None:
    """A reviewer's standing decision that this line's overlap is deliberate."""
    entry = edits.get(seg.cue_id) or {}
    overlap = entry.get("overlap")
    if not overlap:
        return None
    if isinstance(overlap, dict):
        return dict(overlap)
    return {"accepted": True, "inputs": ""}


def check(job, options: dict | None = None, cancel=None, dry_run: bool = False) -> dict:
    """Compare every rendered turn with the one beside it. Changes no audio."""
    from . import phrases as planner

    config = planner.settings(options)
    if dry_run:
        return {}
    edits = {**{str(k): dict(v) for k, v in config["phrases"].items() if isinstance(v, dict)},
             **job.timing_edits}
    ordered = sorted(job.segments, key=lambda s: (s.start, s.index))
    spans: dict[int, Span | None] = {}
    roles: dict[int, str] = {}
    for seg in ordered:
        span, role = _rendered_span(seg, config["threshold_db"], cancel)
        spans[seg.index] = span
        roles[seg.index] = role
    collisions = intended = accepted_count = 0
    observations: dict[int, list] = {seg.index: [] for seg in ordered}
    tolerance = max(0.0, config["collision_gap"])
    for position, seg in enumerate(ordered):
        mine = spans[seg.index]
        if mine is None:
            continue
        for other in ordered[position + 1:]:
            theirs = spans[other.index]
            if theirs is None:
                continue
            if theirs.start >= mine.end - MIN_OVERLAP + tolerance:
                break  # ordered by start: nothing further can overlap either
            seconds = round(min(mine.end, theirs.end) - max(mine.start, theirs.start), 4)
            if seconds < MIN_OVERLAP:
                continue
            original = round(_source_overlap(seg, other), 4)
            evidence = {
                "with_cue": other.cue_id, "with_line": other.index,
                "seconds": seconds, "source_overlap": original,
                "mine": [mine.start, mine.end], "theirs": [theirs.start, theirs.end],
                "roles": [roles.get(seg.index), roles.get(other.index)],
                "speakers": [seg.speaker, other.speaker],
            }
            decision = _accepted(seg, edits) or _accepted(other, edits)
            if decision is not None:
                current = (seg.audio.current().fingerprint if seg.audio.current() else "")
                stale = bool(decision.get("inputs")) and decision["inputs"] != current
                evidence["accepted"] = {**decision, "stale": stale}
                if not stale:
                    accepted_count += 1
                    observations[seg.index].append((
                        "timing_overlap_accepted", "timing", "info", None, evidence))
                    continue
            if seg.speaker == other.speaker:
                collisions += 1
                observations[seg.index].append((
                    "timing_self_overlap", "timing", "warning", None,
                    {**evidence, "note": "one speaker is talking over themselves; this is "
                                         "almost always a fit that ran long"}))
                continue
            if original >= MIN_OVERLAP:
                intended += 1
                observations[seg.index].append((
                    "timing_overlap_intended", "timing", "info", None,
                    {**evidence, "note": "these lines overlapped in the original too; "
                                         "the interruption is preserved on purpose"}))
                continue
            collisions += 1
            observations[seg.index].append((
                "timing_collision", "timing", "warning", None,
                {**evidence, "note": "these two lines did not overlap in the original"}))
    for seg in ordered:
        span = spans[seg.index]
        inputs = f"{span.start:.3f}/{span.end:.3f}" if span else roles.get(seg.index, "")
        apply_findings(seg, DETECTOR, inputs, observations[seg.index])
    summary = {
        "measured": sum(1 for span in spans.values() if span is not None),
        "unmeasured": sum(1 for span in spans.values() if span is None),
        "collisions": collisions,
        "intended_overlaps": intended,
        "accepted_overlaps": accepted_count,
    }
    job.metrics["conversation"] = summary
    if collisions:
        log.info("conversation: %d introduced collision(s), %d original overlap(s) kept",
                 collisions, intended)
    return summary
