"""Phrase timing: fit a line's parts deliberately instead of squeezing all of it.

Whole-clip fitting answers "this line is 12% too long" with "play all of it 12%
faster". That is the right answer when there is nothing better to do and the
wrong one whenever the line contains silence somebody could give back. D07 asks
for the better answer: find the phrases, find the gaps between them, decide
which gaps are performance and which are padding, and spend the padding first.

What this module owns:

- **Evidence.** Where the speech actually runs inside the prepared take, taken
  from the same energy detector boundary preparation uses. A phrase is a run of
  speech between two silences that were already there; nothing is invented.
- **Correspondence, or the honest absence of it.** A phrase is associated with
  a source interval only when the source's own silences group into the same
  number of runs, in order. Translated word order does not match the original,
  so individual words are never paired and an unknown association stays empty.
- **A bounded plan.** Padding shrinks to a floor, protected pauses do not move,
  speech compresses up to a bound and only slows down when an anchor demands
  it. When that is not enough the plan says `infeasible` and names why, because
  dropping a clause to make the numbers work is not fitting, it is deleting.

What it deliberately does not own: rendering (that is `stages/phrase_timing`),
lip shapes, and any decision about whether the result sounds natural.
"""

from __future__ import annotations

import logging
import math
import re

from .cues import (
    CLIP,
    SOURCE,
    TARGET,
    Anchor,
    Pause,
    Phrase,
    Span,
    TimingPlan,
    anchor_id,
    now,
    pause_id,
    phrase_id,
    validate_plan,
)

log = logging.getLogger("doblarr.phrases")

PLANNER = "phrase-timing/1"

# Overflow within this ratio is inaudible once mixed. Shared with whole-clip
# fitting on purpose: the two owners must agree on what "already fits" means.
FIT_SLACK = 1.02
# Clause boundaries worth splitting text at. Deliberately punctuation, not
# "every N words": a line wrapped by a subtitle renderer is not a phrase.
_CLAUSE = re.compile(r"(?<=[.!?。！？…])\s+|(?<=[,;:，；])\s+")


def settings(options: dict | None) -> dict:
    """Normalize the `timing` config section, with today's behaviour as default."""
    values = dict(options or {})
    mode = str(values.get("mode", "whole") or "whole")
    if mode not in ("whole", "phrase"):
        raise ValueError("timing.mode must be whole or phrase")
    return {
        "mode": mode,
        "max_stretch": max(1.0, float(values.get("max_stretch", 1.3))),
        # 1.0 means "never slow speech down". Below 1.0 a hard anchor may pull a
        # phrase later; nothing else ever will, because stretching short speech
        # to fill a subtitle slot is how a dub starts sounding sedated.
        "min_stretch": min(1.0, max(0.5, float(values.get("min_stretch", 1.0)))),
        "min_pause": max(0.0, float(values.get("min_pause", 0.12))),
        # A gap this long is a beat somebody chose. Shorter ones are the
        # breathing a synthesizer puts around punctuation, and tightening
        # those to the floor is what buys the room a fit needs.
        "protect_pause": max(0.0, float(values.get("protect_pause", 0.45))),
        "tail_handle": max(0.0, float(values.get("tail_handle_ms", 60)) / 1000),
        "anchor_tolerance": max(0.01, float(values.get("anchor_tolerance", 0.12))),
        "threshold_db": float(values.get("threshold_db", 12)),
        "min_separation_db": float(values.get("min_separation_db", 10)),
        "min_phrase": max(0.05, float(values.get("min_phrase_seconds", 0.15))),
        # A protective margin carried into the silence on each side of a
        # phrase. An energy detector places a boundary where the level crosses
        # a threshold, which is a little after a soft consonant starts and a
        # little before its tail has died; cutting exactly there is how a "p"
        # goes missing.
        "handle": max(0.0, float(values.get("handle_ms", 40)) / 1000),
        "repair": bool(values.get("repair", True)),
        "collision_gap": float(values.get("collision_gap", 0.0)),
        "phrases": dict(values.get("phrases") or {}),
        "overlaps": dict(values.get("overlaps") or {}),
    }


