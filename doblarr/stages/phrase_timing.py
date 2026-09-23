"""Render one line's phrase timing recipe, then check what actually came out.

The planner in `doblarr.phrases` decides; this renders and verifies. It is the
second timing owner: `timing.mode = "whole"` keeps `fit_timing` exactly as it
was, and `timing.mode = "phrase"` hands the job here and produces the `phrased`
artifact instead. Only ever one of the two, because two stretches of the same
line compound into a warble nobody asked for.

Three things it refuses to do:

- **Assume the stretch it asked for is the stretch it got.** `atempo` is a
  request. The output is measured and `actual_duration` is what was measured,
  so a plan that missed is visible instead of asserted.
- **Fit by deleting.** When the words genuinely do not fit, the line is
  repaired within the shared request budget — an already-generated take that
  fits, then a bounded rewrite — and if neither works the plan stays
  `infeasible` with every word still in it.
- **Reprocess its own output.** It reads `CueAudio.upstream_of(PHRASED)`, so
  changing an anchor re-renders from the prepared take rather than stacking a
  second stretch onto the first.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

from .. import pacing
from .. import phrases as planner
from ..artifacts import digest, matches, record, stamp
from ..cues import PHRASED, Artifact, Selection, now
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..fingerprints import processing as processing_fingerprint
from ..models import DubJob
from . import boundaries
from .common import Plan, dry, stage
from .fit_timing import _atempo_chain, _duration
from .quality import apply_findings

log = logging.getLogger("doblarr.phrase_timing")

DETECTOR = "phrase-timing/1"
# A join sits inside silence the take already had, so this is only insurance
# against a click. It is short enough to be inaudible and is never applied to
# the first attack or the final tail, which are performance.
JOIN_FADE = 0.004


def _forget(seg) -> None:
    """Drop a phrase render this run will not make, and everything after it."""
    if seg.audio.render(PHRASED) is None:
        return
    seg.audio.drop_renders((PHRASED,))
    seg.audio.invalidate_after(PHRASED)
    upstream = seg.audio.upstream_of(PHRASED)
    if upstream is not None and upstream.path:
        seg.audio_clip = Path(upstream.path)


def bypass(job, reason: str = "whole-clip fitting owns this run") -> None:
    """Stand down for a run whose timing belongs to the other owner.

    Findings this detector raised on a previous run are retired rather than
    left open: a collision report about a phrase recipe that is not in use any
    more is not evidence about the audio anybody can hear now.
    """
    for seg in job.segments:
        seg.phrasing = planner.TimingPlan(mode="whole", state="bypassed",
                                          planner=DETECTOR, reason=reason,
                                          slot=round(seg.duration, 4))
        _forget(seg)
        apply_findings(seg, DETECTOR, "bypassed", [])


def _measure(seg, config: dict, cancel
             ) -> tuple[boundaries.Bounds | None, Path | None, Artifact | None]:
    """Measure the prepared take this line would be phrase-fitted from."""
    upstream = seg.audio.upstream_of(PHRASED)
    if upstream is None or not upstream.exists():
        return None, None, None
    source = Path(upstream.path)
    try:
        bounds = boundaries.inspect(source, config["threshold_db"], cancel)
    except (wave.Error, EOFError, OSError) as exc:
        log.info("line %d: take could not be analyzed (%s)", seg.index, exc)
        return None, source, upstream
    return bounds, source, upstream


def _plan_for(seg, bounds, config: dict, edits: dict | None):
    """Build and solve one line's plan, falling back when the evidence is thin."""
    if bounds is None:
        plan = planner.TimingPlan(mode="phrase", state="unavailable", planner=DETECTOR,
                                  slot=round(seg.duration, 4),
                                  reason="no prepared audio to plan from")
        return plan
    if bounds.separation < config["min_separation_db"]:
        return planner.whole_line(
            seg, bounds, config,
            f"only {bounds.separation:.1f} dB between speech and noise "
            f"(needs {config['min_separation_db']:g}), so the phrase boundaries in "
            "this take are not knowable; fitted as one bounded whole")
    plan = planner.build(seg, bounds, config, edits)
    if plan.state != "planned":
        return plan
    if len(plan.phrases) < 2:
        return planner.whole_line(
            seg, bounds, config,
            "only one continuous run of speech was found, so there is no internal "
            "silence to redistribute; fitted as one bounded whole")
    return planner.plan_pieces(plan, config)


