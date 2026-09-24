"""Stage 7 — fit each generated clip into its original time slot (isochrony).

Strategy (cheapest first):
  1. If translation already fits, leave it (short clips too — the mix pads the
     rest of the slot with the background bed).
  2. If long, time-compress with ffmpeg atempo (no pitch change), clamped to
     MAX_STRETCH so the voice still sounds natural.
  3. If still long after max stretch, keep the clamped clip and log a warning —
     the mix places clips by start time, so the overlap is audible and must be
     visible in the logs.
This is the single biggest driver of perceived dub quality.

With `timing.pacing: speaker` (the default) lines are not fitted one by one:
each character's lines in a scene share a base compression and stay within
`timing.pace_local_range` of it, so neighbouring lines do not jump between
natural speed and 1.3x (see `_steady_factors` and `doblarr.pacing`). The
ceiling is then `timing.max_stretch`. `pacing: off` is the rule above exactly,
MAX_STRETCH included.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

from .. import pacing
from ..cues import FITTED, Artifact
from ..ffmpeg import run_ffmpeg, run_ffprobe
from ..fingerprints import processing as processing_fingerprint
from ..models import DubJob, Segment
from .common import Plan, cached, dry, stage
from .quality import apply_findings

log = logging.getLogger("doblarr.fit_timing")

# Stretch beyond this factor sounds unnatural; prefer re-translation instead.
MAX_STRETCH = 1.3
# Overflow within this ratio is inaudible once mixed; don't resample for it.
FIT_SLACK = 1.02


def _duration(path: Path, cancel: threading.Event | None = None) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError, OSError):
        pass
    out = run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        cancel=cancel,
    )
    return float(out.strip())


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5-2.0; chain filters for larger corrections."""
    parts = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


DETECTOR = "fit-timing/1"


def stand_down(job, reason: str) -> None:
    """Drop this owner's derivative for a run the phrase owner is fitting.

    Whole-clip fitting and phrase fitting are the two timing owners and only
    one runs. A `fitted` file left behind by a previous whole-clip run is not a
    derivative of anything this run produced, so keeping it would feed the mix
    audio that was stretched from an input nothing else is reading any more.
    """
    for seg in job.segments:
        if seg.audio.render(FITTED) is None:
            continue
        seg.audio.drop_renders((FITTED,))
        seg.audio.invalidate_after(FITTED)
        upstream = seg.audio.upstream_of(FITTED)
        if upstream is not None and upstream.path:
            seg.audio_clip = Path(upstream.path)
    for seg in job.segments:
        # Retire this owner's findings: an overflow measured against a
        # whole-clip fit says nothing about the phrase recipe now in use. The
        # legacy issue strings belong to whichever owner *did* run, so they are
        # left exactly as the phrase owner set them.
        apply_findings(seg, DETECTOR, "bypassed", [])
    log.info("fit_timing: %s", reason)


def _register_fit(seg, dest: Path, actual: float, factor: float,
                  policy: dict | None = None) -> None:
    """Record the time-fitted derivative and the immutable input it came from.

    `policy` is the pacing that chose the factor. It is absent with pacing off,
    so those fingerprints stay exactly what earlier releases recorded.
    """
    upstream = seg.audio.upstream_of(FITTED)
    request = {"input": upstream.fingerprint if upstream else "",
               "factor": round(factor, 4), "slot": seg.duration, "version": 1}
    if policy is not None:
        request["pacing"] = policy
    seg.audio.put_render(Artifact(
        role=FITTED,
        path=str(dest),
        fingerprint=processing_fingerprint(request),
        derived_from=upstream.role if upstream else "",
        duration=actual / factor if factor else actual,
        bytes=dest.stat().st_size if dest.exists() else None,
    ))


