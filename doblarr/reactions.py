"""Breaths, laughs, gasps and doors: keep them accounted for, not guessed at.

A dub loses more than words. The subtitle cleanup that stops the engine saying
"[gasps]" out loud also removes the only record that a gasp was there, and
Demucs pulls vocal reactions out of the background along with the dialogue. The
result is a scene where somebody visibly laughs and nothing is heard.

This module keeps a ledger instead of a guess. Every non-spoken cue is an event
with four possible answers — retain the original sound, replace it with a
supplied one, omit it on purpose, or leave it unresolved — and *unresolved is
the default and stays the default*. Nothing is ever inserted because a subtitle
said something happened: a tag is a label somebody typed, and synthesising a
laugh from it would be inventing a performance.

Three rules the code enforces rather than documents:

- **Retention needs a clean window.** A reaction is only cut from the original
  when its cue was non-spoken end to end. A tag sitting beside dialogue
  (`[laughs] No puedo creerlo.`) has no known position inside the line, and
  cutting the line's audio to get the laugh would drag the original actor's
  voice back into the dub.
- **A sound plays once.** Before an event is placed, its target window is
  checked against every event already placed. Switching from retain to replace
  drops the previous artifact; it never layers the two.
- **Capability is asked, never assumed.** An engine that accepts a text field
  has not thereby demonstrated it can laugh. Unsupported generation is recorded
  as asked-for and unsupported, and the event stays a visible gap.
"""

from __future__ import annotations

import logging
import math
import wave
from pathlib import Path

from .artifacts import digest, matches, record, stamp
from .cues import (
    SOURCE,
    TARGET,
    UNKNOWN,
    Artifact,
    NonverbalEvent,
    Span,
    event_id,
    now,
)
from .errors import JobCancelled
from .ffmpeg import FFmpegError, run_ffmpeg
from .fingerprints import processing as processing_fingerprint
from .fingerprints import verification as screening_fingerprint

log = logging.getLogger("doblarr.reactions")

DETECTOR = "reaction-coverage/1"
# An event longer than this is not a reaction, it is a scene. Retention is
# bounded so a mis-timed cue cannot paste a minute of the original back in.
MAX_EVENT_SECONDS = 6.0
# How far a separated background has to stand above its own floor before the
# sound is called still present in the bed. Below it, "maybe" is the answer.
PRESENT_DB = 8.0


def settings(options: dict | None) -> dict:
    """Normalize the `coverage` config section. Everything defaults to off."""
    values = dict(options or {})
    mode = str(values.get("mode", "off") or "off")
    if mode not in ("off", "review", "retain"):
        raise ValueError("coverage.mode must be off, review or retain")
    # `review` prepares the audio and leaves it out of the mix; `retain`
    # places it. Both build the same ledger, so the difference is one
    # boolean on each event rather than two code paths.
    return {
        "mode": mode,
        "gain_db": float(values.get("gain_db", 0.0)),
        "handle": max(0.0, float(values.get("handle_ms", 80)) / 1000),
        "fade": max(0.0, float(values.get("fade_ms", 25)) / 1000),
        "max_seconds": max(0.1, min(MAX_EVENT_SECONDS,
                                    float(values.get("max_seconds", 4.0)))),
        "leakage_check": bool(values.get("leakage_check", False)),
        "generate": bool(values.get("generate", False)),
        "in_mix": mode == "retain",
        "events": dict(values.get("events") or {}),
        "assets": dict(values.get("assets") or {}),
        "extra": list(values.get("extra") or []),
    }


def enabled(options: dict | None) -> bool:
    try:
        return settings(options)["mode"] != "off"
    except ValueError:
        return False