def _is_noop(plan, upstream_duration: float | None) -> bool:
    """Whether the recipe would reproduce its input sample for sample."""
    if plan.state not in ("planned", "fallback") or not plan.pieces:
        return False
    speech = [p for p in plan.pieces if p["kind"] == "speech"]
    if any(abs(p["factor"] - 1.0) > 1e-4 for p in speech):
        return False
    if any(abs(p["out"] - p["measured"]) > 1e-4
           for p in plan.pieces if p["kind"] == "gap"):
        return False
    if speech and abs(speech[0]["at"] - speech[0]["in"][0]) > 1e-4:
        return False
    if upstream_duration is None:
        return False
    return abs((plan.planned_duration or 0.0) - upstream_duration) <= 1e-3


def _eligible_take(seg, needed: float):
    """A take already on this cue that would fit, so a repair costs no request.

    Regenerating is the expensive answer. A candidate somebody already paid for
    is the cheap one, and preferring it is why alternate takes are retained.
    """
    current = seg.audio.selection.take_id if seg.audio.selection else None
    options = []
    for take in seg.audio.takes:
        if take.take_id == current or take.state == "failed":
            continue
        if take.raw is None or not take.raw.exists():
            continue
        length = take.raw.duration
        if length is None:
            try:
                length = _duration(Path(take.raw.path))
            except (OSError, ValueError):
                continue
        if length <= needed:
            options.append((length, take))
    if not options:
        return None
    return min(options, key=lambda row: row[0])[1]


def _select(seg, take) -> None:
    previous = seg.audio.selection.take_id if seg.audio.selection else None
    seg.audio.selection = Selection(take_id=take.take_id, reason="restored",
                                    actor="phrase timing", previous=previous, at=now())
    seg.audio.invalidate_after("raw")
    if take.raw is not None:
        seg.audio_clip = Path(take.raw.path)