def _record_timing_findings(seg, actual: float, factor: float) -> None:
    observed = []
    for code in ("timing_overflow", "timing_repair_failed"):
        if code in seg.issues:
            observed.append((code, "timing", "warning", None,
                             {"clip_seconds": actual, "slot_seconds": seg.duration,
                              "stretch": round(factor, 4)}))
    apply_findings(seg, DETECTOR, f"{actual:.4f}/{seg.duration:.4f}/{factor:.4f}", observed)


def _record_pacing(job, measured, takes, options, cancel, paced=None, bases=None) -> None:
    """How fast each line is heard, next to its character's other lines."""
    config = pacing.settings(options)
    paced = paced or {}
    lines = []
    for s, _actual, factor in measured:
        line = paced.get(id(s)) or pacing.measure(s, takes[id(s)], 1.0,
                                                  config["threshold_db"], cancel)
        line.factor = factor
        lines.append(line)
    pacing.record(job, lines, options, bases)


def _policy(config: dict, base: float | None) -> dict:
    """The pacing inputs behind a factor, for the fit fingerprint."""
    return {"mode": config["pacing"], "base": round(base or 1.0, 4),
            **{k: round(config[k], 4) for k in (
                "max_stretch", "min_stretch", "pace_tolerance", "pace_local_range",
                "pace_max_speedup", "pace_scene_gap")}}


def _base(group: list[tuple[Segment, Path, float]], max_stretch: float) -> float:
    """The one compression that fits a pace group into its slots in total.

    Each take counts for at most what the ceiling can fit, so one line far over
    its slot cannot drag its neighbours up to the ceiling with it. Never below
    1.0: the base never slows speech down.
    """
    slots = sum(s.duration for s, _src, _actual in group)
    if slots <= 0:
        return 1.0
    needed = sum(min(actual, s.duration * max_stretch) for s, _src, actual in group)
    return min(max(needed / slots, 1.0), max_stretch)


def _steady_factors(entries, config: dict, max_stretch: float, repair, cancel):
    """Factors that keep each character's lines at one pace.

    Per pace group (one speaker, one scene) there is a base compression, and
    each line stays within `pace_local_range` of it, except that a line is
    always compressed as far as it needs to stop overflowing, up to the
    ceiling. A line that needs more than the group allows is rewritten first
    when a translator can do it, and the base is recomputed once after all the
    repairs. A take slower than its group by more than `pace_tolerance` is
    sped toward the group, which only ever shortens it.
    """
    spread = config["pace_local_range"]
    rows = {id(s): (s, src, actual) for s, src, actual in entries}
    grouped = pacing.groups([s for s, _src, _actual in entries], config["pace_scene_gap"])
    if repair is not None:
        for segs in grouped.values():
            ceiling = max(_base([rows[id(s)] for s in segs], max_stretch) + spread, FIT_SLACK)
            for seg in segs:
                s, src, actual = rows[id(seg)]
                if actual > s.duration * ceiling:
                    rows[id(seg)] = (s, *repair(s, src, actual, ceiling))
    factors: dict[int, float] = {}
    paced: dict[int, pacing.Line] = {}
    bases: dict[str, float] = {}
    for name, segs in grouped.items():
        members = [rows[id(s)] for s in segs]
        base = bases[name] = _base(members, max_stretch)
        lines = []
        for s, src, _actual in members:
            line = pacing.measure(s, src, 1.0, config["threshold_db"], cancel)
            line.group = name
            paced[id(s)] = line
            lines.append(line)
        median = pacing.median_rate(lines, effective=False)
        high = base + spread
        for (s, _src, actual), line in zip(members, lines, strict=True):
            need = actual / s.duration
            if 1.0 < need <= FIT_SLACK:
                need = 1.0  # inaudible once mixed; not worth a resample
            factor = min(max(need, base - spread, config["min_stretch"]), high)
            factor = max(factor, min(need, max_stretch))  # never overflow for the pace
            rate = line.natural_rate
            if rate and median and rate < (1 - config["pace_tolerance"]) * median:
                factor = max(factor, min(median / rate, config["pace_max_speedup"], high))
            factor = round(min(factor, max_stretch), 4)
            factors[id(s)] = 1.0 if abs(factor - 1.0) < 0.005 else factor
    return [rows[id(s)] for s, _src, _actual in entries], factors, paced, bases