def engine_capability(engine: str, client=None) -> str:
    """Whether this engine can actually be asked to produce a nonverbal sound.

    Mirrors `performance.capability`: the adapter is asked, and an adapter that
    cannot answer leaves `unknown`, which is different from `unsupported` and
    very different from `supported`.
    """
    if client is None:
        return "unknown"
    for attribute in ("supports_nonverbal", "nonverbal_engines", "capabilities"):
        value = getattr(client, attribute, None)
        if value is None:
            continue
        try:
            resolved = value(engine) if callable(value) else value
        except Exception:  # noqa: BLE001 - a probe must never break a run
            return "unknown"
        if isinstance(resolved, bool):
            return "supported" if resolved else "unsupported"
        if isinstance(resolved, dict):
            found = resolved.get("nonverbal")
            if found is not None:
                return "supported" if found else "unsupported"
        if isinstance(resolved, list | tuple | set):
            return "supported" if engine in resolved else "unsupported"
    return "unknown"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

def inventory(job, options: dict | None = None) -> list[NonverbalEvent]:
    """Refresh the event ledger: human additions, placement and applied decisions.

    Does not touch audio. Called before the coverage pass so review can see the
    inventory even in a dry run, and so a decision recorded against an event
    survives a rerun that re-derived the same event id.
    """
    config = settings(options)
    known = {event.event_id for event in job.nonverbal}
    for index, raw in enumerate(config["extra"]):
        if not isinstance(raw, dict):
            continue
        event = _manual(raw, index)
        if event is None or event.event_id in known:
            continue
        job.nonverbal.append(event)
        known.add(event.event_id)
    by_cue = {seg.cue_id: seg for seg in job.segments}
    for event in job.nonverbal:
        _place(event, by_cue)
        _apply_decision(event, config)
    job.metrics["nonverbal_events"] = len(job.nonverbal)
    return job.nonverbal


def _manual(raw: dict, index: int) -> NonverbalEvent | None:
    """A missing event somebody added by hand, with its own stable identity."""
    first, last = raw.get("start"), raw.get("end")
    if first is None or last is None:
        return None
    try:
        start, end = float(first), float(last)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(start) and math.isfinite(end) and end > start >= 0):
        return None
    kind = str(raw.get("type") or "unknown")
    from .stages.prepare import classify

    parsed, category = classify(kind)
    return NonverbalEvent(
        event_id=str(raw.get("event_id") or event_id("manual", index)),
        cue_id=str(raw.get("cue") or ""),
        type=parsed,
        category=str(raw.get("category") or category),
        speaker=str(raw["speaker"]) if raw.get("speaker") else None,
        text=str(raw.get("note") or kind),
        source=[Span(start, end, SOURCE)],
        evidence="manual",
        origin="manual",
        checks={"position": "whole", "parsed": parsed != "unknown", "cue_text": ""},
        at=now(),
    )


def _place(event: NonverbalEvent, by_cue: dict) -> None:
    """Where this event would be heard in the dub.

    The dub keeps the original timeline, so an event's target window is its
    source window shifted by whatever placement offset the cue it belongs to
    carries. A cue that was removed from synthesis has no offset, which is the
    common case and means no shift at all.
    """
    span = event.span
    if span is None:
        event.target = None
        return
    seg = by_cue.get(event.cue_id)
    if seg is not None and seg.placement.montage is not None:
        # An audition montage has its own concatenated timeline. Source time
        # does not map onto it, and placing a reaction by source seconds would
        # drop it in the middle of a different line.
        event.target = None
        event.checks = {**event.checks, "mapping": "montage"}
        return
    offset = seg.placement.offset if seg is not None else 0.0
    start = max(0.0, span.start + offset)
    end = start + min(span.duration, MAX_EVENT_SECONDS)
    event.target = Span(round(start, 4), round(end, 4), TARGET)
    event.checks = {**event.checks, "offset": round(offset, 4)}