def _render(seg, plan, source: Path, cancel) -> Path:
    """Cut, tempo and re-place every piece in one ffmpeg pass."""
    speech = [p for p in plan.pieces if p["kind"] == "speech"]
    request = {"source": stamp(source), "planner": planner.PLANNER,
               "pieces": [[p["in"][0], p["in"][1], p["factor"], p["at"]] for p in speech],
               "duration": plan.planned_duration, "version": 1}
    dest = source.parent / "phrased" / f"{source.stem}.{digest(request)[:12]}.wav"
    if matches([dest], request):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    count = len(speech)
    labels = "".join(f"[s{i}]" for i in range(count))
    graph = [f"[0:a]asplit={count}{labels}"]
    for index, piece in enumerate(speech):
        start, end = piece["in"]
        chain = [f"atrim=start={start:.4f}:end={end:.4f}", "asetpts=PTS-STARTPTS"]
        if abs(piece["factor"] - 1.0) > 1e-4:
            chain.append(_atempo_chain(piece["factor"]))
        length = piece["out"]
        fade = min(JOIN_FADE, max(0.0, length / 8))
        if index > 0 and fade > 0:
            chain.append(f"afade=t=in:st=0:d={fade:g}")
        if index < count - 1 and fade > 0:
            chain.append(f"afade=t=out:st={max(0.0, length - fade):g}:d={fade:g}")
        chain.append("aformat=channel_layouts=stereo")
        delay = max(0, round(piece["at"] * 1000))
        chain.append(f"adelay={delay}:all=1")
        graph.append(f"[s{index}]{','.join(chain)}[p{index}]")
    mixed = "".join(f"[p{i}]" for i in range(count))
    total = max(0.05, plan.planned_duration or 0.05)
    if count == 1:
        graph.append(f"[p0]apad=whole_dur={total:g},atrim=end={total:g}[out]")
    else:
        graph.append(f"{mixed}amix=inputs={count}:normalize=0,"
                     f"apad=whole_dur={total:g},atrim=end={total:g}[out]")
    run_ffmpeg(["-y", "-i", str(source), "-filter_complex", ";".join(graph),
                "-map", "[out]", "-ac", "2", "-ar", "48000",
                "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
    temp.replace(dest)
    record([dest], request)
    return dest


def _observe(plan, rendered: Path, config: dict, cancel) -> None:
    """Read the rendered file and say where the phrases really landed.

    A requested tempo is not a delivered tempo, and a plan that reported its
    own arithmetic as the result would hide exactly the drift this check
    exists to catch.
    """
    try:
        bounds = boundaries.inspect(rendered, config["threshold_db"], cancel)
    except (wave.Error, EOFError, OSError):
        return
    plan.actual_duration = round(bounds.duration, 4)
    runs, _lead, _tail, _gaps = planner.runs_from(bounds, config)
    speech = [p for p in plan.pieces if p["kind"] == "speech"]
    if len(runs) != len(speech):
        # The output has a different number of audible runs than the recipe
        # placed — usually because a shortened gap merged two phrases. It is
        # recorded rather than reconciled: an observation that had to guess is
        # not an observation.
        plan.conflicts = [*plan.conflicts, {
            "code": "observation_ambiguous",
            "detail": (f"the rendered line has {len(runs)} audible run(s) where the "
                       f"recipe placed {len(speech)}")}]
        return
    observed = {piece["phrase"]: run for piece, run in zip(speech, runs, strict=True)}
    for anchor in plan.anchors:
        run = observed.get(anchor.phrase_id)
        if run is None:
            continue
        anchor.observed = round(run[0] if anchor.edge == "start" else run[1], 4)
    plan.speech_out = round(sum(end - start for start, end in runs), 4)


def _findings(seg, plan) -> None:
    codes = {c["code"] for c in plan.conflicts}
    observed = []
    if plan.state == "infeasible":
        observed.append(("timing_infeasible", "timing", "warning", None, {
            "reason": plan.reason, "slot": plan.slot,
            "planned": plan.planned_duration, "conflicts": plan.conflicts,
            "note": "every word is still in the line; the fit is what failed"}))
    if codes & {"anchor_order", "anchor_outside_slot", "anchor_orphaned"}:
        observed.append(("timing_anchor_conflict", "timing", "warning", None, {
            "conflicts": [c for c in plan.conflicts if c["code"] in
                          {"anchor_order", "anchor_outside_slot", "anchor_orphaned"}]}))
    if plan.state == "fallback":
        observed.append(("timing_fallback", "timing", "info", None, {
            "reason": plan.reason,
            "note": "this line was fitted as one bounded whole, not by phrase"}))
    missed = [a for a in plan.anchors
              if a.kind == "hard" and a.error is not None and abs(a.error) > a.tolerance]
    if missed:
        observed.append(("timing_anchor_missed", "timing", "warning", None, {
            "anchors": [{"phrase": a.phrase_id, "asked": a.at, "landed": a.observed,
                         "off_by": a.error, "tolerance": a.tolerance} for a in missed]}))
    apply_findings(seg, DETECTOR, plan.inputs or f"{plan.state}/{plan.slot}", observed)


@stage("phrase_timing")
def run(
    job: DubJob,
    work_dir: Path,
    options: dict | None = None,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    force: bool = False,
    translator=None,
    regenerate=None,
    checkpoint=None,
    max_attempts: int = 2,
    budget=None,
) -> Plan | None:
    config = planner.settings(options)
    if config["mode"] != "phrase":
        if not dry_run:
            bypass(job)
        return None
    if dry_run:
        return dry("would plan each line's phrases and render its timing recipe")
    log.info("phrase timing over %d lines (max stretch %.2fx)",
             len(job.segments), config["max_stretch"])
    rendered = repaired = fallbacks = infeasible = 0
    edits = {**{str(k): dict(v) for k, v in config["phrases"].items() if isinstance(v, dict)},
             **job.timing_edits}
    paced: list[tuple] = []  # (seg, take the plan read, rendered file or None)
    for seg in job.segments:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during phrase timing")
        # The timing owner that runs owns the legacy issue strings too, so a
        # line that fits now drops an overflow a previous run recorded.
        seg.issues = [i for i in seg.issues
                      if i not in {"timing_overflow", "timing_repair_failed"}]
        bounds, source, upstream = _measure(seg, config, cancel)
        plan = _plan_for(seg, bounds, config, edits.get(seg.cue_id))
        plan.attempts = seg.phrasing.attempts
        attempts = max(0, min(5, int(max_attempts)))
        for _attempt in range(attempts):
            if plan.state != "infeasible" or not config["repair"]:
                break
            repair = _repair(job, seg, plan, config, translator, regenerate,
                             checkpoint, budget)
            if repair is None:
                break
            repaired += 1
            bounds, source, upstream = _measure(seg, config, cancel)
            spent = plan.attempts + 1
            plan = _plan_for(seg, bounds, config, edits.get(seg.cue_id))
            plan.attempts = spent
        seg.phrasing = plan
        # A fallback that then turned out not to fit is still a fallback: the
        # state names the more actionable of the two facts, so the count reads
        # the plan's shape rather than its state.
        if len(plan.phrases) == 1 and plan.phrases[0].method == "whole":
            fallbacks += 1
        if source is not None:
            paced.append((seg, source, None))
        if plan.state in ("bypassed", "unavailable") or not plan.pieces:
            _forget(seg)
            _findings(seg, plan)
            continue
        if plan.state == "infeasible":
            # The fit failed, and the line is still rendered at the bounded
            # maximum the plan reached. Leaving it unfitted would be worse in
            # every way: it would overrun its slot by more, and nothing would
            # say so. The state stays `infeasible` after the render, so review
            # reads "this did not fit" rather than "this was fitted".
            infeasible += 1
        if plan.state != "infeasible" and _is_noop(
                plan, bounds.duration if bounds is not None else None):
            plan.state = "applied"
            plan.reason = f"{plan.reason}; already fits, so the prepared take is used as it is"
            plan.actual_duration = bounds.duration if bounds is not None else None
            _forget(seg)
            _findings(seg, plan)
            continue
        assert source is not None
        dest = _render(seg, plan, source, cancel)
        _observe(plan, dest, config, cancel)
        if plan.state != "infeasible":
            plan.state = "applied"
        elif plan.actual_duration and plan.slot:
            over = plan.actual_duration - plan.slot
            if over > 0:
                plan.reason = (f"{plan.reason}; rendered at the bounded maximum and "
                               f"still {over:.2f}s over its window")
                seg.issues.append("timing_overflow")
        plan.inputs = processing_fingerprint({
            "input": upstream.fingerprint if upstream else "",
            "pieces": [[p["in"][0], p["in"][1], p["factor"], p["at"]]
                       for p in plan.pieces if p["kind"] == "speech"],
            "duration": plan.planned_duration, "planner": planner.PLANNER})
        seg.audio.put_render(Artifact(
            role=PHRASED, path=str(dest), fingerprint=plan.inputs,
            derived_from=upstream.role if upstream else "",
            duration=plan.actual_duration or plan.planned_duration,
            bytes=dest.stat().st_size if dest.exists() else None))
        seg.audio_clip = dest
        paced[-1] = (seg, source, dest)
        rendered += 1
        _findings(seg, plan)
    _record_pacing(job, paced, options, cancel)

    job.metrics["phrase_rendered"] = rendered
    job.metrics["phrase_repairs"] = repaired
    job.metrics["phrase_fallbacks"] = fallbacks
    job.metrics["phrase_infeasible"] = infeasible
    job.metrics["phrase_moved_seconds"] = round(
        sum(s.phrasing.moved for s in job.segments), 3)
    job.metrics["phrase_max_stretch"] = round(
        max((s.phrasing.max_stretch for s in job.segments), default=1.0), 4)
    job.metrics["phrases_planned"] = sum(len(s.phrasing.phrases) for s in job.segments)
    job.metrics["protected_pauses"] = sum(
        sum(1 for p in s.phrasing.pauses if p.protected) for s in job.segments)
    log.info("phrase timing: %d rendered, %d whole-line fallbacks, %d still infeasible, "
             "%d repaired, %.2fs of padding redistributed",
             rendered, fallbacks, infeasible, repaired,
             job.metrics["phrase_moved_seconds"])
    return None


def _record_pacing(job, paced, options, cancel) -> None:
    """Measure the pace each line is heard at; findings only in phrase mode.

    The factor is what the render did to the voiced speech, measured rather
    than taken from the plan, since a phrase plan compresses unevenly.
    """
    threshold = pacing.settings(options)["threshold_db"]
    lines = []
    for seg, source, dest in paced:
        line = pacing.measure(seg, source, 1.0, threshold, cancel)
        if dest is not None and line.voiced:
            heard = pacing.voiced_seconds(dest, threshold, cancel)
            if heard:
                line.factor = line.voiced / heard
        lines.append(line)
    pacing.record(job, lines, options)


def _repair(job, seg, plan, config, translator, regenerate, checkpoint, budget):
    """Make room for a line that does not fit, cheapest option first.

    An existing take that already fits costs nothing and is tried first. Only
    then is a bounded rewrite requested, and only against the one shared
    request budget, so a repair here cannot multiply with a quality retry.
    """
    needed = (plan.slot or 0.0) * config["max_stretch"]
    take = _eligible_take(seg, needed)
    if take is not None:
        _select(seg, take)
        job.metrics["phrase_take_repairs"] = job.metrics.get("phrase_take_repairs", 0) + 1
        return "take"
    if translator is None or regenerate is None or not hasattr(translator, "shorten"):
        return None
    if budget is not None and not budget.charge("timing_repair"):
        job.metrics["timing_repairs_refused"] = job.metrics.get("timing_repairs_refused", 0) + 1
        plan.reason = f"{plan.reason}; repair stopped: the shared request budget is exhausted"
        return None
    from ..clients.translator import TranslationError

    current = seg.text_translated or seg.text_src
    spoken = plan.speech_in or (plan.planned_duration or 0.0)
    if spoken <= 0 or not current.strip():
        return None
    char_budget = max(1, int(len(current) * needed / spoken * 0.95))
    if char_budget >= len(current):
        return None
    calls_before = getattr(translator, "provider_calls", None)
    try:
        shorter = translator.shorten(current, job.target_lang, char_budget)
    except TranslationError:
        plan.conflicts = [*plan.conflicts, {"code": "repair_failed",
                                            "detail": "the rewrite request failed"}]
        return None
    finally:
        calls_after = getattr(translator, "provider_calls", None)
        if calls_before is not None and calls_after is not None:
            job.metrics["timing_provider_calls"] = (
                job.metrics.get("timing_provider_calls", 0) + calls_after - calls_before)
        job.metrics.setdefault("timing_translation_usage", []).extend(
            getattr(translator, "last_usage", []) or [None])
    if not shorter.strip() or len(shorter) >= len(current):
        return None
    seg.text_translated = shorter
    seg.translation_provenance = {**seg.translation_provenance, "timing_rewritten": True}
    if checkpoint:
        checkpoint()
    regenerate(seg)
    job.metrics["timing_repairs"] = job.metrics.get("timing_repairs", 0) + 1
    return "rewrite"
