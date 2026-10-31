"""How a line moves inside itself: its energy curve, pauses and peaks.

`doblarr.levels` answers "how loud was this line, compared with ordinary
dialogue". This module answers the question an envelope needs: *where inside the
line* the energy goes. It is a separate measurement with its own name and units,
because the two are computed differently:

- ``levels`` reports ``dBFS-rms-speech``: the RMS of 20 ms frame **peaks**
  (max-of-channels). It is a robust loudness figure for short utterances, but a
  frame-peak RMS is not a sample RMS, and treating it as one would mislabel it.
- this module reports ``dBFS-rms-sample``: the true RMS of the samples in each
  25 ms window, every 10 ms, on a 16 kHz mean downmix (``ffmpeg -ac 1``).

Every number states its version (``FEATURES``), frame sizes, channel policy and
smoothing. A line with too little audio, no audio, or audio that is known to be
overlapped or contaminated returns an explicit quality state and ``None`` for
what it cannot know; it never returns a confident zero.

Audio is decoded in bounded chunks (``CHUNK`` seconds), so a two-hour movie is
never held in memory at once, and a chunk is decoded once for all the lines in it.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from pathlib import Path

from .ffmpeg import FFmpegError, run_ffmpeg_bytes

log = logging.getLogger("doblarr.features")

FEATURES = "line-features/1"
UNITS = "dBFS-rms-sample"
CHANNELS = "mean-downmix"
RATE = 16000
WINDOW = 0.025        # seconds per RMS window
HOP = 0.010           # seconds between windows
SMOOTH = 5            # windows in the centred power moving average (50 ms)
FLOOR_DB = -80.0      # silence is reported at this floor, never as -inf
ACTIVE_RANGE_DB = 25.0   # below the line's own loud part, a window is not speech
ACTIVE_ABS_DB = -55.0    # and nothing quieter than this is speech at all
MERGE_GAP = 0.08      # active runs closer than this are one run
MIN_RUN = 0.04        # shorter runs are clicks
PAUSE_MIN = 0.12      # a gap this long inside a line is a pause
PEAK_PROMINENCE_DB = 3.0
PEAK_DISTANCE = 0.15
CURVE_POINTS = 32
CURVE_FLOOR_REL = -24.0
MIN_ACTIVE = 0.30     # less speech than this cannot describe a shape
CLIP_LEVEL = 0.999
CHUNK = 300.0         # seconds decoded at a time
CHUNK_PAD = 30.0      # overlap so a line near a chunk edge fits in one chunk


def describe() -> dict:
    """The measurement contract, recorded with every result."""
    return {"version": FEATURES, "units": UNITS, "channels": CHANNELS, "rate": RATE,
            "window": WINDOW, "hop": HOP, "smoothing_windows": SMOOTH,
            "floor_db": FLOOR_DB, "active_range_db": ACTIVE_RANGE_DB,
            "active_abs_db": ACTIVE_ABS_DB, "pause_min": PAUSE_MIN,
            "curve_points": CURVE_POINTS}


def decode(path: Path, start: float = 0.0, duration: float | None = None, cancel=None):
    """16 kHz mono float32 samples of one stretch of a file."""
    import numpy as np

    args = ["-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(path)]
    if duration is not None:
        args += ["-t", f"{max(0.01, duration):.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    try:
        raw = run_ffmpeg_bytes(args, cancel=cancel)
    except FFmpegError as exc:
        # No samples measure as "missing" (see measure), never as silence.
        log.warning("features: could not decode %s: %s", Path(path).name, exc)
        raw = b""
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class ChunkedAudio:
    """Bounded random access to a long file: decodes CHUNK-second windows on
    demand and keeps the two most recent ones."""

    def __init__(self, path: Path, cancel=None, chunk: float = CHUNK, keep: int = 2):
        self.path = Path(path)
        self.cancel = cancel
        self.chunk = chunk
        self.keep = keep
        self._chunks: OrderedDict[int, object] = OrderedDict()
        self.decoded_seconds = 0.0

    def _get(self, n: int):
        if n in self._chunks:
            self._chunks.move_to_end(n)
            return self._chunks[n]
        start = n * self.chunk
        samples = decode(self.path, start, self.chunk + CHUNK_PAD, self.cancel)
        self.decoded_seconds += len(samples) / RATE
        self._chunks[n] = samples
        while len(self._chunks) > self.keep:
            self._chunks.popitem(last=False)
        return samples

    def span(self, start: float, end: float):
        """Samples of [start, end); empty when the file has none there."""
        import numpy as np

        start = max(0.0, start)
        if end <= start:
            return np.zeros(0, dtype=np.float32)
        n = int(start // self.chunk)
        if end - n * self.chunk > self.chunk + CHUNK_PAD:
            # Longer than a chunk can hold: read it directly, still bounded by the span.
            samples = decode(self.path, start, end - start, self.cancel)
            self.decoded_seconds += len(samples) / RATE
            return samples
        samples = self._get(n)
        offset = start - n * self.chunk
        return samples[int(offset * RATE):int((offset + (end - start)) * RATE)]


def _db(power: float) -> float:
    return FLOOR_DB if power <= 1e-12 else max(FLOOR_DB, 10 * math.log10(power))


def energy_curve(samples) -> list[float]:
    """Smoothed per-window sample RMS in dBFS, one value per HOP."""
    import numpy as np

    window, hop = int(WINDOW * RATE), int(HOP * RATE)
    if len(samples) < window:
        return []
    count = 1 + (len(samples) - window) // hop
    squared = np.asarray(samples, dtype=np.float64) ** 2
    cumulative = np.concatenate([[0.0], np.cumsum(squared)])
    starts = np.arange(count) * hop
    power = (cumulative[starts + window] - cumulative[starts]) / window
    if SMOOTH > 1 and len(power) >= SMOOTH:
        kernel = np.ones(SMOOTH) / SMOOTH
        padded = np.pad(power, (SMOOTH // 2, SMOOTH - 1 - SMOOTH // 2), mode="edge")
        power = np.convolve(padded, kernel, mode="valid")
    return [round(_db(float(p)), 2) for p in power]


def active_runs(curve: list[float]) -> list[tuple[float, float]]:
    """Speech-active intervals, in seconds from the start of the curve."""
    if not curve:
        return []
    ordered = sorted(curve)
    loud = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    limit = max(loud - ACTIVE_RANGE_DB, ACTIVE_ABS_DB)
    runs: list[list[float]] = []
    for i, value in enumerate(curve):
        if value < limit:
            continue
        t = i * HOP
        if runs and t - runs[-1][1] <= MERGE_GAP:
            runs[-1][1] = t + HOP
        else:
            runs.append([t, t + HOP])
    return [(round(a, 3), round(b + WINDOW - HOP, 3)) for a, b in runs if b - a >= MIN_RUN]


def peaks(curve: list[float], runs: list[tuple[float, float]]) -> list[dict]:
    """Prominent local maxima inside active speech, strongest first."""
    found = []
    distance = max(1, int(PEAK_DISTANCE / HOP))
    for start, end in runs:
        a, b = int(start / HOP), min(len(curve), int(end / HOP) + 1)
        for i in range(a, b):
            left = curve[max(a, i - distance):i]
            right = curve[i + 1:min(b, i + distance + 1)]
            if (left and max(left) >= curve[i]) or (right and max(right) > curve[i]):
                continue
            base = min(curve[max(a, i - 3 * distance):min(b, i + 3 * distance + 1)])
            prominence = curve[i] - base
            if prominence >= PEAK_PROMINENCE_DB:
                found.append({"t": round(i * HOP, 3), "db": curve[i],
                              "prominence": round(prominence, 2)})
    return sorted(found, key=lambda p: -p["db"])[:8]


def shape(curve: list[float], runs: list[tuple[float, float]]) -> tuple[list[float], float]:
    """The curve over the active span, resampled to CURVE_POINTS values in dB
    relative to the line's mean active level, and that mean."""
    if not runs:
        return [], FLOOR_DB
    a, b = int(runs[0][0] / HOP), min(len(curve), max(int(runs[-1][1] / HOP), 1))
    span = curve[a:b] or curve
    active = [v for i, v in enumerate(curve) if any(s <= i * HOP < e for s, e in runs)]
    mean_power = sum(10 ** (v / 10) for v in active) / max(1, len(active))
    mean_db = _db(mean_power)
    points = []
    for k in range(CURVE_POINTS):
        position = k * (len(span) - 1) / max(1, CURVE_POINTS - 1)
        low, high = int(math.floor(position)), int(math.ceil(position))
        value = span[low] + (span[high] - span[low]) * (position - low)
        points.append(round(max(CURVE_FLOOR_REL, value - mean_db), 2))
    return points, round(mean_db, 2)


