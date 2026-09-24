"""Speech-boundary preparation and protected clip edges.

Two operations, both reversible and both owned here:

1. **Preparation** runs on the *raw* take, after the raw checks have had their
   say and before anything measures the clip against its slot. It detects where
   speech actually starts and stops and writes a trimmed derivative, so the
   timing stage is not asked to compress speech that overruns only because the
   generator left dead air on the front.

2. **Edges** run on the *final* dialogue render, after fitting, and ease a
   voice in and out only where the clip would otherwise begin or end on a
   non-zero sample. An edge that is already smooth is left exactly as it is.
   By default the entrance is short (it must not blunt a consonant) and the
   exit longer, on an S-shaped curve, so a line neither pops in nor is cut
   off mid-breath. `edge_fade_ms`, when set, is the older single short linear
   fade exactly as it was.

What this deliberately does not do: it never removes an internal pause, never
shortens a clip that is merely quiet, never stretches a short utterance to fill
its slot, and never turns an empty or failed generation into something that
looks like a valid line. When the evidence is ambiguous the take is left alone
and the reason is recorded, because leaving audio untouched is the safe answer.

The detector is deliberately plain energy analysis over the PCM Doblarr already
decodes for clip checks. A VAD model would add a heavyweight optional
dependency for a decision whose failure mode must be "do nothing", and the
fixtures show plain energy is adequate for finding generator padding. The
thresholds below are the tunable part; they are conservative on purpose.
"""

from __future__ import annotations

import array
import logging
import math
import sys
import wave
from pathlib import Path

from ..artifacts import digest, matches, record, stamp
from ..cues import (
    CLIP,
    EDGED,
    RAW,
    TRIMMED,
    Artifact,
    Span,
    SpeechPreparation,
)
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..fingerprints import processing as processing_fingerprint

log = logging.getLogger("doblarr.boundaries")

DETECTOR = "energy-boundaries/1"

# Analysis frame. 10 ms is short enough to place a boundary within a phoneme and
# long enough that one stray sample cannot define a boundary on its own.
FRAME_SECONDS = 0.010
# Consecutive frames above the threshold before speech is believed to have
# started. Two frames (20 ms) rejects clicks without losing a plosive.
HANGOVER_FRAMES = 2
# A gap at least this long inside the active region is reported as a suspected
# pause or breath. It is reported, never removed.
MIN_INTERNAL_SILENCE = 0.12
# Longest fade the edge pass will ever apply, and the window it inspects to
# decide whether an edge is already smooth.
MAX_FADE_SECONDS = 0.050
# The exit may be longer: a trailing vowel or breath needs room to die away,
# and nothing important is ever said in the last few hundredths of a take.
MAX_FADE_OUT_SECONDS = 0.150
FADE_CURVES = ("hsin", "qsin", "tri")
EDGE_WINDOW_SECONDS = 0.002
# Nothing below this is audible content. It is the same floor the clip checks
# use to call a generation silent.
SILENCE_DB = -55.0


class Bounds:
    """Raw measurements from one take. No decision is made here."""

    def __init__(self, duration, frames, rate, noise_db, speech_db,
                 first, last, clipped, silences):
        self.duration = duration
        self.frames = frames
        self.rate = rate
        self.noise_db = noise_db
        self.speech_db = speech_db
        self.first = first          # seconds, CLIP domain, or None when silent
        self.last = last            # seconds, CLIP domain, or None when silent
        self.clipped = clipped
        self.silences = silences    # [(start, end)] inside the active region

    @property
    def separation(self) -> float:
        """How far the speech stands above the noise floor, in dB."""
        return self.speech_db - self.noise_db


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1e-10))