def owns_timing(options: dict | None) -> bool:
    """Whether phrase fitting, rather than whole-clip fitting, owns this run."""
    try:
        return settings(options)["mode"] == "phrase"
    except ValueError:
        return False


def split_text(text: str) -> list[str]:
    """Clause-sized pieces of a line, at punctuation only."""
    parts = [piece.strip() for piece in _CLAUSE.split(text or "") if piece.strip()]
    return parts or ([text.strip()] if (text or "").strip() else [])


def runs_from(bounds, config: dict):
    """Speech runs inside a measured take, its lead and tail, and the real gaps.

    Returns runs in the CLIP domain, already grown by their protective handles,
    plus `detected`: how long each gap was *before* the handles were carved out
    of it. Whether a pause is performance is a question about what the take
    contains, not about how much of it this renderer decided to keep, so the
    classification uses the detected length.
    """
    if bounds.first is None or bounds.last is None or bounds.last <= bounds.first:
        return [], 0.0, 0.0, []
    runs: list[tuple[float, float]] = []
    cursor = bounds.first
    for gap_start, gap_end in bounds.silences:
        if gap_start > cursor:
            runs.append((cursor, gap_start))
        cursor = max(cursor, gap_end)
    if bounds.last > cursor:
        runs.append((cursor, bounds.last))
    runs = [(a, b) for a, b in runs if b - a >= config["min_phrase"]]
    if not runs:
        runs = [(bounds.first, bounds.last)]
    detected = [round(following[0] - previous[1], 4)
                for previous, following in zip(runs, runs[1:], strict=False)]
    runs = _with_handles(runs, bounds.duration, config["handle"])
    return runs, runs[0][0], max(0.0, bounds.duration - runs[-1][1]), detected


def _with_handles(runs, duration: float, handle: float):
    """Grow each run into the surrounding silence, never past its neighbour.

    Two adjacent runs share the gap between them at its midpoint, so the
    handles can never overlap and no sample is ever rendered twice — a
    duplicated consonant at a join is exactly the artifact this guards against.
    """
    if handle <= 0 or not runs:
        return runs
    grown = []
    for index, (start, end) in enumerate(runs):
        left = 0.0 if index == 0 else runs[index - 1][1] + (start - runs[index - 1][1]) / 2
        right = (duration if index == len(runs) - 1
                 else end + (runs[index + 1][0] - end) / 2)
        grown.append((max(left, start - handle, 0.0), min(right, end + handle, duration)))
    return grown


def source_runs(seg, config: dict) -> list[tuple[float, float]]:
    """The original's own speech runs, grouped by its silences, in SOURCE time.

    Built from word evidence when the transcript carries timed words. Without
    them there is nothing to group and the answer is an empty list, which is
    read as "correspondence unknown" and never as "one phrase".
    """
    timed = [w for w in (seg.words or [])
             if isinstance(w, dict) and w.get("start") is not None and w.get("end") is not None]
    if len(timed) < 2 or seg.source.word_domain != SOURCE:
        return []
    runs: list[list[float]] = []
    for word in sorted(timed, key=lambda w: float(w["start"])):
        start, end = float(word["start"]), float(word["end"])
        if end <= start:
            continue
        if runs and start - runs[-1][1] < config["protect_pause"]:
            runs[-1][1] = max(runs[-1][1], end)
        else:
            runs.append([start, end])
    return [(a, b) for a, b in runs]