def _apply_decision(event: NonverbalEvent, config: dict) -> None:
    """Fold the configured/reviewed decision for this event into the record."""
    entry = config["events"].get(event.event_id)
    if isinstance(entry, str):
        entry = {"decision": entry}
    if not isinstance(entry, dict):
        if config["mode"] == "off":
            event.decision = "unresolved"
        return
    wanted = str(entry.get("decision") or "").strip()
    if wanted in ("retain", "replace", "omit", "covered", "unresolved"):
        event.decision = wanted
    if entry.get("gain_db") is not None:
        event.gain_db = float(entry["gain_db"])
    if entry.get("type"):
        from .stages.prepare import classify

        parsed, category = classify(str(entry["type"]))
        if parsed != "unknown":
            event.type, event.category = parsed, category
    asset = config["assets"].get(event.event_id) or entry.get("asset")
    if asset:
        event.asset = str(asset)


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------

def process(job, options: dict | None = None, cancel=None, dry_run: bool = False,
            vb=None, budget=None, engine: str = "", work_dir: Path | None = None) -> dict:
    """Resolve every event's coverage and render the audio a decision asks for."""
    config = settings(options)
    inventory(job, options)
    if dry_run:
        return {}
    if config["mode"] == "off":
        for event in job.nonverbal:
            _release(event)
            event.coverage = "unresolved"
            event.reason = "reaction coverage is off for this run"
            event.findings = []
        job.metrics["coverage"] = summary(job)
        return job.metrics["coverage"]

    if job.kind == "audition":
        # Same reason as the montage guard in `_place`: an audition's target
        # time is montage time, so nothing here can be placed correctly.
        for event in job.nonverbal:
            _release(event)
            event.coverage = "unresolved"
            event.reason = ("an audition montage has its own timeline, so source "
                            "reactions cannot be placed in it")
            event.findings = []
        job.metrics["coverage"] = summary(job)
        return job.metrics["coverage"]
    root = Path(work_dir or (job.artifacts_dir or Path("."))) / "reactions"
    capability = engine_capability(engine, vb)
    placed: list[NonverbalEvent] = []
    for event in sorted(job.nonverbal, key=lambda e: (e.target.start if e.target else 0.0)):
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during reaction coverage")
        _evidence(job, event, cancel, root)
        _resolve(job, event, config, root, placed, capability, vb, budget, cancel)
        if event.rendered:
            placed.append(event)
    job.metrics["coverage"] = summary(job)
    log.info("coverage: %d event(s) — %s", len(job.nonverbal),
             ", ".join(f"{state} {count}" for state, count
                       in sorted(job.metrics["coverage"]["states"].items())))
    return job.metrics["coverage"]


def _release(event: NonverbalEvent) -> None:
    """Drop the audio a previous decision produced, so nothing is layered."""
    event.artifact = None
    event.inputs = ""
    event.in_mix = False


def _resolve(job, event, config, root, placed, capability, vb, budget, cancel) -> None:
    """Turn one event's decision into either audio or an explicit visible gap."""
    event.findings = []
    if event.decision == "omit":
        _release(event)
        event.coverage = "omitted"
        event.reason = "left out on purpose"
        return
    if event.decision == "covered" or (
            event.decision == "unresolved" and event.checks.get("still_in_bed")):
        _release(event)
        event.coverage = "covered"
        event.reason = event.reason or (
            "the separated background still carries this sound, so nothing is inserted")
        return
    if event.decision == "unresolved":
        _release(event)
        event.coverage = "unresolved"
        event.reason = ("no coverage decision has been made for this event"
                        if config["mode"] != "off" else event.reason)
        _finding(event, "reaction_uncovered", "delivery", "info",
                 {"type": event.type, "category": event.category,
                  "text": event.text, "evidence": event.evidence,
                  "note": "a subtitle tag is not proof the sound is missing, "
                          "and not proof it is there"})
        return
    if event.target is None:
        _release(event)
        event.coverage = "unavailable"
        event.reason = "this event has no recorded time, so it cannot be placed"
        return
    duplicate = _duplicate(event, placed)
    if duplicate is not None:
        _release(event)
        event.coverage = "unresolved"
        event.reason = (f"another event ({duplicate.event_id}) already covers this moment; "
                        "placing both would play the sound twice")
        _finding(event, "reaction_duplicate", "delivery", "warning",
                 {"with_event": duplicate.event_id, "with_type": duplicate.type,
                  "target": [event.target.start, event.target.end]})
        return
    if event.decision == "replace":
        _replace(event, config, root, capability, cancel)
        return
    _retain(job, event, config, root, vb, budget, cancel)


