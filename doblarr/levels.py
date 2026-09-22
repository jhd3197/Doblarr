"""Source-relative dynamics with exactly one level owner.

D05 in one sentence: a line the original actor whispered should still be
quieter than the line next to it, and a line they shouted should still land,
without any of it being able to run away with the gain.

Two halves, deliberately separated:

- **Measurement** reads the *recorded source* over a cue's source spans and
  says how loud that performance was relative to ordinary dialogue in the same
  material. It refuses to produce a number it cannot stand behind: too short,
  overlapped by another speaker, or indistinguishable from its own background
  all come back as an explicit state rather than a quiet zero.
- **Processing** runs once, after timing, on the fitted derivative. It brings
  the line to one baseline target and then applies the bounded performance gain
  in the same step, so there is no second loudness pass anywhere downstream
  that could erase the contrast that was the whole point.

Everything is in `dBFS-rms-speech`: the RMS of the speech-active frames, in
dBFS. Not LUFS — integrated loudness is meaningless over a 400 ms interjection,
and these measurements exist precisely to compare short utterances with each
other. The legacy pre-fit `loudnorm` path keeps its own LUFS units and is
untouched; the two never both run.
"""

from __future__ import annotations

import logging
import math
import statistics
import wave
from pathlib import Path

from .artifacts import digest, matches, record, stamp
from .cues import (
    LEVELED,
    Artifact,
    LevelDecision,
    SourceMeasurement,
)
from .errors import JobCancelled
from .ffmpeg import run_ffmpeg
from .fingerprints import processing as processing_fingerprint
from .fingerprints import verification as measurement_fingerprint

log = logging.getLogger("doblarr.levels")

METHOD = "speech-rms/1"
UNITS = "dBFS-rms-speech"
PROCESSOR = "levels/1"

# How far below the loudest part of a clip a frame can be and still count as
# speech. A window, not a floor test: a take that is speech from end to end has
# no silence to compare against, and gating on a noise floor would report one
# frame of it. 18 dB keeps ordinary speech dynamics and drops the padding.
ACTIVE_RANGE_DB = 18.0
# Below this much speech-active audio a level is an accident of which consonant
# landed in the window, not a performance measurement.
MIN_MEASURED_SECONDS = 0.30
# Speech this close to its own background cannot be measured apart from it.
MIN_SEPARATION_DB = 8.0
# A baseline needs this many clean cues before it means "ordinary dialogue" and
# not "the three lines we happened to be able to measure".
MIN_BASELINE_SAMPLES = 4
MIN_SPEAKER_BASELINE_SAMPLES = 6
# Nothing may be pushed past this peak; the mix limiter is the last resort, not
# the first one.
PEAK_CEILING = 0.89
FRAME_SECONDS = 0.02


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1e-10))


def settings(options: dict | None) -> dict:
    """Effective level settings, with every default written down once."""
    values = dict(options or {})
    mode = str(values.get("mode", "legacy"))
    return {
        "mode": mode,
        "target_db": float(values.get("target_db", -20.0)),
        "strength": max(0.0, min(1.0, float(values.get("strength", 0.7)))),
        "max_boost_db": max(0.0, float(values.get("max_boost_db", 4.0))),
        "max_cut_db": max(0.0, float(values.get("max_cut_db", 8.0))),
        "min_seconds": max(0.0, float(values.get("min_seconds", MIN_MEASURED_SECONDS))),
        "min_separation_db": float(values.get("min_separation_db", MIN_SEPARATION_DB)),
        "peak_ceiling": max(0.1, min(1.0, float(values.get("peak_ceiling", PEAK_CEILING)))),
        # `follow_source` needs measurements by definition; the flag only adds
        # them to the other modes. Reading it as an override would let the
        # schema default silently leave the mode with nothing to follow.
        "measure_source": bool(values.get("measure_source", False))
        or mode == "follow_source",
        "gains": {str(k): float(v) for k, v in (values.get("gains") or {}).items()},
    }


