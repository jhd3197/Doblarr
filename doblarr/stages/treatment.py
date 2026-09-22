"""Stage — put the reviewed acoustic space or device around the dry line.

Runs after `edges`, which is deliberate and is the only ordering that works.
The dry joins are protected first, so a time-based effect rings out of a clean
edge rather than out of a click; and nothing fades the result afterwards, so
the tail is not cut off at the subtitle end. That single ordering decision is
what the `treated` role exists to express.

Three things this stage does that a plain filter call would not:

- It reads `CueAudio.upstream_of(TREATED)` — the edged, or levelled, or fitted
  derivative, whichever the run produced. Re-running with a different preset
  reprocesses the *dry* line, so two presets can never be stacked on one
  another, and turning the feature off gives the dry line back untouched.
- It measures the result and applies a bounded makeup gain, so adding
  reflections cannot quietly become a level decision the level owner never
  made. What it could not correct is recorded rather than absorbed.
- It generates no speech. A treatment change re-renders audio that already
  exists; the TTS request count across a preset change is zero, and the
  integration suite asserts it.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

from .. import treatments
from ..artifacts import digest, matches, record, stamp
from ..cues import TREATED, Artifact, Treatment
from ..errors import JobCancelled
from ..ffmpeg import FFmpegError, run_ffmpeg
from ..fingerprints import processing as processing_fingerprint
from ..levels import analyze as analyze_levels
from ..levels import layout as layout_of
from ..models import DubJob
from .quality import apply_findings

log = logging.getLogger("doblarr.treatment")


def run(job: DubJob, options: dict | None = None, cancel: threading.Event | None = None,
        dry_run: bool = False, work_dir: Path | None = None,
        program_seconds: float | None = None) -> dict:
    """Render every line's treatment, or record plainly why it has none."""
    config = treatments.settings(options)
    if dry_run:
        return {}
    edits = {str(k): dict(v) for k, v in (job.treatment_edits or {}).items()
             if isinstance(v, dict)}
    if config["mode"] != "on":
        for seg in job.segments:
            _forget(seg)
            seg.treatment = Treatment(
                outcome="bypassed", version=treatments.CATALOGUE,
                capability="supported",
                reason="acoustic treatment is off for this run")
            _observe(seg, None)
        job.metrics["treatments"] = {**treatments.summary(job), "mode": "off"}
        return job.metrics["treatments"]

    root = Path(work_dir or (job.artifacts_dir or Path("."))) / "treatments"
    rendered = 0
    for seg in job.segments:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during acoustic treatment")
        decision = treatments.decide(seg, config, edits)
        upstream = seg.audio.upstream_of(TREATED)
        if decision.preset == "dry" or decision.outcome == "unsupported":
            _forget(seg)
            seg.treatment = decision
            _observe(seg, treatments.straddles(seg, config))
            continue
        if upstream is None or not upstream.exists():
            _forget(seg)
            decision.outcome = "unavailable"
            decision.tail = 0.0
            decision.reason = "there is no rendered line to treat"
            seg.treatment = decision
            _observe(seg, None)
            continue
        source = Path(upstream.path)
        decision.dry_role = upstream.role
        try:
            dry_stats = analyze_levels(source)
            decision.dry_duration = round(dry_stats["duration"], 4)
        except (OSError, wave.Error, EOFError) as exc:
            _forget(seg)
            decision.outcome = "unavailable"
            decision.tail = 0.0
            decision.reason = f"the dry line could not be measured: {exc}"
            seg.treatment = decision
            _observe(seg, None)
            continue
        if not _render(seg, decision, upstream, root, config, cancel, dry_stats):
            _forget(seg)
            seg.treatment = decision
            _observe(seg, None)
            continue
        seg.treatment = decision
        rendered += 1
        _observe(seg, treatments.straddles(seg, config),
                 program_seconds=program_seconds)
    _transitions(job)
    summary = {**treatments.summary(job), "mode": "on",
               "default": config["default"], "rendered": rendered}
    job.metrics["treatments"] = summary
    log.info("treatments: %s on %d/%d lines (%d unsupported, longest tail %.2fs)",
             config["default"], summary["applied"], len(job.segments),
             summary["unsupported"], summary["longest_tail"])
    return summary


def _render(seg, decision, upstream, root: Path, config: dict, cancel,
            dry_stats: dict) -> bool:
    """Produce the treated derivative, correcting the level it moved.

    Two passes at most: one to hear what the effect did to the level, one to
    put it back where the preset said it should be. The second is skipped when
    the first already landed inside the deadband, so an unchanged preset on a
    warm run renders nothing at all.
    """
    source = Path(upstream.path)
    channels, rate = layout_of(source)
    tail = min(decision.tail, config["max_tail"])
    if tail < decision.tail:
        decision.reason = (f"{decision.reason}; the tail was bounded to "
                           f"{tail:.2f}s by treatments.max_tail")
        decision.tail = tail
    probe = _one_pass(seg, decision, source, root, channels, rate, 0.0, cancel)
    if probe is None:
        return False
    try:
        treated_stats = analyze_levels(probe)
    except (OSError, wave.Error, EOFError) as exc:
        decision.outcome = "failed"
        decision.tail = 0.0
        decision.reason = f"the treated line could not be measured: {exc}"
        return False
    makeup, why = treatments.makeup_for(dry_stats["speech_db"],
                                        treated_stats["speech_db"],
                                        decision.offset)
    final: Path = probe
    if makeup:
        corrected = _one_pass(seg, decision, source, root, channels, rate, makeup, cancel)
        if corrected is None:
            return False
        final = corrected
    decision.makeup = makeup
    decision.filters = treatments.chain(decision.preset, decision.intensity, makeup)
    decision.latency = 0.0  # every filter in the catalogue is IIR or a delay line
    decision.outcome = "applied"
    decision.reason = f"{decision.reason}; {why}"
    try:
        with wave.open(str(final), "rb") as audio:
            length = audio.getnframes() / audio.getframerate()
    except (OSError, wave.Error, EOFError):
        length = None
    decision.inputs = processing_fingerprint({
        "input": upstream.fingerprint,
        "preset": decision.preset, "intensity": round(decision.intensity, 4),
        "makeup": makeup, "catalogue": treatments.CATALOGUE})
    seg.audio.put_render(Artifact(
        role=TREATED, path=str(final), fingerprint=decision.inputs,
        derived_from=decision.dry_role, duration=length,
        bytes=final.stat().st_size if final.exists() else None))
    seg.audio_clip = final
    return True