def build(seg, bounds, config: dict, edits: dict | None = None) -> TimingPlan:
    """Describe one line's phrases, pauses and anchors. No audio is touched.

    `bounds` is the measured prepared take. `edits` is the reviewer's own
    record for this cue: hard anchors, protected-pause overrides and a bypass.
    """
    edits = dict(edits or {})
    plan = TimingPlan(mode="phrase", planner=PLANNER, slot=round(seg.duration, 4),
                      onset=seg.placement.onset, at=now(),
                      max_stretch=config["max_stretch"],
                      min_stretch=config["min_stretch"])
    if edits.get("bypass"):
        plan.state = "bypassed"
        plan.bypassed = True
        plan.reason = "a reviewer asked for this line's timing to be left alone"
        return plan
    runs, lead, tail, detected = runs_from(bounds, config)
    if not runs:
        plan.state = "unavailable"
        plan.reason = "no speech-active region could be located in this take"
        return plan

    texts = split_text(seg.text_translated or seg.text_src)
    aligned_text = len(texts) == len(runs) and len(runs) > 1
    originals = source_runs(seg, config)
    # Correspondence is accepted only when the two sides group into the same
    # number of runs. Anything else is recorded as unknown rather than forced:
    # a translated clause can carry two source clauses' worth of meaning, and a
    # fabricated pairing would anchor the dub to the wrong moment.
    aligned_source = len(originals) == len(runs) and len(runs) > 1
    cue_source = seg.source.start
    method = "aligned" if (aligned_text or aligned_source) else "acoustic"
    if len(runs) == 1:
        method = "whole"

    for order, (start, end) in enumerate(runs):
        identity = phrase_id(seg.cue_id, order)
        plan.phrases.append(Phrase(
            phrase_id=identity, order=order,
            text=texts[order] if aligned_text else "",
            clip=Span(start, end, CLIP),
            source=([Span(*originals[order], SOURCE)] if aligned_source else []),
            method=method,
            # The detector's separation is the only number here worth quoting,
            # and it is about audibility, not about the phrase being right.
            confidence=round(min(1.0, max(0.0, bounds.separation / 30)), 3),
        ))

    protect = config["protect_pause"]
    overrides = {str(k): v for k, v in (edits.get("pauses") or {}).items()}
    # The lead is boundary preparation's business, not this planner's: an
    # intended opening wait and untrimmed generator padding look identical from
    # here, so it is preserved rather than spent.
    if lead > 0:
        plan.pauses.append(Pause(pause_id=pause_id(seg.cue_id, -1), after="",
                                 clip=Span(0.0, lead, CLIP), kind="pause",
                                 protected=True, origin="boundary"))
    last_end = runs[-1][1]
    for order, (previous, following) in enumerate(zip(runs, runs[1:], strict=False)):
        if following[0] - previous[1] <= 1e-6:
            # The handles met in the middle of this gap: the two phrases are
            # contiguous now and there is no silence left between them to
            # classify, let alone to spend.
            continue
        gap = Span(previous[1], following[0], CLIP)
        identity = pause_id(seg.cue_id, order)
        override = overrides.get(identity)
        protected = detected[order] >= protect
        kind = "pause" if protected else "padding"
        origin = "auto"
        if isinstance(override, dict) and "protected" in override:
            protected = bool(override["protected"])
            kind = str(override.get("kind") or ("pause" if protected else "padding"))
            origin = "review"
        elif isinstance(override, bool):
            protected, origin = override, "review"
            kind = "pause" if protected else "padding"
        plan.pauses.append(Pause(pause_id=identity, after=plan.phrases[order].phrase_id,
                                 clip=gap, kind=kind, protected=protected, origin=origin))
    if tail > config["tail_handle"]:
        plan.pauses.append(Pause(pause_id=pause_id(seg.cue_id, len(runs)),
                                 after=plan.phrases[-1].phrase_id,
                                 clip=Span(last_end, last_end + tail, CLIP),
                                 kind="padding", protected=False, origin="tail"))

    _derive_anchors(seg, plan, config, edits, cue_source)
    plan.conflicts = [*plan.conflicts, *validate_plan(plan, f"cue {seg.cue_id}")]
    plan.state = "planned"
    return plan