def owns_processing(options: dict | None) -> bool:
    """True when this module, not the pre-fit loudnorm, decides loudness."""
    return settings(options)["mode"] in ("consistent", "follow_source", "manual")


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def _frames(path: Path) -> tuple[list[float], float]:
    """Per-frame peak amplitude and the clip's duration.

    Channel policy: channels are combined by maximum, never averaged, so a
    performance panned into one channel does not read as half as loud. The
    policy that ran is recorded on the measurement.
    """
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise wave.Error("level analysis requires 16-bit PCM")
        rate = audio.getframerate()
        channels = max(1, audio.getnchannels())
        block = max(1, int(round(rate * FRAME_SECONDS)))
        import array
        import sys

        peaks: list[float] = []
        pending, pending_count = 0.0, 0
        while raw := audio.readframes(block * 16):
            samples = array.array("h", raw)
            if sys.byteorder != "little":
                samples.byteswap()
            for offset in range(0, len(samples), channels):
                frame = samples[offset:offset + channels]
                if not frame:
                    break
                pending = max(pending, max(abs(v) for v in frame) / 32768)
                pending_count += 1
                if pending_count >= block:
                    peaks.append(pending)
                    pending, pending_count = 0.0, 0
        if pending_count:
            peaks.append(pending)
        return peaks, audio.getnframes() / rate