def _repair(job, s, src, actual, ceiling, translator, regenerate, checkpoint,
            max_attempts, budget, cancel, accept_rewrite=None) -> tuple[Path, float]:
    """Shorten and regenerate a line while it needs more than `ceiling` x its slot."""
    from ..clients.translator import TranslationError

    for _ in range(max(0, min(5, int(max_attempts)))):
        if actual <= s.duration * ceiling:
            break
        # One charge covers the rewrite request and the regeneration it
        # triggers; the budget is shared with quality retries.
        if budget is not None and not budget.charge("timing_repair"):
            job.metrics["timing_repairs_refused"] = (
                job.metrics.get("timing_repairs_refused", 0) + 1)
            break
        current = s.text_translated or s.text_src
        char_budget = max(1, int(len(current) * s.duration / actual * 0.95))
        try:
            calls_before = getattr(translator, "provider_calls", None)
            shorter = translator.shorten(current, job.target_lang, char_budget)
        except TranslationError:
            s.issues.append("timing_repair_failed")
            break
        finally:
            calls_after = getattr(translator, "provider_calls", None)
            if calls_before is not None and calls_after is not None:
                job.metrics["timing_provider_calls"] = (
                    job.metrics.get("timing_provider_calls", 0) + calls_after - calls_before
                )
            job.metrics.setdefault("timing_translation_usage", []).extend(
                getattr(translator, "last_usage", []) or [None]
            )
        if not shorter.strip() or len(shorter) >= len(current):
            break
        if accept_rewrite is not None and not accept_rewrite(s, current, shorter):
            break  # keep the words; paying to regenerate a wrong line helps no one
        s.text_translated = shorter
        s.translation_provenance = {**s.translation_provenance, "timing_rewritten": True}
        if checkpoint:
            checkpoint()
        regenerate(s)
        job.metrics["timing_repairs"] = job.metrics.get("timing_repairs", 0) + 1
        regenerated = s.audio.upstream_of(FITTED)
        if regenerated is not None and regenerated.exists():
            src = Path(regenerated.path)
        elif s.audio_clip is not None:
            src = Path(s.audio_clip)
        else:
            raise RuntimeError("timing repair did not produce an audio clip")
        actual = _duration(src, cancel=cancel)
    return src, actual


