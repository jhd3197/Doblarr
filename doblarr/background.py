"""Is the bed under the dub still the original's world, and only that?

Demucs produces an *estimate* of the music-and-effects stem, not a studio M&E.
Two things go wrong with it and they go wrong in opposite directions: a sound
that belonged to the scene is pulled out with the voices and disappears, or a
piece of the original performance stays behind and is heard under the dub in
the wrong language.

This module looks for both, timecodes what it finds, and is careful about how
much it claims:

- **Missing effects** are inferred by comparing the original and the separated
  bed over a known event's window. A big drop is evidence the separator took
  the sound; it is not proof, because a quiet door in a loud scene measures the
  same way.
- **Leakage** is screened with recognition, on purpose, only in windows where
  nobody is scheduled to speak. Music vocals, reverberation and cross-talk all
  produce words there. A hit is a suspicion with a confidence attached, and
  recognition hearing nothing is never recorded as a clean bed.
- **No automatic gating.** Nothing here reaches for a noise gate or a ducking
  curve. Removing suspected leakage by processing the bed would damage the
  scene to hide a problem; the answer is a patch a person supplies, placed as
  an ordinary coverage event, or a note for the mix.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from . import levels
from .cues import Finding, Span, finding_id
from .errors import JobCancelled
from .fingerprints import verification as screening_fingerprint
from .stages.quality import apply_findings

log = logging.getLogger("doblarr.background")

DETECTOR = "background-integrity/1"
# How far the original has to stand over the separated bed before the drop is
# worth reporting as a possibly-removed effect.
MISSING_DB = 12.0
# The shortest window with no scheduled dialogue that is worth screening.
MIN_QUIET = 1.2
# Never screen more than this many windows in one run: the point is a sample a
# reviewer can act on, not a full second transcription pass over the episode.
MAX_SCREENED = 12


def kind(job) -> dict:
    """What the bed under this dub actually is, said plainly.

    An original-mix fallback is a different product from a separated bed — the
    original performance is still in it, at a reduced level — and every report
    that mentions the background has to say which one it had.
    """
    separated = bool(job.background and job.background != job.source_audio
                     and Path(job.background).is_file())
    return {
        "separated": separated,
        "label": "estimated background (separated)" if separated
                 else "original mix, reduced (no separation ran)",
        "note": ("Demucs output is an estimate of the music and effects, not a "
                 "studio M&E stem." if separated else
                 "Separation did not run, so the original dialogue is still in the "
                 "bed under the dub at a reduced level."),
    }


def _quiet_windows(job) -> list[Span]:
    """Stretches of the dub where no line is scheduled to speak."""
    from .cues import TARGET

    ordered = sorted(job.segments, key=lambda s: (s.start, s.index))
    windows: list[Span] = []
    cursor = 0.0
    for seg in ordered:
        if seg.start - cursor >= MIN_QUIET:
            windows.append(Span(round(cursor, 3), round(seg.start, 3), TARGET))
        cursor = max(cursor, seg.end)
    return windows


def _nearest(job, span: Span):
    """The cue a timecoded finding about `span` belongs on."""
    if not job.segments:
        return None
    return min(job.segments, key=lambda s: abs(s.start - span.start))


def check(job, options: dict | None = None, cancel=None, work_dir: Path | None = None,
          vb=None, budget=None, dry_run: bool = False) -> dict:
    """Report background integrity. Changes no audio, ever."""
    from . import reactions

    config = reactions.settings(options)
    bed_kind = kind(job)
    summary = {**bed_kind, "missing": 0, "leaks": 0, "screened": 0,
               "windows": len(_quiet_windows(job))}
    if dry_run or config["mode"] == "off":
        job.metrics["background"] = summary
        return summary
    root = Path(work_dir or (job.artifacts_dir or Path("."))) / "background"

    missing = _missing_effects(job, bed_kind)
    leaks, screened = _leakage(job, config, root, vb, budget, cancel, bed_kind)
    summary["missing"] = missing
    summary["leaks"] = leaks
    summary["screened"] = screened
    job.metrics["background"] = summary
    if missing or leaks:
        log.info("background: %d possibly-removed effect(s), %d suspected leak(s) "
                 "in %d screened window(s)", missing, leaks, screened)
    return summary


def _missing_effects(job, bed_kind: dict) -> int:
    """Flag events whose sound the separator appears to have taken with it."""
    from .reactions import DETECTOR as COVERAGE

    found = 0
    for event in job.nonverbal:
        checks = event.checks or {}
        source_db, bed_db = checks.get("source_db"), checks.get("bed_db")
        event.findings = [f for f in event.findings if f.code != "background_missing_effect"]
        if not bed_kind["separated"] or source_db is None or bed_db is None:
            continue
        drop = round(source_db - bed_db, 2)
        if drop < MISSING_DB:
            continue
        found += 1
        event.findings.append(Finding(
            finding_id=finding_id(event.event_id, "background_missing_effect", COVERAGE),
            code="background_missing_effect", kind="delivery", severity="info",
            confidence=None, scope="event", target=event.event_id,
            span=event.span, detector=DETECTOR, inputs=event.inputs or event.event_id,
            evidence={"source_db": source_db, "bed_db": bed_db, "drop_db": drop,
                      "type": event.type, "text": event.text,
                      "note": "the original is much louder here than the separated bed, "
                              "which is what a removed effect looks like — and also what "
                              "a quiet sound in a loud scene looks like"}))
    return found


def _leakage(job, config: dict, root: Path, vb, budget, cancel, bed_kind: dict):
    """Screen dialogue-free windows of the bed for original-language words."""
    observations: dict[int, list] = {}
    inputs: dict[int, str] = {}
    screened = leaks = 0
    bed = Path(job.background) if bed_kind["separated"] and job.background else None
    windows = _quiet_windows(job)[:MAX_SCREENED] if bed is not None else []
    language = job.script_lang or job.source_lang
    for span in windows:
        assert bed is not None       # `windows` is empty without one
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during background screening")
        seg = _nearest(job, span)
        if seg is None:
            continue
        key = screening_fingerprint({"bed": str(bed), "window": [span.start, span.end],
                                     "language": language, "detector": DETECTOR})
        try:
            clip = levels.extract_window(
                bed, span.start, min(span.end, span.start + 8.0),
                root / f"quiet-{span.start:.0f}.wav", cancel)
            stats = levels.analyze(clip)
        except (OSError, wave.Error, EOFError):
            continue
        if stats["speech_db"] is None or stats["speech_db"] < -55:
            continue  # nothing audible here at all; there is nothing to screen
        if not config["leakage_check"] or vb is None:
            continue
        if budget is not None and not budget.charge("background_screen"):
            break
        screened += 1
        try:
            heard = vb.transcribe(clip, language=language) or {}
        except Exception as exc:  # noqa: BLE001 - a screening failure is reviewable
            observations.setdefault(seg.index, []).append((
                "background_screen_failed", "technical", "info", None,
                {"window": [span.start, span.end], "error": str(exc)}))
            inputs[seg.index] = key
            continue
        text = str((heard or {}).get("text") or "").strip()
        inputs[seg.index] = key
        if not text:
            continue
        leaks += 1
        observations.setdefault(seg.index, []).append((
            "background_leakage", "content", "warning", _confidence(heard),
            {"window": [span.start, span.end], "heard": text, "language": language,
             "bed_db": stats["speech_db"],
             "note": "recognition heard source-language words in the bed where no line "
                     "is scheduled. Music vocals, reverberation and cross-talk produce "
                     "this too — listen before concluding the separation leaked."}))
    for seg in job.segments:
        apply_findings(seg, DETECTOR, inputs.get(seg.index, "not screened"),
                       observations.get(seg.index, []))
    return leaks, screened


def _confidence(heard: dict) -> float | None:
    value = (heard or {}).get("confidence")
    return float(value) if isinstance(value, int | float) else None