def _one_pass(seg, decision, source: Path, root: Path, channels: int, rate: int,
              makeup: float, cancel) -> Path | None:
    """Render `source` through the preset once, reusing an identical render."""
    filters = treatments.chain(decision.preset, decision.intensity, makeup)
    request = {"source": stamp(source), "preset": decision.preset,
               "intensity": round(decision.intensity, 4), "makeup": round(makeup, 3),
               "catalogue": treatments.CATALOGUE, "filters": filters,
               "channels": channels, "rate": rate, "version": 1}
    dest = root / f"{source.stem}.{digest(request)[:12]}.wav"
    if matches([dest], request):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    try:
        run_ffmpeg(["-y", "-i", str(source), "-af", ",".join(filters),
                    "-ar", str(rate), "-ac", str(channels),
                    "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
    except FFmpegError as exc:
        temp.unlink(missing_ok=True)
        decision.outcome = "failed"
        decision.tail = 0.0
        decision.reason = f"{decision.preset} could not be rendered here: {exc}"
        log.warning("treatments: %s failed on line %s: %s", decision.preset,
                    seg.cue_id or seg.index, exc)
        return None
    temp.replace(dest)
    record([dest], request)
    return dest


def _forget(seg) -> None:
    """Drop a treated derivative this run will not make, and anything after it."""
    if seg.audio.render(TREATED) is None:
        return
    seg.audio.drop_renders((TREATED,))
    seg.audio.invalidate_after(TREATED)
    upstream = seg.audio.upstream_of(TREATED)
    if upstream is not None and upstream.path:
        seg.audio_clip = Path(upstream.path)


def _observe(seg, straddled, program_seconds: float | None = None) -> None:
    """Record what a reviewer would need to know about this line's treatment."""
    treatment = seg.treatment
    observed = []
    if treatment.outcome == "unsupported":
        observed.append((
            "treatment_unsupported", "delivery", "warning", None,
            {"preset": treatment.preset, "missing": list(treatment.missing),
             "note": "this treatment was asked for and not applied; the line is dry"}))
    if treatment.outcome in ("unavailable", "failed"):
        observed.append((
            "treatment_failed", "technical", "warning", None,
            {"preset": treatment.preset, "reason": treatment.reason}))
    if treatment.applied and abs(treatment.makeup) >= treatments.MAX_MAKEUP_DB - 1e-6:
        observed.append((
            "treatment_level_clamped", "performance", "warning", None,
            {"preset": treatment.preset, "makeup_db": treatment.makeup,
             "note": "the effect moved this line's level further than makeup may "
                     "correct, so it is not where the preset intended"}))
    if straddled is not None:
        observed.append((
            "treatment_scene_straddle", "timing", "info", None,
            {"scene": straddled["id"], "line_end": seg.end, "scene_end": straddled["end"],
             "note": "this line starts inside the scene rule and ends after it; the "
                     "whole line was treated as one, which is the only thing a "
                     "half-treated sentence could honestly be"}))
    if (treatment.applied and treatment.tail and program_seconds
            and seg.start + (treatment.dry_duration or 0.0) + treatment.tail
            > program_seconds):
        observed.append((
            "treatment_tail_clipped", "delivery", "info", None,
            {"preset": treatment.preset, "tail": treatment.tail,
             "program_seconds": round(program_seconds, 3),
             "note": "the tail runs past the end of the programme and the mix "
                     "will cut it there rather than extend delivery"}))
    apply_findings(seg, treatments.DETECTOR,
                   f"{treatment.preset}/{treatment.intensity:.2f}/{treatment.outcome}",
                   observed)


def _transitions(job) -> None:
    """Flag joins where the space changes between two lines that nearly touch.

    Not a defect and not corrected automatically: a cut from a phone to the
    room is a real edit somebody may have meant. It is a join worth hearing,
    so it is offered as an info finding on the second line rather than
    smoothed away behind the reviewer's back.
    """
    ordered = sorted(job.segments, key=lambda s: (s.start, s.index))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        observed = []
        gap = current.start - previous.end
        changed = previous.treatment.preset != current.treatment.preset
        if changed and gap <= treatments.TRANSITION_GAP:
            observed.append((
                "treatment_transition", "delivery", "info", None,
                {"from": previous.treatment.preset, "to": current.treatment.preset,
                 "gap": round(gap, 3), "with_cue": previous.cue_id,
                 "tail": previous.treatment.tail,
                 "note": "the space changes between these two lines; the earlier "
                         "tail is kept and rings into the gap rather than being "
                         "cut at the subtitle end"}))
        apply_findings(current, f"{treatments.DETECTOR}-join",
                       f"{previous.treatment.preset}>{current.treatment.preset}"
                       f"@{gap:.3f}", observed)