def measure(samples, *, flags: list[str] | None = None) -> dict:
    """Features of one line's samples. `flags` are known problems (overlap,
    contamination) that downgrade the quality state without hiding the numbers."""
    import numpy as np

    flags = list(flags or [])
    duration = round(len(samples) / RATE, 3)
    if not len(samples):
        return {"version": FEATURES, "quality": "missing", "duration": duration,
                "reasons": ["no audio in this span"], "flags": flags}
    peak = float(np.max(np.abs(samples)))
    curve = energy_curve(samples)
    runs = active_runs(curve)
    active = round(sum(b - a for a, b in runs), 3)
    pauses = [{"start": round(runs[i][1], 3), "end": round(runs[i + 1][0], 3),
               "seconds": round(runs[i + 1][0] - runs[i][1], 3)}
              for i in range(len(runs) - 1) if runs[i + 1][0] - runs[i][1] >= PAUSE_MIN]
    found = peaks(curve, runs)
    curve32, mean_db = shape(curve, runs)
    reasons = []
    quality = "ok"
    if peak >= CLIP_LEVEL:
        flags.append("clipped")
    if active < MIN_ACTIVE:
        quality = "insufficient"
        reasons.append(f"only {active:.2f}s of active speech")
    elif {"overlap", "contaminated"} & set(flags):
        quality = "contaminated"
        reasons += [f for f in flags if f in ("overlap", "contaminated")]
    span = (runs[-1][1] - runs[0][0]) if runs else 0.0
    return {
        "version": FEATURES, "quality": quality, "reasons": reasons, "flags": flags,
        "duration": duration, "active_seconds": active,
        "active_span": [runs[0][0], runs[-1][1]] if runs else None,
        "intervals": [list(r) for r in runs], "pauses": pauses,
        "peaks": [{**p, "position": round((p["t"] - runs[0][0]) / span, 3)
                   if span > 0 else 0.0} for p in found],
        "mean_db": mean_db if runs else None,
        "max_db": max(curve) if curve else None,
        "range_db": round(max(curve32) - min(curve32), 2) if curve32 else None,
        "sample_peak": round(peak, 4), "curve": curve32,
        "frames": curve[::2],     # 20 ms resolution for display and fitting
    }