@stage("fit_timing")
def run(
    job: DubJob,
    work_dir: Path,
    enabled: bool = True,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    force: bool = False,
    translator=None,
    regenerate=None,
    checkpoint=None,
    max_attempts=2,
    budget=None,
    options: dict | None = None,
    accept_rewrite=None,
) -> Plan | None:
    if not enabled:
        log.info("fit_timing disabled")
        return None
    config = pacing.settings(options)
    # `pacing: off` is the earlier behaviour exactly, constant ceiling included.
    steady = config["pacing"] == "speaker"
    max_stretch = config["max_stretch"] if steady else MAX_STRETCH
    log.info("fit_timing over %d clips (max stretch %.2fx, pacing %s)",
             len(job.segments), max_stretch, config["pacing"])
    if dry_run:
        return dry("would measure each clip vs slot and time-stretch to fit")

    # Read the declared upstream artifact, not the clip projection: re-running
    # with a different slot must re-stretch the prepared take rather than the
    # previously stretched file.
    clips = []
    for s in job.segments:
        upstream = s.audio.upstream_of(FITTED)
        source = (Path(upstream.path) if upstream and upstream.exists()
                  else Path(s.audio_clip) if s.audio_clip and Path(s.audio_clip).exists()
                  else None)
        if source is not None:
            clips.append((s, source))
    if not clips:
        log.info("fit_timing: no generated clips to fit")
        return None

    plan: list[tuple[Segment, Path, Path, float, float]] = []  # seg, src, dest, actual, factor
    measured: list[tuple[Segment, float, float]] = []     # seg, actual, factor
    takes: dict[int, Path] = {}                            # the take each factor applies to
    can_repair = (translator is not None and regenerate is not None
                  and hasattr(translator, "shorten"))

    def repair(s, src, actual, ceiling):
        return _repair(job, s, src, actual, ceiling, translator, regenerate, checkpoint,
                       max_attempts, budget, cancel, accept_rewrite)

    entries: list[tuple[Segment, Path, float]] = []
    for s, src in clips:
        s.issues = [i for i in s.issues if i not in {"timing_overflow", "timing_repair_failed"}]
        if s.duration <= 0:
            continue
        actual = _duration(src, cancel=cancel)
        if can_repair and not steady:
            src, actual = repair(s, src, actual, 1.15)
        entries.append((s, src, actual))

    paced: dict[int, pacing.Line] = {}
    bases: dict[str, float] = {}
    factors: dict[int, float] = {}
    if steady:
        entries, factors, paced, bases = _steady_factors(
            entries, config, max_stretch, repair if can_repair else None, cancel)

    for s, src, actual in entries:
        takes[id(s)] = src
        if steady:
            factor = factors[id(s)]
            if factor == 1.0:
                if actual > s.duration * FIT_SLACK:
                    s.issues.append("timing_overflow")
                measured.append((s, actual, 1.0))
                continue
        else:
            if actual <= s.duration * FIT_SLACK:
                measured.append((s, actual, 1.0))
                continue
            factor = min(actual / s.duration, MAX_STRETCH)
        if actual / factor > s.duration * FIT_SLACK:
            s.issues.append("timing_overflow")
        dest = src.parent / "fit" / f"{src.stem}.{factor:.4f}.wav"
        measured.append((s, actual, factor))
        plan.append((s, src, dest, actual, factor))

    # Every measured clip updates its findings, so a cue that now fits retires
    # its old overflow instead of leaving a stale one open.
    for s, actual, factor in measured:
        _record_timing_findings(s, actual, factor)
    _record_pacing(job, measured, takes, options, cancel, paced, bases)

    if not plan:
        log.info("fit_timing: every clip fits its slot")
        return None

    by_start = sorted(job.segments, key=lambda t: t.start)
    job.metrics["timing_flags"] = sum("timing_overflow" in s.issues for s in job.segments)
    job.metrics["stretched_lines"] = len(plan)
    job.metrics["max_stretch"] = round(max(factor for *_row, factor in plan), 4)
    for s, src, dest, actual, factor in plan:
        policy = _policy(config, bases.get(paced[id(s)].group)) if steady else None
        if cached(dest, src, force):
            _register_fit(s, dest, actual, factor, policy)
            s.audio_clip = dest
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".partial.wav")
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(src),
                "-af",
                _atempo_chain(factor),
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(temp),
            ],
            cancel=cancel,
        )
        temp.replace(dest)
        _register_fit(s, dest, actual, factor, policy)
        if actual > s.duration * max_stretch * FIT_SLACK:
            over = actual / max_stretch - s.duration
            nxt = next((t for t in by_start if t.start >= s.end), None)
            spill = (
                f"; overlaps line {nxt.index} at {nxt.start:.2f}s"
                if nxt
                else "; runs past the end of its slot"
            )
            log.warning(
                "line %d: %.2fs clip in %.2fs slot — %.2fs over even at max %.2fx stretch%s",
                s.index,
                actual,
                s.duration,
                over,
                max_stretch,
                spill,
            )
        else:
            log.info(
                "  line %d: %.2fs -> %.2fs (atempo %.2f)", s.index, actual, actual / factor, factor
            )
        s.audio_clip = dest
    return None
