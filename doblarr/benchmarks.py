"""Deterministic fixtures and a baseline recorder for audio-quality work.

The generated media is tone, not speech. It exists to pin down numeric
invariants — durations, levels, cue identity, cache reuse, request counts — so a
later plan can prove it changed only what it meant to change. It cannot show
whether a performance sounds natural; that judgement needs real listening
material and is recorded separately in each plan's execution record.

Everything here runs offline: FFmpeg renders the fixture media and a stub
speech engine renders each line as a tone, so a baseline is reproducible on a
machine with no voicebox, no models and no network.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .cues import (
    FITTED,
    MONTAGE,
    NORMALIZED,
    RAW,
    SOURCE,
    Artifact,
    Placement,
    Selection,
    Span,
    Take,
    ensure_identity,
)
from .ffmpeg import run_ffmpeg
from .models import DubJob, Segment, Speaker
from .pipeline import run_job
from .services import Services
from .telemetry import write_json

SAMPLE_RATE = 48000
BENCHMARK_VERSION = 1

# Counters that legitimately differ between two machines or two fixture
# roots because they hash local file identity. Reported separately so a
# comparison does not read as a real change.
LOCAL_COUNTERS = frozenset({"mix_fingerprint"})


@dataclass(frozen=True)
class Cue:
    """One fixture line: its slot, the tone it speaks and how loud the source is."""

    start: float
    end: float
    text: str
    speaker: str
    # Seconds of silence the generated take opens and closes with. Quiet onsets
    # and tails are exactly what a boundary stage must not clip away.
    lead: float = 0.0
    tail: float = 0.0
    # Amplitude of the generated take, 0..1. Varied levels are what a
    # source-relative level plan has to reproduce.
    level: float = 0.3
    # Seconds of generated speech; longer than the slot means the timing stage
    # has real work to do.
    spoken: float = 0.0


# One scene covering the cases Plan 01 must keep reproducible: two speakers, an
# overlapping pair, a quiet onset and tail, a clip that overruns its slot, and a
# non-spoken cue that must survive as evidence without being synthesized.
SCENE: tuple[Cue, ...] = (
    Cue(1.0, 3.0, "Primera linea", "SPEAKER_00", level=0.30, spoken=1.8),
    Cue(3.2, 5.0, "Segunda linea", "SPEAKER_01", level=0.12, lead=0.35, tail=0.40, spoken=1.0),
    # Deliberately overlapping the cue above: two speakers talking across each
    # other is a case the mix and any future collision review must handle.
    Cue(4.5, 6.5, "Tercera linea encimada", "SPEAKER_00", level=0.45, spoken=1.9),
    Cue(7.0, 8.0, "Cuarta linea larga que no cabe", "SPEAKER_01", level=0.30, spoken=1.9),
    Cue(9.0, 10.5, "[laughter]", "SPEAKER_00", level=0.20, spoken=1.0),
)


def example_script(root: Path) -> DubJob:
    """A worked cue example: two speakers, an overlap, an edit and a montage.

    Every time in it is attached to a named domain, so nothing has to be
    inferred from context:

    - cue 0 and cue 1 belong to different speakers and their source intervals
      overlap, which is what genuinely happens when people talk across each
      other;
    - cue 2 has had its target window moved by review while its source interval
      stays exactly where the line was spoken;
    - cue 3 is placed inside an audition montage, so its target window is
      montage time while its source interval is still original-media time.

    Used by the tests and by `scripts/quality_baseline.py example`.
    """
    job = DubJob(input_file=Path(root) / "example.mkv", source_lang="ja", target_lang="es",
                 subtitle_file=Path(root) / "example.srt")
    job.speakers = {"GINKO": Speaker("GINKO"), "NUI": Speaker("NUI")}
    job.segments = [
        Segment(0, 1.0, 3.0, "Primera", speaker="GINKO", text_translated="Primera"),
        Segment(1, 2.5, 4.2, "Segunda", speaker="NUI", text_translated="Segunda"),
        Segment(2, 6.0, 8.0, "Tercera", speaker="GINKO", text_translated="Tercera"),
        Segment(3, 0.0, 1.7, "Cuarta", speaker="NUI", text_translated="Cuarta"),
    ]
    ensure_identity(job)

    # Two speakers whose source intervals overlap.
    job.segments[0].source.spans = [Span(1.0, 3.0, SOURCE)]
    job.segments[1].source.spans = [Span(2.5, 4.2, SOURCE)]
    for seg in job.segments[:2]:
        seg.source.method = "subtitle"
        seg.source.speaker = seg.speaker

    # An edited target position. The source interval is untouched: the line was
    # spoken at 5.4-7.4 whatever the reviewer does with its placement.
    moved = job.segments[2]
    moved.source.spans = [Span(5.4, 7.4, SOURCE)]
    moved.source.method = "subtitle"
    moved.placement = Placement(onset=0.12, offset=0.6)

    # An audition montage mapping: target time is montage time, and the cue's
    # original interval is still recorded in source time.
    montage = job.segments[3]
    montage.source.spans = [Span(11.2, 12.9, SOURCE)]
    montage.source.method = "subtitle"
    montage.source_start = 11.2
    montage.placement = Placement(montage=Span(0.0, 1.7, MONTAGE))

    # One cue carries a full audio chain so the artifact roles are exercised.
    first = job.segments[0]
    take = Take(take_id="take-a", fingerprint="gen-a", engine="tone", profile="tone-voice",
                text="Primera", state="generated",
                raw=Artifact(role=RAW, path=str(Path(root) / "raw.wav"), fingerprint="gen-a"))
    first.audio.takes.append(take)
    first.audio.selection = Selection(take_id="take-a", reason="auto")
    first.audio.put_render(Artifact(role=NORMALIZED, path=str(Path(root) / "norm.wav"),
                                    fingerprint="proc-a", derived_from=RAW))
    first.audio.put_render(Artifact(role=FITTED, path=str(Path(root) / "fit.wav"),
                                    fingerprint="proc-b", derived_from=NORMALIZED))
    return job


def _tone(path: Path, seconds: float, hz: float, amplitude: float,
          lead: float = 0.0, tail: float = 0.0) -> Path:
    """A reproducible 16-bit mono tone with optional silent lead-in/tail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(round(SAMPLE_RATE * (lead + seconds + tail))):
        position = index / SAMPLE_RATE
        value = 0.0
        if lead <= position < lead + seconds:
            value = amplitude * math.sin(2 * math.pi * hz * (position - lead))
        frames.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000)))
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        out.writeframes(b"".join(frames))
    return path