def _duplicate(event, placed) -> NonverbalEvent | None:
    """An event already placed over the same moment, whatever produced it."""
    if event.target is None:
        return None
    for other in placed:
        if other.target is None or other.event_id == event.event_id:
            continue
        overlap = min(other.target.end, event.target.end) - max(
            other.target.start, event.target.start)
        if overlap > 0.05:
            return other
    return None


def _retain(job, event, config, root, vb, budget, cancel) -> None:
    """Keep the original sound, when there is a window it can be cut from."""
    if event.checks.get("position") not in (None, "whole"):
        _release(event)
        event.coverage = "unresolved"
        event.reason = ("this tag sits beside dialogue, so where the sound is inside "
                        "the line is unknown; cutting it would also retain the "
                        "original actor's voice")
        _finding(event, "reaction_unclean", "delivery", "warning",
                 {"position": event.checks.get("position"),
                  "cue_text": event.checks.get("cue_text", "")})
        return
    # A vocal reaction lives in the separated dialogue stem; a door or a crowd
    # lives in the bed. Cutting each from the stem it belongs to is what keeps
    # a retained laugh from arriving with the music under it.
    stem, origin = ((job.vocals, "vocals") if event.category == "vocal" and job.vocals
                    else (job.source_track or job.source_audio, "source"))
    span = event.span
    if stem is None or span is None or not Path(stem).is_file():
        _release(event)
        event.coverage = "unavailable"
        event.reason = "the original audio this would be cut from is not on disk"
        return
    neighbours = _neighbours(job, span)
    if neighbours:
        _finding(event, "reaction_unclean", "delivery", "warning",
                 {"cues": [n.cue_id for n in neighbours],
                  "lines": [n.index for n in neighbours],
                  "note": "a speaking line overlaps this window, so the retained "
                          "audio may carry source-language words"})
    start = max(0.0, span.start - config["handle"])
    end = min(span.end + config["handle"], start + config["max_seconds"])
    try:
        path = _cut(Path(stem), start, end, event, config, root, cancel)
    except (FFmpegError, OSError) as exc:
        _release(event)
        event.coverage = "unavailable"
        event.reason = f"the original could not be cut here: {exc}"
        return
    leak = _screen(event, path, job, config, vb, budget)
    if leak:
        _finding(event, "reaction_unclean", "delivery", "warning", leak)
    event.artifact = Artifact(role=UNKNOWN, path=str(path),
                              fingerprint=event.inputs, derived_from=origin,
                              duration=round(end - start, 4),
                              bytes=path.stat().st_size if path.exists() else None,
                              proven=False)
    event.coverage = "retained"
    event.in_mix = config["in_mix"]
    event.handle = config["handle"]
    event.fade_in = event.fade_out = config["fade"]
    event.reason = (f"cut from the {origin} stem at {span.start:.2f}-{span.end:.2f}s"
                    + ("; a speaking line overlaps this window" if neighbours else "")
                    + ("" if config["in_mix"] else
                       "; prepared for review only, not placed in the dub"))