def _read_frames(path: Path):
    """Per-frame peak amplitude, 0..1, using the loudest channel at each frame.

    Channel policy: a take whose speech sits in one channel must not read as
    silence because the other channel is empty, so channels are combined by
    maximum rather than averaged.
    """
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise wave.Error("boundary analysis requires 16-bit PCM")
        rate = audio.getframerate()
        channels = max(1, audio.getnchannels())
        block = max(1, int(round(rate * FRAME_SECONDS)))
        peaks: list[float] = []
        clipped = 0
        total = 0
        pending = 0.0
        pending_count = 0
        while raw := audio.readframes(block * 16):
            samples = array.array("h", raw)
            if sys.byteorder != "little":
                samples.byteswap()
            total += len(samples)
            for offset in range(0, len(samples), channels):
                frame = samples[offset:offset + channels]
                if not frame:
                    break
                loudest = max(abs(v) for v in frame)
                if loudest >= 32760:
                    clipped += 1
                pending = max(pending, loudest / 32768)
                pending_count += 1
                if pending_count >= block:
                    peaks.append(pending)
                    pending, pending_count = 0.0, 0
        if pending_count:
            peaks.append(pending)
        duration = audio.getnframes() / rate
    return peaks, rate, duration, clipped / max(1, total)


def _active_region(peaks, noise_db: float, threshold_db: float):
    """First/last speech frame and the gaps between them, at this threshold."""
    limit = noise_db + threshold_db
    loud = [_db(p) >= limit for p in peaks]
    first = last = None
    run = 0
    for index, is_loud in enumerate(loud):
        if not is_loud:
            run = 0
            continue
        run += 1
        if run < HANGOVER_FRAMES:
            continue
        if first is None:
            first = (index - run + 1) * FRAME_SECONDS
        last = (index + 1) * FRAME_SECONDS
    if first is None or last is None:
        return None, None, []
    gaps, gap_start = [], None
    for index in range(int(first / FRAME_SECONDS), int(last / FRAME_SECONDS)):
        if index < len(loud) and not loud[index]:
            if gap_start is None:
                gap_start = index * FRAME_SECONDS
        elif gap_start is not None:
            if index * FRAME_SECONDS - gap_start >= MIN_INTERNAL_SILENCE:
                gaps.append((gap_start, index * FRAME_SECONDS))
            gap_start = None
    return first, last, gaps


def measure(path: Path, threshold_db: float = 12.0) -> Bounds:
    """Measure one take's noise floor, speech level and speech-active bounds."""
    peaks, rate, duration, clipped_fraction = _read_frames(Path(path))
    if not peaks:
        return Bounds(duration, 0, rate, -100.0, -100.0, None, None, False, [])
    ordered = sorted(peaks)
    # The floor is the level the take sits at when nothing is being said — its
    # own noise, not digital silence. Read it from the quietest few frames
    # rather than a fixed percentile: a take with only 100 ms of padding still
    # has a real floor, and the third-quietest frame ignores a lone dropout.
    noise_db = _db(ordered[min(len(ordered) - 1, max(2, int(len(ordered) * 0.02)))])
    speech_db = _db(ordered[max(0, int(len(ordered) * 0.95) - 1)])
    first, last, gaps = _active_region(peaks, noise_db, threshold_db)
    return Bounds(duration, len(peaks), rate, noise_db, speech_db,
                  first, last, clipped_fraction > 0.001, gaps)


def inspect(path: Path, threshold_db: float = 12.0, cancel=None) -> Bounds:
    """Measure any audio file, converting to PCM first when it is not already.

    The public entry point for stages that need the same speech-active
    evidence outside boundary preparation — phrase timing reads the internal
    gaps from here rather than reimplementing the detector.
    """
    source = Path(path)
    analysed = _pcm(source, cancel)
    try:
        return measure(analysed, threshold_db)
    finally:
        if analysed != source:
            analysed.unlink(missing_ok=True)