def analyze(path: Path, min_separation_db: float = MIN_SEPARATION_DB) -> dict:
    """Speech-active level, floor and peak of one PCM file.

    `speech_db` is the RMS of the frames within `ACTIVE_RANGE_DB` of the
    loudest part, which is what makes a quiet line measurable at all:
    averaging in the silence around it would report the silence.

    `noise_db` is read from the frames that fell *outside* that window, so it
    is a real floor rather than the quietest moment of the speech itself. A
    clip with no such frames has no measurable floor, and `separation` is
    `None` — unknown, which is not the same as zero.
    """
    peaks, duration = _frames(Path(path))
    if not peaks:
        return {"speech_db": None, "noise_db": None, "peak": 0.0,
                "active_seconds": 0.0, "duration": duration, "separation": None,
                "min_separation_db": min_separation_db}
    ordered = sorted(peaks)
    high_db = _db(ordered[max(0, int(len(ordered) * 0.95) - 1)])
    limit = high_db - ACTIVE_RANGE_DB
    active = [p for p in peaks if _db(p) >= limit]
    quiet = [p for p in peaks if _db(p) < limit]
    if not active:  # pragma: no cover - the 95th percentile is always in range
        active = [ordered[-1]]
    speech_db = _db(math.sqrt(sum(p * p for p in active) / len(active)))
    noise_db = _db(sorted(quiet)[len(quiet) // 2]) if quiet else None
    return {
        "speech_db": round(speech_db, 3),
        "noise_db": round(noise_db, 3) if noise_db is not None else None,
        "peak": max(peaks),
        "active_seconds": round(len(active) * FRAME_SECONDS, 3),
        "duration": duration,
        "separation": (round(speech_db - noise_db, 3) if noise_db is not None else None),
        "min_separation_db": min_separation_db,
    }


def _layout(path: Path) -> tuple[int, int]:
    """Channel count and sample rate of a PCM file, for a format-preserving render.

    The level pass must not change the channel layout. FFmpeg's mono-to-stereo
    rematrix costs 3 dB, which would silently shift every measured level the
    pass exists to control, so the output keeps the input's layout and the
    applied gain is exactly the gain that was decided.
    """
    try:
        with wave.open(str(path), "rb") as audio:
            return max(1, audio.getnchannels()), audio.getframerate()
    except (OSError, wave.Error, EOFError):
        return 2, 48000


def extract_window(source: Path, start: float, end: float, dest: Path,
                   cancel=None) -> Path:
    """Pull one source interval as 16-bit mono PCM for measurement."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-y", "-ss", f"{max(0.0, start):g}", "-i", str(source),
                "-t", f"{max(0.01, end - start):g}", "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "pcm_s16le", str(dest)], cancel=cancel)
    return dest


def overlapping(segments, seg) -> bool:
    """Whether another speaker's source interval runs across this cue's."""
    spans = seg.source.spans
    if not spans:
        return False
    start, end = min(s.start for s in spans), max(s.end for s in spans)
    for other in segments:
        if other is seg or other.speaker == seg.speaker or not other.source.spans:
            continue
        o_start = min(s.start for s in other.source.spans)
        o_end = max(s.end for s in other.source.spans)
        if o_start < end and o_end > start:
            return True
    return False


def dialogue_stream(job) -> tuple[Path | None, str, bool]:
    """Which audio to measure, what to call it, and whether it is contaminated.

    The separated vocal stem is the better evidence when separation actually
    ran. When it did not, `job.vocals` is the full mix under another name, and
    every measurement taken from it is flagged as contaminated rather than
    quietly trusted.
    """
    separated = (job.vocals is not None and job.background is not None
                 and job.vocals != job.source_audio
                 and job.background != job.source_audio)
    if separated and Path(job.vocals).exists():
        return Path(job.vocals), "separated-vocals", False
    # `source_track`, not `source_audio`: an audition replaces the working
    # audio with a montage, and measuring a montage would attribute one cue's
    # loudness to whatever was concatenated next to it.
    track = job.source_track or job.source_audio
    if track and Path(track).exists():
        return Path(track), "source-stream", True
    return None, "missing", True


def measure_sources(job, options: dict | None = None, cancel=None,
                    work_dir: Path | None = None) -> dict:
    """Measure every cue's original performance and build the dialogue baseline.

    Returns the frozen baseline record, which is stored on the job so a resume
    reuses the same reference instead of recomputing one over a different set
    of cues.
    """
    config = settings(options)
    if not config["measure_source"]:
        for seg in job.segments:
            seg.measurement = SourceMeasurement(
                state="unknown", method=METHOD,
                exclusions=["source measurement is off"])
        return {"scope": "off", "method": METHOD}
    stream, origin, contaminated = dialogue_stream(job)
    if stream is None:
        for seg in job.segments:
            seg.measurement = SourceMeasurement(state="missing", method=METHOD,
                                                source=origin,
                                                exclusions=["no source audio to measure"])
        log.warning("levels: no source audio; source-relative gain is unavailable")
        return {"scope": "missing", "method": METHOD}

    root = Path(work_dir or (job.artifacts_dir or Path("."))) / "levels"
    identity = stamp(stream)
    for seg in job.segments:
        # Checked per cue, not only inside FFmpeg: a run whose extracts are
        # all cached would otherwise ignore a cancel until it finished the
        # whole episode.
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during source measurement")
        seg.measurement = _measure_cue(job, seg, stream, origin, contaminated,
                                       config, root, identity, cancel)
    baseline = build_baseline(job.segments, config)
    apply_baseline(job.segments, baseline)
    job.dialogue_baseline = baseline
    log.info("levels: measured %d/%d cues; baseline %s",
             sum(1 for s in job.segments if s.measurement.state == "measured"),
             len(job.segments), baseline.get("scope"))
    return baseline


def _measure_cue(job, seg, stream: Path, origin: str, contaminated: bool,
                 config: dict, root: Path, identity, cancel) -> SourceMeasurement:
    spans = [s for s in seg.source.spans]
    exclusions: list[str] = []
    if not spans:
        return SourceMeasurement(state="missing", method=METHOD, source=origin,
                                 channels="max-of-channels",
                                 exclusions=["the cue has no recorded source interval"])
    start, end = min(s.start for s in spans), max(s.end for s in spans)
    inputs = measurement_fingerprint({"stream": identity, "start": round(start, 3),
                                      "end": round(end, 3), "method": METHOD})
    if end - start < config["min_seconds"]:
        return SourceMeasurement(state="insufficient", method=METHOD, source=origin,
                                 channels="max-of-channels", inputs=inputs,
                                 measured_seconds=round(end - start, 3),
                                 exclusions=[f"only {end - start:.2f}s of source to measure"])
    clip = root / f"cue-{seg.cue_id or seg.index}-{inputs}.wav"
    try:
        if not clip.is_file():
            extract_window(stream, start, end, clip, cancel)
        stats = analyze(clip, config["min_separation_db"])
    except (OSError, wave.Error, EOFError) as exc:
        return SourceMeasurement(state="missing", method=METHOD, source=origin,
                                 channels="max-of-channels", inputs=inputs,
                                 exclusions=[f"source interval could not be measured: {exc}"])
    overlap = overlapping(job.segments, seg)
    if overlap:
        exclusions.append("another speaker talks across this cue")
    if contaminated:
        exclusions.append("no separated dialogue stem; music and effects are in the reference")
    separation = stats["separation"]
    state = "measured"
    if stats["active_seconds"] < config["min_seconds"]:
        state = "insufficient"
        exclusions.append(f"only {stats['active_seconds']:.2f}s of speech-active source")
    elif separation is not None and separation < config["min_separation_db"]:
        state = "contaminated"
        exclusions.append(
            f"only {separation:.1f} dB between speech and its background")
    elif overlap or contaminated:
        state = "contaminated"
    if separation is None:
        exclusions.append("no quiet frames in this interval to read a floor from")
    return SourceMeasurement(
        state=state,
        speech_db=stats["speech_db"],
        units=UNITS,
        method=METHOD,
        channels="max-of-channels",
        measured_seconds=stats["active_seconds"],
        confidence=(round(min(1.0, max(0.0, separation / 30)), 3)
                    if separation is not None else None),
        overlapped=overlap,
        contaminated=contaminated or state == "contaminated",
        exclusions=exclusions,
        source=origin,
        inputs=inputs,
    )


def build_baseline(segments, config: dict) -> dict:
    """The ordinary-dialogue reference every relative level is measured against.

    Per speaker when that speaker has enough clean cues of their own, global
    when the material as a whole does, and an explicit fallback otherwise — a
    baseline computed from two lines is not a baseline, and saying so is better
    than inventing one.
    """
    usable = [s for s in segments if s.measurement.trusted or
              (s.measurement.state == "measured" and s.measurement.speech_db is not None)]
    clean = [s for s in usable if not s.measurement.overlapped
             and not s.measurement.contaminated]
    pool = clean or []
    by_speaker: dict[str, list[float]] = {}
    for seg in pool:
        by_speaker.setdefault(seg.speaker, []).append(seg.measurement.speech_db)
    speakers = {label: round(statistics.median(values), 3)
                for label, values in by_speaker.items()
                if len(values) >= MIN_SPEAKER_BASELINE_SAMPLES}
    levels = [s.measurement.speech_db for s in pool]
    if len(levels) >= MIN_BASELINE_SAMPLES:
        return {"scope": "speaker" if speakers else "global",
                "global_db": round(statistics.median(levels), 3),
                "speakers": speakers, "samples": len(levels), "method": METHOD,
                "units": UNITS}
    return {"scope": "fallback", "global_db": None, "speakers": {},
            "samples": len(levels), "method": METHOD, "units": UNITS,
            "reason": f"only {len(levels)} clean source measurements; "
                      f"{MIN_BASELINE_SAMPLES} are needed for a reference"}


def apply_baseline(segments, baseline: dict) -> None:
    """Attach the baseline each cue is compared to, and its relative level."""
    for seg in segments:
        measurement = seg.measurement
        reference = baseline.get("speakers", {}).get(seg.speaker)
        scope = "speaker"
        if reference is None:
            reference = baseline.get("global_db")
            scope = baseline.get("scope", "fallback")
        measurement.baseline_scope = scope
        measurement.baseline_samples = int(baseline.get("samples", 0))
        if reference is None or measurement.speech_db is None:
            measurement.baseline_db = None
            measurement.relative_db = None
            if measurement.state == "measured" and reference is None:
                measurement.exclusions = [*measurement.exclusions,
                                          "no trustworthy dialogue baseline in this material"]
            continue
        measurement.baseline_db = reference
        measurement.relative_db = round(measurement.speech_db - reference, 3)


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

def decide(seg, stats: dict, config: dict, manual_db: float | None) -> LevelDecision:
    """What gain this line should get, before anything is rendered.

    Order matters and is fixed here: reach the baseline target once, then add a
    bounded performance gain. A manual value replaces the automatic one
    outright — a reviewer who set a number has already heard the line.
    """
    mode = config["mode"]
    measured = stats["speech_db"]
    decision = LevelDecision(mode=mode, target_db=config["target_db"],
                             measured_db=measured, strength=config["strength"],
                             bound_db=config["max_boost_db"])
    if measured is None:
        decision.outcome = "unavailable"
        decision.reason = "the generated line could not be measured"
        return decision
    decision.baseline_db = round(config["target_db"] - measured, 3)
    if manual_db is not None:
        decision.manual = True
        decision.requested_db = round(float(manual_db), 3)
        decision.applied_db = decision.requested_db
        decision.outcome = "applied"
        decision.reason = "manual per-line gain set in review"
        return decision
    if mode == "consistent":
        decision.outcome = "applied"
        decision.reason = "consistent volume: every line reaches the same target"
        return decision
    if mode == "manual":
        decision.outcome = "applied"
        decision.reason = "manual mode with no gain set for this line"
        return decision
    # follow_source
    measurement = seg.measurement
    if not measurement.trusted:
        decision.outcome = "fallback"
        decision.reason = (measurement.exclusions[0] if measurement.exclusions
                           else f"source evidence is {measurement.state}")
        return decision
    decision.requested_db = round((measurement.relative_db or 0.0) * config["strength"], 3)
    bound = config["max_boost_db"] if decision.requested_db > 0 else config["max_cut_db"]
    decision.bound_db = bound
    decision.applied_db = round(max(-config["max_cut_db"],
                                    min(config["max_boost_db"], decision.requested_db)), 3)
    if abs(decision.applied_db - decision.requested_db) > 1e-6:
        decision.outcome = "clamped"
        decision.reason = (f"source suggested {decision.requested_db:+.1f} dB; "
                           f"bounded to {decision.applied_db:+.1f} dB")
    else:
        decision.outcome = "applied"
        decision.reason = (f"following the original at {config['strength']:.0%} "
                           f"of {measurement.relative_db:+.1f} dB")
    return decision


def protect_peak(decision: LevelDecision, peak: float, ceiling: float) -> float:
    """Total gain in dB after peak protection, recorded on the decision.

    The reduction comes out of the baseline component and leaves the
    performance gain alone. Peak protection is about this one file not
    clipping; the contrast between a whisper and a shout is the thing the
    feature exists for, and spending it to satisfy a ceiling would quietly
    undo the work. The line simply sits lower, and says so.
    """
    total = decision.baseline_db + decision.applied_db
    predicted = max(1e-10, peak) * (10 ** (total / 20))
    if predicted <= ceiling:
        decision.peak = round(min(1.0, predicted), 4)
        return round(total, 3)
    reduction = 20 * math.log10(predicted / ceiling)
    decision.peak_limited = True
    decision.peak = round(ceiling, 4)
    decision.baseline_db = round(decision.baseline_db - reduction, 3)
    decision.reason = (f"{decision.reason}; held {reduction:.1f} dB back from the "
                       f"baseline to stay under the peak ceiling")
    return round(total - reduction, 3)


def process(job, options: dict | None = None, cancel=None, dry_run: bool = False) -> None:
    """Apply the one level pass, after timing, to every cue that has audio.

    Reads `CueAudio.upstream_of(LEVELED)` — the fitted derivative, or whatever
    is the most processed artifact before this role — so re-running with new
    settings reprocesses its input and never stacks gain on its own output.
    """
    config = settings(options)
    if dry_run:
        return
    if config["mode"] in ("legacy", "off"):
        for seg in job.segments:
            seg.level = LevelDecision(mode=config["mode"], outcome="bypassed",
                                      reason="the pre-fit loudness pass owns this run"
                                      if config["mode"] == "legacy"
                                      else "level processing is off")
            _forget(seg)
        return
    applied = 0
    for seg in job.segments:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during level processing")
        upstream = seg.audio.upstream_of(LEVELED)
        if upstream is None or not upstream.exists():
            seg.level = LevelDecision(mode=config["mode"], outcome="unavailable",
                                      reason="no rendered audio to level")
            continue
        source = Path(upstream.path)
        try:
            stats = analyze(source)
        except (OSError, wave.Error, EOFError) as exc:
            seg.level = LevelDecision(mode=config["mode"], outcome="unavailable",
                                      reason=f"the rendered line could not be measured: {exc}")
            continue
        manual = config["gains"].get(seg.cue_id, config["gains"].get(str(seg.index)))
        decision = decide(seg, stats, config, manual)
        if decision.outcome == "unavailable":
            seg.level = decision
            continue
        total = protect_peak(decision, stats["peak"], config["peak_ceiling"])
        decision.inputs = processing_fingerprint({
            "input": upstream.fingerprint, "gain": total, "mode": config["mode"],
            "processor": PROCESSOR})
        seg.level = decision
        channels, rate = _layout(source)
        request = {"source": stamp(source), "gain": total, "processor": PROCESSOR,
                   "channels": channels, "rate": rate, "version": 1}
        dest = source.parent / "levels" / f"{source.stem}.{digest(request)[:12]}.wav"
        if not matches([dest], request):
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_suffix(".partial.wav")
            run_ffmpeg(["-y", "-i", str(source), "-af", f"volume={total:.3f}dB",
                        "-ar", str(rate), "-ac", str(channels),
                        "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
            temp.replace(dest)
            record([dest], request)
        seg.audio.put_render(Artifact(
            role=LEVELED, path=str(dest), fingerprint=decision.inputs,
            derived_from=upstream.role, duration=upstream.duration,
            bytes=dest.stat().st_size if dest.exists() else None))
        seg.audio_clip = dest
        applied += 1
    job.metrics["levels_applied"] = applied
    job.metrics["levels_mode"] = config["mode"]
    job.metrics["levels_clamped"] = sum(1 for s in job.segments
                                        if s.level.outcome == "clamped")
    job.metrics["levels_fallback"] = sum(1 for s in job.segments
                                         if s.level.outcome == "fallback")
    job.metrics["levels_peak_limited"] = sum(1 for s in job.segments if s.level.peak_limited)
    log.info("levels: %s mode applied to %d/%d lines (%d clamped, %d fell back)",
             config["mode"], applied, len(job.segments),
             job.metrics["levels_clamped"], job.metrics["levels_fallback"])


# How far a line's speech has to stand above the bed under it before it is
# reasonably intelligible. Below this the line is *reported*, never quietly
# raised: turning a whisper into ordinary speech to satisfy a number is the
# failure this whole module exists to avoid.
MIN_MARGIN_DB = 6.0
MIX_DETECTOR = "mix-margin/1"


def check_mix(job, options: dict | None = None, cancel=None, work_dir: Path | None = None,
              dry_run: bool = False) -> int:
    """Measure each line against the bed it will actually be heard over.

    A whisper that is mathematically the right number of dB below ordinary
    dialogue can still be inaudible under music. This measures the mixed
    result and the background across the same window and raises a finding
    where the margin is too small — it changes no audio, because the answer
    to "this is masked" is a human decision about the mix, not a gain.
    """
    from .stages.quality import apply_findings

    config = settings(options)
    if dry_run or config["mode"] in ("legacy", "off"):
        return 0
    bed = job.background if job.background and job.background != job.source_audio else None
    mixed = job.dubbed_track
    if mixed is None or not Path(mixed).is_file() or bed is None or not Path(bed).is_file():
        # Without a separated bed there is nothing to measure the line
        # against, and guessing a margin from the full mix would be measuring
        # the dub against itself.
        return 0
    root = Path(work_dir or (job.artifacts_dir or Path("."))) / "levels" / "mix"
    flagged = 0
    for seg in job.segments:
        if cancel is not None and cancel.is_set():
            break
        if seg.level.outcome in ("bypassed", "unavailable") or seg.duration <= 0:
            continue
        try:
            speech = analyze(extract_window(Path(mixed), seg.start, seg.end,
                                      root / f"mix-{seg.cue_id or seg.index}.wav", cancel))
            under = analyze(extract_window(Path(bed), seg.start, seg.end,
                                     root / f"bed-{seg.cue_id or seg.index}.wav", cancel))
        except (OSError, wave.Error, EOFError):
            continue
        if speech["speech_db"] is None or under["speech_db"] is None:
            continue
        margin = round(speech["speech_db"] - under["speech_db"], 2)
        observed = []
        if margin < MIN_MARGIN_DB:
            flagged += 1
            observed.append((
                "quiet_line_masked", "performance", "warning", None,
                {"margin_db": margin, "needs_db": MIN_MARGIN_DB,
                 "mixed_db": speech["speech_db"], "bed_db": under["speech_db"],
                 "applied_db": seg.level.applied_db,
                 "note": "the line may be hard to hear over the bed here; "
                         "raising it would undo the performance contrast, so "
                         "this is a mix decision"}))
        apply_findings(seg, MIX_DETECTOR, f"{margin:.2f}", observed)
    job.metrics["levels_mix_flagged"] = flagged
    if flagged:
        log.info("levels: %d line(s) sit less than %.0f dB over the bed", flagged,
                 MIN_MARGIN_DB)
    return flagged


def _forget(seg) -> None:
    """Drop a level derivative this run will not make, and anything after it."""
    if seg.audio.render(LEVELED) is None:
        return
    seg.audio.drop_renders((LEVELED,))
    seg.audio.invalidate_after(LEVELED)
    # Whichever timing owner ran, and whichever preparation preceded it: ask
    # the ordering rather than naming the roles, so a role added later cannot
    # be silently skipped here.
    upstream = seg.audio.upstream_of(LEVELED)
    if upstream is not None and upstream.path:
        seg.audio_clip = Path(upstream.path)