def _replace(event, config, root, capability, cancel) -> None:
    """Use a supplied local sound, or say plainly why there is nothing to use."""
    if not event.asset:
        _release(event)
        if config["generate"]:
            # The engine was asked. Whether it can answer is a fact about the
            # adapter, and an adapter that cannot is recorded as such.
            event.coverage = "unsupported" if capability != "supported" else "unavailable"
            event.reason = (
                "no replacement sound was supplied and this engine does not generate "
                "nonverbal audio" if capability == "unsupported" else
                "no replacement sound was supplied and this engine's nonverbal "
                "capability is unknown" if capability == "unknown" else
                "no replacement sound was supplied")
            event.checks = {**event.checks, "engine_nonverbal": capability}
            _finding(event, "reaction_unsupported", "delivery", "warning",
                     {"capability": capability,
                      "note": "asked for, not applied; accepting a text field is not "
                              "evidence an engine can laugh"})
            return
        event.coverage = "unavailable"
        event.reason = "a replacement was chosen but no sound file was supplied"
        _finding(event, "reaction_uncovered", "delivery", "warning",
                 {"decision": "replace", "note": "no asset path was given"})
        return
    asset = Path(event.asset)
    if not asset.is_file():
        _release(event)
        event.coverage = "unavailable"
        event.reason = f"the supplied sound is not on disk: {asset.name}"
        _finding(event, "reaction_uncovered", "delivery", "warning",
                 {"asset": asset.name, "note": "the replacement file is missing"})
        return
    try:
        path = _cut(asset, 0.0, config["max_seconds"], event, config, root, cancel,
                    whole=True)
    except (FFmpegError, OSError) as exc:
        _release(event)
        event.coverage = "unavailable"
        event.reason = f"the supplied sound could not be decoded: {exc}"
        return
    event.artifact = Artifact(role=UNKNOWN, path=str(path), fingerprint=event.inputs,
                              derived_from="asset",
                              bytes=path.stat().st_size if path.exists() else None,
                              proven=False)
    event.coverage = "replaced"
    event.in_mix = config["in_mix"]
    event.fade_in = event.fade_out = config["fade"]
    event.reason = (f"replaced with {asset.name}"
                    + ("" if config["in_mix"] else
                       "; prepared for review only, not placed in the dub"))


def _cut(source: Path, start: float, end: float, event, config, root: Path,
         cancel, whole: bool = False) -> Path:
    """One bounded, faded, gain-adjusted piece of audio for a single event."""
    length = max(0.05, min(end - start, config["max_seconds"]))
    request = {"source": stamp(source), "start": round(start, 4), "length": round(length, 4),
               "gain": round(event.gain_db, 3), "fade": round(config["fade"], 4),
               "whole": whole, "detector": DETECTOR, "version": 1}
    event.inputs = processing_fingerprint(request)
    dest = root / f"{event.event_id}.{digest(request)[:12]}.wav"
    if matches([dest], request):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".partial.wav")
    chain = []
    if event.gain_db:
        chain.append(f"volume={event.gain_db:.3f}dB")
    fade = min(config["fade"], length / 4)
    if fade > 0:
        chain.append(f"afade=t=in:st=0:d={fade:g}")
        chain.append(f"afade=t=out:st={max(0.0, length - fade):g}:d={fade:g}")
    chain.append("aformat=channel_layouts=stereo")
    run_ffmpeg(["-y", "-ss", f"{max(0.0, start):g}", "-i", str(source),
                "-t", f"{length:g}", "-vn", "-af", ",".join(chain),
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(temp)],
               cancel=cancel)
    temp.replace(dest)
    record([dest], request)
    return dest


def _neighbours(job, span: Span) -> list:
    """Speaking cues whose original interval runs across this event's window."""
    found = []
    for seg in job.segments:
        for other in seg.source.spans:
            if min(other.end, span.end) - max(other.start, span.start) > 0.05:
                found.append(seg)
                break
    return found