def _derive_anchors(seg, plan: TimingPlan, config: dict, edits: dict,
                    cue_source: float | None) -> None:
    """Soft anchors from the original's own timing, then the reviewer's hard ones."""
    tolerance = config["anchor_tolerance"]
    slot = seg.duration
    for phrase in plan.phrases:
        if not phrase.source or cue_source is None:
            continue
        at = phrase.source[0].start - cue_source
        if not (0 <= at <= slot):
            continue
        plan.anchors.append(Anchor(
            anchor_id=anchor_id(seg.cue_id, phrase.phrase_id, "start"),
            phrase_id=phrase.phrase_id, edge="start", at=round(at, 4),
            kind="soft", tolerance=tolerance, origin="source",
            note="where the original speaker started this part"))
    known = {p.phrase_id for p in plan.phrases}
    by_order = {p.order: p.phrase_id for p in plan.phrases}
    for raw in (edits.get("anchors") or []):
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("phrase") or "")
        if target not in known and raw.get("order") is not None:
            target = by_order.get(int(raw["order"]), "")
        if target not in known:
            # An anchor for a phrase this take no longer has. Recorded as a
            # conflict rather than dropped: the reviewer asked for something,
            # and the line has changed underneath them.
            plan.conflicts.append({
                "code": "anchor_orphaned",
                "detail": f"an anchor names phrase {raw.get('phrase') or raw.get('order')}, "
                          "which this take does not have any more"})
            continue
        edge = "end" if str(raw.get("edge")) == "end" else "start"
        wanted = raw.get("at")
        if wanted is None:
            continue
        try:
            at = float(wanted)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(at) or at < 0:
            continue
        identity = anchor_id(seg.cue_id, target, edge)
        plan.anchors = [a for a in plan.anchors if a.anchor_id != identity]
        plan.anchors.append(Anchor(
            anchor_id=identity, phrase_id=target, edge=edge, at=round(at, 4),
            kind="hard", tolerance=float(raw.get("tolerance") or tolerance),
            origin="review", note=str(raw.get("note") or "")))


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------

def plan_pieces(plan: TimingPlan, config: dict) -> TimingPlan:
    """Turn phrases, pauses and anchors into an ordered render instruction.

    The plan is solved run by run between hard anchors, so a reviewer's anchor
    is a wall the fitting happens inside rather than a suggestion averaged away.
    Each run spends its padding first and only then compresses speech, up to
    the configured bound. A run that still does not fit is a conflict and the
    whole plan becomes `infeasible` — the words stay, the problem is visible.
    """
    if plan.state not in ("planned", "applied", "fallback") or not plan.phrases:
        return plan
    slot = plan.slot or 0.0
    onset = plan.onset or 0.0
    lead = next((p for p in plan.pauses if p.origin == "boundary"), None)
    # An explicit placement onset replaces the take's own lead silence rather
    # than adding to it; otherwise a trimmed take with a re-inserted onset
    # would wait twice.
    start_at = max(onset, lead.measured if lead else 0.0)

    items = _items(plan, config)
    walls = _walls(plan, items, slot, start_at)
    pieces: list[dict] = []
    conflicts = list(plan.conflicts)
    moved = protected_kept = speech_in = speech_out = 0.0
    used_max = 1.0
    used_min = 1.0

    for (first, last), (window_start, window_end, hard) in walls:
        run_items = items[first:last]
        window = window_end - window_start
        speech = sum(item["seconds"] for item in run_items if item["kind"] == "speech")
        fixed = sum(item["seconds"] for item in run_items
                    if item["kind"] == "gap" and item["protected"])
        loose = [item for item in run_items if item["kind"] == "gap" and not item["protected"]]
        slack = sum(max(0.0, item["seconds"] - item["floor"]) for item in loose)
        needed = speech + fixed + sum(item["seconds"] for item in loose)
        allowance = window * (FIT_SLACK if not hard else 1.0)
        shrink = 0.0
        factor = 1.0
        if needed > allowance:
            shrink = min(needed - allowance, slack)
            remaining = needed - shrink - allowance
            if remaining > 1e-6 and speech > 0:
                factor = min(config["max_stretch"], speech / max(1e-6, speech - remaining))
                if speech / factor - (speech - remaining) > 1e-3:
                    conflicts.append({
                        "code": "phrase_infeasible",
                        "detail": (f"{speech:.2f}s of speech needs {needed:.2f}s but only "
                                   f"{window:.2f}s is available, even after spending "
                                   f"{shrink:.2f}s of padding and compressing "
                                   f"{config['max_stretch']:.2f}x"),
                        "window": [round(window_start, 3), round(window_end, 3)]})
        elif hard and needed < window - 1e-3 and config["min_stretch"] < 1.0:
            # Only a hard anchor can justify slowing speech down, and only as
            # far as the configured floor.
            factor = max(config["min_stretch"], speech / max(1e-6, speech + (window - needed)))
        used_max = max(used_max, factor)
        used_min = min(used_min, factor)
        cursor = window_start
        for item in run_items:
            if item["kind"] == "speech":
                out = item["seconds"] / factor
                speech_in += item["seconds"]
                speech_out += out
                pieces.append({
                    "kind": "speech", "phrase": item["id"],
                    "in": [round(item["start"], 4), round(item["end"], 4)],
                    "factor": round(factor, 4), "at": round(cursor, 4),
                    "out": round(out, 4)})
                cursor += out
                continue
            if item["protected"]:
                out = item["seconds"]
                protected_kept += out
            else:
                share = 0.0 if slack <= 0 else (
                    max(0.0, item["seconds"] - item["floor"]) / slack)
                out = item["seconds"] - shrink * share
                moved += item["seconds"] - out
            out = max(item["floor"], out)
            pieces.append({"kind": "gap", "pause": item["id"],
                           "measured": round(item["seconds"], 4),
                           "at": round(cursor, 4), "out": round(out, 4)})
            item["planned"] = out
            cursor += out

    for pause in plan.pauses:
        planned = next((p["out"] for p in pieces
                        if p["kind"] == "gap" and p.get("pause") == pause.pause_id), None)
        pause.planned = None if planned is None else round(planned, 4)

    plan.pieces = pieces
    plan.moved = round(moved, 4)
    plan.protected_kept = round(protected_kept, 4)
    plan.speech_in = round(speech_in, 4)
    plan.speech_out = round(speech_out, 4)
    plan.max_stretch = round(used_max, 4)
    plan.min_stretch = round(used_min, 4)
    plan.planned_duration = round(
        max((p["at"] + p["out"] for p in pieces), default=start_at), 4)
    plan.conflicts = conflicts
    if any(c["code"] in ("phrase_infeasible", "anchor_order", "anchor_outside_slot")
           for c in conflicts):
        # The fallback reason is kept: "fitted as one whole *and* it still does
        # not fit" is two different facts and a reviewer needs both.
        was = f"{plan.reason}; " if plan.state == "fallback" and plan.reason else ""
        plan.state = "infeasible"
        plan.reason = was + conflicts[0]["detail"]
    elif plan.state == "fallback" and plan.reason:
        # Same rule as above: why it fell back and what the fit then did are two
        # facts, and overwriting the first with the second loses the useful one.
        plan.reason = f"{plan.reason}; {_describe(plan, slot)}"
    else:
        plan.reason = _describe(plan, slot)
    _observe_planned(plan)
    return plan