def bed_relation(bed: ChunkedAudio | None, start: float, end: float,
                 speech_db: float | None) -> dict:
    """How loud the background is under a line, and the speech margin over it."""
    if bed is None:
        return {"state": "no-bed"}
    samples = bed.span(start, end)
    if not len(samples):
        return {"state": "missing"}
    import numpy as np

    rms = float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))
    bed_db = round(_db(rms * rms), 2)
    return {"state": "measured", "bed_db": bed_db,
            "margin_db": round(speech_db - bed_db, 2) if speech_db is not None else None}


def measure_lines(audio: Path, spans: list[tuple[float, float]], *,
                  flags: list[list[str]] | None = None, bed: Path | None = None,
                  cancel=None, progress=None) -> tuple[list[dict], dict]:
    """Features for many spans of one file, decoded chunk by chunk."""
    reader = ChunkedAudio(audio, cancel)
    bed_reader = ChunkedAudio(bed, cancel) if bed and Path(bed).is_file() else None
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    results: list[dict | None] = [None] * len(spans)
    for done, i in enumerate(order):
        if cancel is not None and cancel.is_set():
            from .errors import JobCancelled

            raise JobCancelled("cancelled during line features")
        start, end = spans[i]
        result = measure(reader.span(start, end), flags=(flags or [[]] * len(spans))[i])
        result["bed"] = bed_relation(bed_reader, start, end, result.get("mean_db"))
        results[i] = result
        if progress is not None and done % 25 == 0:
            progress(done, len(spans), f"line {done}/{len(spans)}")
    stats = {"decoded_seconds": round(reader.decoded_seconds, 1),
             "bed_decoded_seconds": round(bed_reader.decoded_seconds, 1) if bed_reader else 0.0}
    return [r or {} for r in results], stats


def measure_file(path: Path) -> dict:
    """Features of a whole (short) file, such as one generated take."""
    return measure(decode(Path(path)))