def write_media(root: Path, scene=SCENE, name: str = "fixture.mkv") -> tuple[Path, Path]:
    """Render fixture video with one tagged audio track, plus its subtitles.

    Needs a real FFmpeg: container muxing and stream tagging are exactly the
    behaviors a mocked command line would fail to prove.
    """
    root.mkdir(parents=True, exist_ok=True)
    duration = max(c.end for c in scene) + 2
    source = _tone(root / "fixture.source.wav", duration, 220.0, 0.25)
    media = root / name
    run_ffmpeg([
        "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=64x64:d={duration:g}:r=10",
        "-i", str(source),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le",
        "-metadata:s:a:0", "language=jpn",
        "-shortest", str(media),
    ])
    subtitles = root / "fixture.srt"
    subtitles.write_text("".join(
        f"{i + 1}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n\n"
        for i, c in enumerate(scene)
    ), encoding="utf-8")
    return media, subtitles


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    whole, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


class ToneEngine:
    """An offline stand-in for voicebox that renders each line as a tone.

    It counts requests, so a baseline can state plainly how many generations a
    run cost and a later plan can prove a processing-only change costs none.
    """

    def __init__(self, root: Path, scene=SCENE):
        self.root = Path(root)
        self.requests: list[dict] = []
        self.observer = None
        self.plan = {c.text: c for c in scene}

    # -- the surface synthesize.py actually uses -------------------------
    def list_voices(self) -> list[dict]:
        return [{"id": "tone-voice", "name": "Tone"}]

    def create_profile(self, name: str, language: str) -> str:
        return "tone-voice"

    def add_sample(self, profile_id: str, path: Path, text: str) -> None:
        return None

    def transcribe(self, path: Path, language: str = "") -> dict:
        return {"text": ""}

    def synthesize_to_file(self, profile_id, text, language, dest, cancel_event=None, **kwargs):
        self.requests.append({"profile": profile_id, "text": text, "options": kwargs})
        cue = self.plan.get(text)
        seconds = cue.spoken if cue and cue.spoken else 1.0
        return _tone(Path(dest), seconds, 330.0,
                     cue.level if cue else 0.3,
                     lead=cue.lead if cue else 0.0,
                     tail=cue.tail if cue else 0.0)


