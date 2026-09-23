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


def _register_fit(seg, dest: Path, actual: float, factor: float) -> None:
    """Record the time-fitted derivative and the immutable input it came from."""
    upstream = seg.audio.upstream_of(FITTED)
    request = {"input": upstream.fingerprint if upstream else "",
               "factor": round(factor, 4), "slot": seg.duration, "version": 1}
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


def _record_pacing(job, measured, takes, options, cancel) -> None:
    """How fast each line is heard, next to its character's other lines."""
    config = pacing.settings(options)
    lines = [pacing.measure(s, takes[id(s)], factor, config["threshold_db"], cancel)
             for s, _actual, factor in measured]
    pacing.record(job, lines, options)


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
) -> Plan | None:
    if not enabled:
        log.info("fit_timing disabled")
        return None
    log.info("fit_timing over %d clips (max stretch %.2fx)", len(job.segments), MAX_STRETCH)
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
    for s, src in clips:
        s.issues = [i for i in s.issues if i not in {"timing_overflow", "timing_repair_failed"}]
        if s.duration <= 0:
            continue
        actual = _duration(src, cancel=cancel)
        if translator is not None and regenerate is not None and hasattr(translator, "shorten"):
            from ..clients.translator import TranslationError

            for _ in range(max(0, min(5, int(max_attempts)))):
                if actual <= s.duration * 1.15:
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
        takes[id(s)] = src
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
    _record_pacing(job, measured, takes, options, cancel)

    if not plan:
        log.info("fit_timing: every clip fits its slot")
        return None

    by_start = sorted(job.segments, key=lambda t: t.start)
    job.metrics["timing_flags"] = sum("timing_overflow" in s.issues for s in job.segments)
    job.metrics["stretched_lines"] = len(plan)
    job.metrics["max_stretch"] = round(max(factor for *_row, factor in plan), 4)
    for s, src, dest, actual, factor in plan:
        if cached(dest, src, force):
            _register_fit(s, dest, actual, factor)
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
        _register_fit(s, dest, actual, factor)
        if actual > s.duration * MAX_STRETCH * FIT_SLACK:
            over = actual / MAX_STRETCH - s.duration
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
                MAX_STRETCH,
                spill,
            )
        else:
            log.info(
                "  line %d: %.2fs -> %.2fs (atempo %.2f)", s.index, actual, actual / factor, factor
            )
        s.audio_clip = dest
    return None