def _items(plan: TimingPlan, config: dict) -> list[dict]:
    """The plan's content in play order: speech runs and the gaps between them."""
    ordered = sorted(plan.phrases, key=lambda p: p.order)
    by_after = {p.after: p for p in plan.pauses if p.after}
    tail = next((p for p in plan.pauses if p.origin == "tail"), None)
    items: list[dict] = []
    for phrase in ordered:
        span = phrase.clip
        items.append({"kind": "speech", "id": phrase.phrase_id,
                      "start": span.start if span else 0.0,
                      "end": span.end if span else 0.0,
                      "seconds": span.duration if span else 0.0,
                      "protected": False, "floor": 0.0})
        gap = by_after.get(phrase.phrase_id)
        if gap is None or gap is tail:
            continue
        items.append({"kind": "gap", "id": gap.pause_id, "seconds": gap.measured,
                      "protected": gap.protected,
                      "floor": (gap.measured if gap.protected
                                else min(gap.measured, config["min_pause"]))})
    if tail is not None:
        items.append({"kind": "gap", "id": tail.pause_id, "seconds": tail.measured,
                      "protected": False, "floor": 0.0})
    return items


def _walls(plan: TimingPlan, items: list[dict], slot: float, start_at: float):
    """Split the item list into runs bounded by hard anchors.

    Yields `((first, last), (window_start, window_end, hard))`. Without hard
    anchors there is exactly one run covering the whole slot, which is the
    ordinary case and costs nothing.
    """
    position = {item["id"]: index for index, item in enumerate(items)}
    cuts: list[tuple[int, float]] = []
    for anchor in plan.anchors:
        if anchor.kind != "hard":
            continue
        index = position.get(anchor.phrase_id)
        if index is None:
            continue
        cuts.append((index if anchor.edge == "start" else index + 1, anchor.at))
    cuts = sorted({(index, at) for index, at in cuts if 0 < index < len(items)})
    runs = []
    previous_index, previous_time = 0, start_at
    for index, at in cuts:
        runs.append(((previous_index, index), (previous_time, at, True)))
        previous_index, previous_time = index, at
    runs.append(((previous_index, len(items)),
                 (previous_time, slot, bool(cuts))))
    return runs