def decide(bounds: Bounds, slot: float, settings: dict) -> SpeechPreparation:
    """Turn measurements into a trim decision, preferring to do nothing.

    Every path that is not a confident trim leaves the audio untouched and says
    why, so a reviewer can tell "nothing needed doing" apart from "we could not
    tell" apart from "this generation is empty".
    """
    handle = max(0.0, float(settings.get("handle_ms", 60)) / 1000)
    max_trim = max(0.0, float(settings.get("max_trim_seconds", 2.0)))
    min_trim = max(0.0, float(settings.get("min_trim_ms", 30)) / 1000)
    min_separation = float(settings.get("min_separation_db", 10))

    prepared = SpeechPreparation(
        detector=DETECTOR,
        handle=handle,
        raw_duration=bounds.duration,
        noise_db=round(bounds.noise_db, 2),
        speech_db=round(bounds.speech_db, 2),
        clipped=bounds.clipped,
        confidence=round(min(1.0, max(0.0, bounds.separation / 30)), 3),
    )
    if bounds.speech_db < SILENCE_DB:
        # Nothing audible anywhere. The raw checks own this failure; preparation
        # must not make an empty generation look like a usable line by cropping.
        prepared.decision = "empty"
        prepared.reason = f"nothing above {SILENCE_DB:g} dBFS in the take"
        prepared.confidence = 1.0
        return prepared
    if bounds.separation < min_separation:
        # A whisper in noise, or a take with no dynamic range at all. There is
        # audio here, but where it starts is not knowable from energy alone.
        prepared.decision = "uncertain"
        prepared.reason = (f"only {bounds.separation:.1f} dB between speech and noise "
                           f"(needs {min_separation:g})")
        return prepared
    if bounds.first is None or bounds.last is None or bounds.last <= bounds.first:
        prepared.decision = "uncertain"
        prepared.reason = "no speech-active region could be located"
        return prepared

    prepared.active = Span(bounds.first, bounds.last, CLIP)
    prepared.active_duration = bounds.last - bounds.first
    prepared.silences = [Span(start, end, CLIP) for start, end in bounds.silences]

    lead = max(0.0, min(bounds.first - handle, max_trim))
    tail = max(0.0, min(bounds.duration - bounds.last - handle, max_trim))
    clamped = ((bounds.first - handle) > max_trim
               or (bounds.duration - bounds.last - handle) > max_trim)
    if lead + tail < min_trim:
        prepared.decision = "kept"
        prepared.reason = "no removable boundary padding"
        return prepared
    if slot > 0 and (bounds.last - bounds.first) <= 0:
        prepared.decision = "kept"
        prepared.reason = "active region has no duration"
        return prepared
    prepared.decision = "trimmed"
    prepared.lead = round(lead, 4)
    prepared.tail = round(tail, 4)
    prepared.reason = ("removable generator padding"
                       + (f"; clamped to {max_trim:g}s per side" if clamped else ""))
    return prepared


def _settings(options: dict | None) -> dict:
    values = dict(options or {})
    return {
        "trim": bool(values.get("trim", False)),
        "handle_ms": float(values.get("handle_ms", 60)),
        "max_trim_seconds": float(values.get("max_trim_seconds", 2.0)),
        "min_trim_ms": float(values.get("min_trim_ms", 30)),
        "threshold_db": float(values.get("threshold_db", 12)),
        "min_separation_db": float(values.get("min_separation_db", 10)),
        "edge_fade_ms": float(values.get("edge_fade_ms", 0)),
        "edge_fade_in_ms": float(values.get("edge_fade_in_ms", 12)),
        "edge_fade_out_ms": float(values.get("edge_fade_out_ms", 40)),
        "edge_fade_curve": str(values.get("edge_fade_curve", "hsin")),
        "edge_threshold_db": float(values.get("edge_threshold_db", -40)),
    }


def edge_fades(settings: dict) -> tuple[float, float, str]:
    """(fade in, fade out) in seconds and the afade curve, from the settings.

    A nonzero `edge_fade_ms` is the older setting and wins: one linear fade
    of that length at each end, capped at 50 ms, exactly as before. Otherwise
    the entrance and exit are eased separately.
    """
    legacy = settings["edge_fade_ms"] / 1000
    if legacy > 0:
        fade = min(MAX_FADE_SECONDS, legacy)
        return fade, fade, "tri"
    curve = settings["edge_fade_curve"]
    if curve not in FADE_CURVES:
        raise ValueError(f"boundaries.edge_fade_curve must be one of {', '.join(FADE_CURVES)}")
    return (max(0.0, min(MAX_FADE_SECONDS, settings["edge_fade_in_ms"] / 1000)),
            max(0.0, min(MAX_FADE_OUT_SECONDS, settings["edge_fade_out_ms"] / 1000)),
            curve)


def _pcm(path: Path, cancel=None) -> Path:
    """A 16-bit PCM view of `path`, converting only when necessary."""
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as audio:
            if audio.getsampwidth() == 2:
                return source
    except (wave.Error, EOFError, OSError):
        pass
    converted = source.with_name(source.stem + ".boundaries.wav")
    run_ffmpeg(["-y", "-i", str(source), "-ac", "2", "-ar", "48000",
                "-c:a", "pcm_s16le", str(converted)], cancel=cancel)
    return converted


