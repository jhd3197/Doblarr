"""Select varied dialogue and separate only a short montage for voice auditions."""

import sys
import wave
from array import array
from dataclasses import replace

from ..artifacts import matches, record, stamp
from ..cues import MONTAGE, SOURCE, Placement, Span
from ..ffmpeg import run_ffmpeg
from .common import work_stem
from .fit_timing import _duration


def select_segments(job, count=8):
    candidates = [s for s in job.segments if 1 <= s.duration <= 15]
    if not candidates:
        raise ValueError("no 1–15 second dialogue samples available for an audition")
    count = max(1, min(24, int(count)))
    selected = {}
    by_speaker = {}
    for seg in candidates:
        if seg.speaker not in by_speaker or seg.duration > by_speaker[seg.speaker].duration:
            by_speaker[seg.speaker] = seg
    for seg in list(by_speaker.values())[: max(1, count - 3)]:
        selected[seg.index] = seg
    fastest = max(candidates, key=lambda s: len(s.text_src) / s.duration)
    selected[fastest.index] = fastest
    # Sample energy across the episode; this adds quiet and loud source passages.
    try:
        with wave.open(str(job.source_audio), "rb") as source:
            energies = []
            if source.getsampwidth() == 2:
                for seg in candidates[:: max(1, len(candidates) // 64)]:
                    source.setpos(
                        min(source.getnframes(), round(seg.start * source.getframerate()))
                    )
                    values = array("h", source.readframes(source.getframerate() // 2))
                    if sys.byteorder != "little":
                        values.byteswap()
                    energies.append((sum(v * v for v in values) / max(1, len(values)), seg))
                if energies:
                    for _, seg in [
                        min(energies, key=lambda x: x[0]),
                        max(energies, key=lambda x: x[0]),
                    ]:
                        selected[seg.index] = seg
    except (OSError, EOFError, wave.Error):
        pass
    for i in range(count):
        seg = candidates[round(i * (len(candidates) - 1) / max(1, count - 1))]
        selected.setdefault(seg.index, seg)
    return sorted(list(selected.values())[:count], key=lambda s: s.start)


def run(job, work, count=8, cancel=None, force=False, dry_run=False):
    if dry_run:
        return
    chosen = select_segments(job, count)
    source = job.source_audio
    duration = _duration(source, cancel)
    windows = [(max(0, s.start - 0.25), min(duration, s.end + 0.25)) for s in chosen]
    dest = work / f"{work_stem(job)}.samples.wav"
    request = {"source": stamp(source), "windows": windows}
    # JSON manifests store tuples as lists.
    request["windows"] = [list(w) for w in windows]
    if not matches([dest], request, force):
        filters = [f"[0:a]asplit={len(windows)}" + "".join(f"[s{i}]" for i in range(len(windows)))]
        for i, (start, end) in enumerate(windows):
            filters.append(f"[s{i}]atrim=start={start:g}:end={end:g},asetpts=PTS-STARTPTS[c{i}]")
        filters.append(
            "".join(f"[c{i}]" for i in range(len(windows)))
            + f"concat=n={len(windows)}:v=0:a=1[out]"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".partial.wav")
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(source),
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[out]",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(temp),
            ],
            cancel=cancel,
        )
        temp.replace(dest)
        record([dest], request)
    remapped = []
    offset = 0.0
    for seg, (start, end) in zip(chosen, windows, strict=True):
        copy = replace(
            seg,
            source_start=seg.start,
            start=seg.start - start + offset,
            end=seg.end - start + offset,
            words=[],
        )
        # The montage is its own timeline. The cue keeps its original source
        # interval, so a montage never becomes the record of where the line is.
        copy.placement = Placement(montage=Span(copy.start, copy.end, MONTAGE))
        if not copy.source.spans and seg.end > seg.start >= 0:
            copy.source.spans = [Span(seg.start, seg.end, SOURCE)]
        remapped.append(copy)
        offset += end - start
    job.segments = remapped
    # Deliberately not touching job.source_reference or job.source_track: the
    # montage replaces the working audio, never the record of which original
    # track this came from or where the original performance can be heard.
    job.source_track = job.source_track or source
    job.source_audio = dest
    job.speakers = {s.speaker: job.speakers[s.speaker] for s in remapped}
    job.metrics["audition_seconds"] = offset