def _observe_planned(plan: TimingPlan) -> None:
    """Record where each anchored edge is *planned* to land, before rendering."""
    starts = {p.get("phrase"): p for p in plan.pieces if p["kind"] == "speech"}
    for anchor in plan.anchors:
        piece = starts.get(anchor.phrase_id)
        if piece is None:
            continue
        anchor.observed = round(
            piece["at"] if anchor.edge == "start" else piece["at"] + piece["out"], 4)


def _describe(plan: TimingPlan, slot: float) -> str:
    parts = [f"{len(plan.phrases)} phrase{'' if len(plan.phrases) == 1 else 's'}"]
    if plan.moved:
        parts.append(f"{plan.moved:.2f}s of padding redistributed")
    if plan.protected_kept:
        parts.append(f"{plan.protected_kept:.2f}s of protected pause kept")
    if plan.max_stretch > 1.001:
        parts.append(f"compressed up to {plan.max_stretch:.2f}x")
    elif plan.min_stretch < 0.999:
        parts.append(f"slowed to {plan.min_stretch:.2f}x for a hard anchor")
    else:
        parts.append("no stretching needed")
    parts.append(f"{plan.planned_duration:.2f}s in a {slot:.2f}s window")
    return "; ".join(parts)


def whole_line(seg, bounds, config: dict, reason: str) -> TimingPlan:
    """The bounded single-phrase fallback, used when phrase evidence is not usable.

    It is a real plan with one phrase, so everything downstream — rendering,
    invalidation, review — works the same way. `state="fallback"` is what says
    the line was fitted as one piece and why, instead of a phrase recipe
    quietly pretending to know where the clauses were.
    """
    plan = TimingPlan(mode="phrase", planner=PLANNER, slot=round(seg.duration, 4),
                      onset=seg.placement.onset, at=now(),
                      max_stretch=config["max_stretch"],
                      min_stretch=config["min_stretch"])
    if bounds is None or bounds.first is None or bounds.last is None:
        plan.state = "unavailable"
        plan.reason = reason
        return plan
    identity = phrase_id(seg.cue_id, 0)
    plan.phrases = [Phrase(phrase_id=identity, order=0,
                           text=seg.text_translated or seg.text_src,
                           clip=Span(bounds.first, bounds.last, CLIP),
                           method="whole")]
    tail = max(0.0, bounds.duration - bounds.last)
    if bounds.first > 0:
        plan.pauses.append(Pause(pause_id=pause_id(seg.cue_id, -1), after="",
                                 clip=Span(0.0, bounds.first, CLIP), kind="pause",
                                 protected=True, origin="boundary"))
    if tail > config["tail_handle"]:
        plan.pauses.append(Pause(pause_id=pause_id(seg.cue_id, 1), after=identity,
                                 clip=Span(bounds.last, bounds.last + tail, CLIP),
                                 kind="padding", protected=False, origin="tail"))
    plan.state = "fallback"
    plan.reason = reason
    return plan_pieces(plan, config)


def target_span(seg, plan: TimingPlan) -> Span | None:
    """Where this line's speech is planned to be heard, on the target timeline."""
    speech = [p for p in plan.pieces if p["kind"] == "speech"]
    if not speech:
        return None
    start = seg.start + speech[0]["at"]
    end = seg.start + speech[-1]["at"] + speech[-1]["out"]
    if end <= start:
        return None
    return Span(round(start, 4), round(end, 4), TARGET)