def observations(job: DubJob, engine: ToneEngine) -> dict:
    """What this run did, in terms a later plan can compare against."""
    lines: list[dict] = []
    for seg in job.segments:
        current = seg.audio.current()
        take = seg.audio.selected()
        lines.append({
            "cue_id": seg.cue_id,
            "index": seg.index,
            "origin": seg.lineage.origin,
            "speaker": seg.speaker,
            "slot": [seg.start, seg.end],
            "source_spans": [s.as_dict() for s in seg.source.spans],
            "take_id": take.take_id if take else None,
            "take_fingerprint": take.fingerprint if take else None,
            "takes": len(seg.audio.takes),
            "roles": [a.role for a in seg.audio.renders],
            "rendered_role": current.role if current else None,
            "rendered_fingerprint": current.fingerprint if current else None,
            "issues": sorted(seg.issues),
            "findings": sorted(
                (f.code, f.disposition) for f in seg.findings),
        })
    roles: set[str] = set()
    for line in lines:
        roles.update(line["roles"])
        if line["take_id"]:
            roles.add(RAW)
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "kind": job.kind,
        "segments": len(job.segments),
        "speakers": sorted(job.speakers),
        "nonverbal": job.nonverbal,
        "cue_lineage": job.cue_lineage,
        "tts_requests": len(engine.requests),
        "metrics": {k: v for k, v in sorted(job.metrics.items())
                    if k != "timing_translation_usage"},
        "source_reference": (job.source_reference.as_dict()
                             if job.source_reference else None),
        "roles_available": sorted(roles),
        "lines": lines,
    }


def baseline(root: Path, scene=SCENE, overrides: dict | None = None,
             media_root: Path | None = None) -> dict:
    """Render the fixture scene end to end offline and return the observations.

    `root` holds the work dir and the output dir, so a baseline never touches a
    real library. `media_root` holds the fixture media and defaults to a folder
    inside `root`; pass a stable one to compare two baselines, because cue
    identity is scoped to the source document's path, which is the point.
    """
    root = Path(root)
    media, subtitles = write_media(Path(media_root) if media_root else root / "media", scene)
    config = Config.load(root / "config.yaml").with_overrides({
        "paths.work_dir": str(root / "work"),
        "paths.output_dir": str(root / "output"),
        "dub.dry_run": False,
        "dub.voice_mode": "preset",
        "dub.preset_voices": ["tone-voice"],
        "dub.preserve_versions": False,
        "transcribe.diarize": False,
        "quality.asr": "off",
        **(overrides or {}),
    })
    engine = ToneEngine(root, scene)
    services = Services(config)
    services._cache["voicebox"] = engine  # the documented injection point

    job = DubJob(input_file=media, source_lang="ja", target_lang="es",
                 subtitle_file=subtitles)
    job.script_is_target = True  # the fixture text is already the target text
    run_job(job, config, services=services)
    return observations(job, engine)


def record(root: Path, destination: Path, scene=SCENE,
           overrides: dict | None = None, note: str = "",
           media_root: Path | None = None) -> Path:
    """Write a baseline observation file. Local only; never committed."""
    data = baseline(root, scene, overrides, media_root=media_root)
    data["note"] = note or (
        "Tone fixtures. Numeric invariants only — this proves nothing about "
        "how the dub sounds.")
    write_json(Path(destination), data)
    return Path(destination)


def compare(before: Path, after: Path) -> dict:
    """Differences between two baselines, per cue and per counter."""
    left = json.loads(Path(before).read_text(encoding="utf-8"))
    right = json.loads(Path(after).read_text(encoding="utf-8"))
    by_cue = {line["cue_id"]: line for line in left["lines"]}
    changed = []
    for line in right["lines"]:
        previous = by_cue.get(line["cue_id"])
        if previous is None:
            changed.append({"cue_id": line["cue_id"], "change": "added"})
            continue
        fields = {k: [previous[k], line[k]] for k in line if previous.get(k) != line[k]}
        if fields:
            changed.append({"cue_id": line["cue_id"], "change": "changed", "fields": fields})
    missing = [cue for cue in by_cue if cue not in {line["cue_id"] for line in right["lines"]}]
    moved = {k: [left["metrics"].get(k), right["metrics"].get(k)]
             for k in set(left["metrics"]) | set(right["metrics"])
             if left["metrics"].get(k) != right["metrics"].get(k)}
    return {
        "tts_requests": [left["tts_requests"], right["tts_requests"]],
        "removed_cues": missing,
        "changed_cues": changed,
        "changed_counters": {k: v for k, v in moved.items() if k not in LOCAL_COUNTERS},
        "changed_local_counters": {k: v for k, v in moved.items() if k in LOCAL_COUNTERS},
    }


def roles_present(job: DubJob) -> set[str]:
    """Artifact roles the run actually produced — a quick harness assertion."""
    found: set[str] = set()
    for seg in job.segments:
        if seg.audio.raw():
            found.add(RAW)
        found.update(a.role for a in seg.audio.renders)
    return found & {RAW, NORMALIZED, FITTED}