def _screen(event, path: Path, job, config, vb, budget) -> dict | None:
    """Listen to a retained reaction for source-language words, when asked to.

    Recognition hearing nothing is not proof the clip is clean — a laugh is not
    words and neither is reverberated cross-talk — so a clean result is
    recorded as a check that found nothing, never as a guarantee.
    """
    if not config["leakage_check"] or vb is None:
        return None
    key = screening_fingerprint({"audio": event.inputs, "detector": DETECTOR,
                                 "language": job.script_lang or job.source_lang})
    if event.checks.get("screened") == key:
        return None
    if budget is not None and not budget.charge("reaction_screen"):
        event.checks = {**event.checks, "screened": "", "screen_note":
                        "the shared request budget is exhausted"}
        return None
    try:
        heard = vb.transcribe(Path(path), language=job.script_lang or job.source_lang) or {}
    except Exception as exc:  # noqa: BLE001 - a screening failure is reviewable
        event.checks = {**event.checks, "screen_note": f"screening failed: {exc}"}
        return None
    text = str((heard or {}).get("text") or "").strip()
    event.checks = {**event.checks, "screened": key, "heard": text}
    if not text:
        return None
    return {"heard": text,
            "note": "recognition heard words in this retained sound; music vocals, "
                    "reverberation and cross-talk also produce this, so it is a "
                    "suspicion to listen to, not a verdict"}


def _evidence(job, event, cancel, root: Path) -> None:
    """Measure whether this sound may already be in the bed or under the dub."""
    from . import levels

    span = event.span
    if span is None:
        return
    checks = dict(event.checks)
    bed = job.background if job.background and job.background != job.source_audio else None
    for label, track in (("source", job.source_track or job.source_audio), ("bed", bed)):
        if track is None or not Path(track).is_file():
            checks[f"{label}_db"] = None
            continue
        try:
            stats = levels.analyze(levels.extract_window(
                Path(track), span.start, span.end,
                root / "evidence" / f"{label}-{event.event_id}.wav", cancel))
        except (OSError, wave.Error, EOFError):
            checks[f"{label}_db"] = None
            continue
        checks[f"{label}_db"] = stats["speech_db"]
        checks[f"{label}_floor_db"] = stats["noise_db"]
    present = (checks.get("bed_db") is not None and checks.get("bed_floor_db") is not None
               and checks["bed_db"] - checks["bed_floor_db"] >= PRESENT_DB)
    # A vocal reaction is exactly what separation pulls *out* of the bed, so
    # energy in the bed under a laugh is far more likely to be the score. Only
    # a background event is credited to the bed on this evidence.
    checks["still_in_bed"] = bool(present and event.category == "background")
    checks["bed_kind"] = ("estimated" if bed is not None else
                          "original mix (no separation ran)")
    event.checks = checks


def _finding(event, code: str, kind: str, severity: str, evidence: dict) -> None:
    from .cues import Finding, finding_id

    event.findings = [f for f in event.findings if f.code != code]
    event.findings.append(Finding(
        finding_id=finding_id(event.event_id, code, DETECTOR),
        code=code, kind=kind, severity=severity, scope="event",
        target=event.event_id, span=event.target, detector=DETECTOR,
        evidence=evidence, inputs=event.inputs or event.event_id))


def summary(job) -> dict:
    """Coverage counts, kept strictly apart from speech verification."""
    states: dict[str, int] = {}
    for event in job.nonverbal:
        states[event.coverage] = states.get(event.coverage, 0) + 1
    resolved = sum(1 for e in job.nonverbal
                   if e.coverage in ("retained", "replaced", "omitted", "covered"))
    return {
        "events": len(job.nonverbal),
        "resolved": resolved,
        "unresolved": len(job.nonverbal) - resolved,
        "rendered": sum(1 for e in job.nonverbal if e.rendered),
        "placed": sum(1 for e in job.nonverbal if e.placed),
        "states": dict(sorted(states.items())),
        "findings": sum(len(e.findings) for e in job.nonverbal),
        "note": ("coverage counts what was decided about known events; it is not a "
                 "claim that every sound in the original was found"),
    }


def placements(job) -> list[NonverbalEvent]:
    """Events whose audio the mix should place, in target order.

    An event prepared under `coverage.mode = review` is deliberately not
    here: it exists so somebody can hear it, not so it ships.
    """
    return sorted((e for e in job.nonverbal if e.placed),
                  key=lambda e: e.target.start if e.target else 0.0)
