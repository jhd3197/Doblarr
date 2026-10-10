"""Finishing an interrupted dub from what it already produced (docs/episode-recovery.md).

A reviewed script, an explicit cast and existing stems are enough to render
an episode again without transcribing, translating or separating it anew.
scripts/finish_episode.py drives these from the command line.
"""

from __future__ import annotations

import json
import math
from dataclasses import fields
from pathlib import Path

from .artifacts import digest
from .cues import RAW, Artifact, Selection, Take, now
from .discovery import lang_name
from .ffmpeg import run_ffmpeg
from .models import DubJob, Segment, Speaker
from .stages import mux
from .stages.prepare import interjection


def load_reviewed_script(job: DubJob, path: Path) -> None:
    """Explicitly reuse reviewed translations without re-running transcription."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    identity = payload.get("identity", {})
    if (Path(identity.get("input", "")).resolve() != job.input_file.resolve()
            or identity.get("target_lang") != job.target_lang):
        raise ValueError("reviewed script must match the input video and target language")
    allowed = {f.name for f in fields(Segment)} - {"audio_clip"}
    segments = [Segment(**{k: v for k, v in s.items() if k in allowed})
                for s in payload["segments"]]
    if not segments or len({s.index for s in segments}) != len(segments):
        raise ValueError("reviewed script needs unique, nonempty segment IDs")
    for seg in segments:
        if (not math.isfinite(seg.start) or not math.isfinite(seg.end)
                or seg.start < 0 or seg.end <= seg.start or not seg.text_translated):
            raise ValueError(f"invalid reviewed dialogue on line {seg.index}")
    job.segments = segments
    job.script_lang = payload.get("script_lang") or identity.get("source_lang")


def apply_cast(job: DubJob, payload: dict) -> dict:
    """Require exactly one explicit character assignment for every spoken cue.

    An entry is either a preset (`voice`) or a clone of the original actor
    (`clone: true`, optionally with `reference_line` or `reference: low`, and a
    `fallback_voice` for a character with no clean reference).
    """
    assignments = {}
    cast = payload.get("speakers", {})
    for label, entry in cast.items():
        if not entry.get("voice") and not entry.get("clone"):
            raise ValueError(f"missing voice profile for {label}")
        for index in entry.get("segments", []):
            if index in assignments:
                raise ValueError(f"duplicate cast assignment for line {index}")
            assignments[index] = label
    expected = {s.index for s in job.segments}
    if set(assignments) != expected:
        raise ValueError(f"cast must cover exactly the spoken cues; "
                         f"missing={sorted(expected - set(assignments))}, "
                         f"unknown={sorted(set(assignments) - expected)}")
    job.speakers = {label: Speaker(label, voicebox_profile_id=(
                        None if entry.get("clone") else entry["voice"]))
                    for label, entry in cast.items()}
    for entry in cast.values():
        if entry.get("clone"):
            # A clone's profile is built by synthesize; a stale preset id left
            # in the file must not short-circuit that.
            entry.pop("voice", None)
    for seg in job.segments:
        seg.speaker = assignments[seg.index]
        seg.voice = None  # the reviewed cast takes precedence over old per-line overrides
    return cast


def retain_reactions(job: DubJob, work: Path) -> set[int]:
    """Use the original actor's own sound for lines that are only a reaction.

    "Tsk!" or "Heh heh" is not a line an engine can act — asked for "¡Tsk!" one
    produced a cheerful laugh. The sound is cut from the separated original
    dialogue and selected as the line's take, the same `restored` selection
    synthesize already keeps instead of generating over it.
    """
    kept: set[int] = set()
    if not job.vocals:
        return kept
    folder = work / "reactions"
    folder.mkdir(parents=True, exist_ok=True)
    for seg in job.segments:
        if not interjection(seg.text_src):
            continue
        dest = folder / f"line_{seg.index:04d}.wav"
        length = seg.end - seg.start + 0.1
        run_ffmpeg(["-y", "-ss", f"{max(0.0, seg.start - 0.05):.3f}", "-t", f"{length:.3f}",
                    "-i", str(job.vocals), "-ac", "1", "-ar", "48000",
                    "-af", f"afade=t=in:d=0.02,afade=t=out:st={max(0.0, length - 0.02):.3f}:d=0.02",
                    "-c:a", "pcm_s16le", str(dest)])
        fingerprint = digest({"retained": str(job.vocals), "start": seg.start,
                              "end": seg.end})[:16]
        identifier = f"retained-{fingerprint[:12]}"
        seg.audio.takes = [t for t in seg.audio.takes if t.take_id != identifier] + [Take(
            take_id=identifier, fingerprint=fingerprint, engine="retained-original",
            profile=seg.speaker, text=seg.text_translated or seg.text_src, origin="import",
            state="reused", raw=Artifact(role=RAW, path=str(dest), fingerprint=fingerprint,
                                         bytes=dest.stat().st_size))]
        seg.audio.selection = Selection(take_id=identifier, reason="restored",
                                        actor="reaction-retain", at=now())
        seg.audio_clip = dest
        kept.add(seg.index)
    job.metrics["reactions_retained"] = len(kept)
    return kept


def export_mp4(job: DubJob, output: Path, subtitles: Path, title: str | None = None) -> Path:
    """Portable copy with the dub selected and optional translated captions."""
    if output.resolve() == job.input_file.resolve():
        raise ValueError("MP4 output must not overwrite the original")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".partial.mp4")
    lang = mux.LANG3.get(job.target_lang, job.target_lang)
    run_ffmpeg([
        "-y", "-i", str(job.input_file), "-i", str(job.dubbed_track), "-i", str(subtitles),
        "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
        "-metadata:s:a:0", f"language={lang}", "-metadata:s:a:0",
        f"title={title or lang_name(job.target_lang) + ' AI preview'}",
        "-metadata:s:s:0", f"language={lang}", "-disposition:a:0", "default",
        "-disposition:s:0", "0", "-movflags", "+faststart", str(temp)])
    temp.replace(output)
    return output