def _forget_trim(seg, raw) -> None:
    """Drop a trim this run will not make, and anything derived from it.

    A previous run's trimmed derivative is not a derivative of an untrimmed
    take, so keeping it would quietly feed old audio to the mix.
    """
    if seg.audio.render(TRIMMED) is None:
        return
    seg.audio.drop_renders((TRIMMED,))
    seg.audio.invalidate_after(TRIMMED)
    if raw is not None and raw.path:
        seg.audio_clip = Path(raw.path)


def prepare(seg, options: dict | None = None, cancel=None) -> SpeechPreparation:
    """Analyze the selected raw take and register a trimmed derivative.

    Returns the recorded decision. The raw take is never modified; the
    derivative is a separate artifact that reruns reuse rather than re-trim.
    """
    settings = _settings(options)
    raw = seg.audio.raw()
    if not settings["trim"]:
        seg.preparation = SpeechPreparation(
            take_id=raw.fingerprint if raw else "",
            detector=DETECTOR, decision="bypassed",
            reason="boundaries.trim is off")
        _forget_trim(seg, raw)
        return seg.preparation
    if raw is None or not raw.exists():
        seg.preparation = SpeechPreparation(
            detector=DETECTOR, decision="unknown",
            reason="no raw take is available to analyze")
        return seg.preparation
    if "silence" in seg.issues:
        # The raw checks already rejected this generation. Trimming it would
        # only hide the failure behind a short, plausible-looking clip.
        seg.preparation = SpeechPreparation(
            take_id=raw.fingerprint, detector=DETECTOR, decision="empty",
            reason="raw checks reported silence")
        return seg.preparation

    source = Path(raw.path)
    analysed = _pcm(source, cancel)
    try:
        bounds = measure(analysed, settings["threshold_db"])
    except (wave.Error, EOFError, OSError) as exc:
        seg.preparation = SpeechPreparation(
            take_id=raw.fingerprint, detector=DETECTOR, decision="unknown",
            reason=f"take could not be analyzed: {exc}")
        return seg.preparation
    finally:
        if analysed != source:
            analysed.unlink(missing_ok=True)

    prepared = decide(bounds, seg.duration, settings)
    prepared.take_id = raw.fingerprint
    # An intended opening wait is a placement decision, not generator padding:
    # it is re-inserted after the trim so the line still speaks when it should.
    prepared.onset = seg.placement.onset
    seg.preparation = prepared
    if prepared.decision != "trimmed":
        _forget_trim(seg, raw)
        return prepared

    request = {"source": stamp(source), "lead": prepared.lead, "tail": prepared.tail,
               "onset": prepared.onset or 0.0, "detector": DETECTOR, "version": 1}
    dest = source.parent / "prepared" / f"{source.stem}.{digest(request)[:12]}.wav"
    if not matches([dest], request):
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".partial.wav")
        end = max(prepared.lead, bounds.duration - prepared.tail)
        chain = [f"atrim=start={prepared.lead:g}:end={end:g}", "asetpts=PTS-STARTPTS"]
        if prepared.onset:
            chain.append(f"adelay={round(prepared.onset * 1000)}:all=1")
        run_ffmpeg(["-y", "-i", str(source), "-af", ",".join(chain),
                    "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
        temp.replace(dest)
        record([dest], request)
    seg.audio.put_render(Artifact(
        role=TRIMMED,
        path=str(dest),
        fingerprint=processing_fingerprint({
            "input": raw.fingerprint, "lead": prepared.lead, "tail": prepared.tail,
            "onset": prepared.onset or 0.0, "detector": DETECTOR, "version": 1}),
        derived_from=RAW,
        duration=(bounds.duration - prepared.trimmed) + (prepared.onset or 0.0),
        bytes=dest.stat().st_size if dest.exists() else None,
    ))
    seg.audio_clip = dest
    return prepared


def edge_peaks(path: Path, window: float = EDGE_WINDOW_SECONDS) -> tuple[float, float]:
    """Peak amplitude in the first and last `window` seconds, 0..1."""
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise wave.Error("edge analysis requires 16-bit PCM")
        rate = audio.getframerate()
        count = audio.getnframes()
        span = max(1, min(count, int(round(rate * window))))

        def peak(position: int) -> float:
            audio.setpos(position)
            samples = array.array("h", audio.readframes(span))
            if sys.byteorder != "little":
                samples.byteswap()
            return max((abs(v) for v in samples), default=0) / 32768

        head = peak(0)
        tail = peak(max(0, count - span))
    return head, tail


# An interrupted line keeps its hard stop: only this much, enough that the
# cut does not click.
CUT_FADE_SECONDS = 0.008


def finish_edges(job, options: dict | None = None, cancel=None, dry_run: bool = False,
                 endings: dict[str, str] | None = None) -> None:
    """Ease in and out only the edges that would click, on the final render.

    `endings` (from doblarr.decisions) marks lines that end on purpose: an
    `interrupted` line keeps its hard stop, a `trailing` one fades out for
    twice as long. The older single fade (`edge_fade_ms`) ignores them.
    """
    endings = endings or {}
    settings = _settings(options)
    fade_in_at, fade_out_at, curve = edge_fades(settings)
    if dry_run or (fade_in_at <= 0 and fade_out_at <= 0):
        if not dry_run:
            log.info("edge fades disabled")
        return
    threshold = 10 ** (settings["edge_threshold_db"] / 20)
    faded = 0
    for seg in job.segments:
        # Checked per cue, not only inside ffmpeg: a run whose clips are all
        # cached would otherwise ignore a cancel until it finished the list.
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during edge preparation")
        upstream = seg.audio.upstream_of(EDGED)
        if upstream is None or not upstream.exists():
            continue
        source = Path(upstream.path)
        analysed = _pcm(source, cancel)
        try:
            head, tail = edge_peaks(analysed)
            with wave.open(str(analysed), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError, OSError):
            continue
        finally:
            if analysed != source:
                analysed.unlink(missing_ok=True)
        # A fade can never eat more than a tenth of the clip, so a very short
        # utterance is shaped rather than swallowed.
        fade_in = min(fade_in_at, duration / 10) if head > threshold else 0.0
        out_at = fade_out_at
        if curve != "tri" and endings.get(seg.cue_id) == "interrupted":
            out_at = min(fade_out_at, CUT_FADE_SECONDS)
        elif curve != "tri" and endings.get(seg.cue_id) == "trailing":
            out_at = min(MAX_FADE_OUT_SECONDS, fade_out_at * 2)
        fade_out = min(out_at, duration / 10) if tail > threshold else 0.0
        if duration <= 0 or (fade_in <= 0 and fade_out <= 0):
            # Already smooth at both ends: an audible new fade would only
            # soften a consonant that was fine.
            seg.audio.drop_renders((EDGED,))
            seg.audio_clip = source
            continue
        # The linear curve keeps the key it always had, so an existing
        # edge_fade_ms render is still found in the cache.
        shape = {} if curve == "tri" else {"curve": curve}
        request = {"source": stamp(source), "in": round(fade_in, 4),
                   "out": round(fade_out, 4), "version": 1, **shape}
        dest = source.parent / "edges" / f"{source.stem}.{digest(request)[:12]}.wav"
        if not matches([dest], request):
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_suffix(".partial.wav")
            chain = []
            bend = "" if curve == "tri" else f":curve={curve}"
            if fade_in:
                chain.append(f"afade=t=in:st=0:d={fade_in:g}{bend}")
            if fade_out:
                chain.append(f"afade=t=out:st={max(0.0, duration - fade_out):g}"
                             f":d={fade_out:g}{bend}")
            run_ffmpeg(["-y", "-i", str(source), "-af", ",".join(chain),
                        "-c:a", "pcm_s16le", str(temp)], cancel=cancel)
            temp.replace(dest)
            record([dest], request)
        seg.audio.put_render(Artifact(
            role=EDGED,
            path=str(dest),
            fingerprint=processing_fingerprint({
                "input": upstream.fingerprint, "in": round(fade_in, 4),
                "out": round(fade_out, 4), "version": 1, **shape}),
            derived_from=upstream.role,
            duration=duration,
            bytes=dest.stat().st_size if dest.exists() else None,
        ))
        seg.audio_clip = dest
        faded += 1
    job.metrics["edge_fades"] = faded
    log.info("edge fades applied to %d/%d clips", faded, len(job.segments))

