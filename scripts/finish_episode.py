"""Resume one subtitle-led dub with existing stems and an explicit voice profile.

Run from the repository root with the project's Python environment.
All media and receipts stay in the ignored work/output directories.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import fields, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doblarr.clients.translator import PromptureTranslator  # noqa: E402
from doblarr.clients.voicebox import VoiceboxClient  # noqa: E402
from doblarr.config import Config  # noqa: E402
from doblarr.discovery import lang_name  # noqa: E402
from doblarr.ffmpeg import run_ffmpeg  # noqa: E402
from doblarr.models import DubJob, Segment, Speaker  # noqa: E402
from doblarr.stages import fit_timing, mix, mux, quality, synthesize, transcribe  # noqa: E402
from doblarr.stages.common import save_script  # noqa: E402
from doblarr.versions import preserve_version  # noqa: E402


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
    """Require exactly one explicit character assignment for every spoken cue."""
    assignments = {}
    cast = payload.get("speakers", {})
    for label, entry in cast.items():
        if not entry.get("voice"):
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
    job.speakers = {label: Speaker(label, voicebox_profile_id=entry["voice"])
                    for label, entry in cast.items()}
    for seg in job.segments:
        seg.speaker = assignments[seg.index]
        seg.voice = None  # the reviewed cast takes precedence over old per-line overrides
    return cast


def export_mp4(job: DubJob, output: Path, subtitles: Path, title: str | None = None) -> Path:
    """Portable copy with the dub selected and optional translated captions."""
    if output.resolve() == job.input_file.resolve():
        raise ValueError("MP4 output must not overwrite the original")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".partial.mp4")
    lang = mux._LANG3.get(job.target_lang, job.target_lang)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--subs", type=Path, required=True)
    parser.add_argument("--stems-dir", type=Path, default=Path("work"))
    parser.add_argument("--work-dir", type=Path, default=Path("work/episode-es"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--profile", help="Single-voice fallback; use --cast-file for characters")
    parser.add_argument("--cast-file", type=Path, help="Explicit speaker profiles and cue indices")
    parser.add_argument("--script", type=Path, help="Previously reviewed translated script JSON")
    parser.add_argument("--normalize", action="store_true", help="Normalize dialogue loudness")
    parser.add_argument("--track-name", default="{language_name} AI (preset preview)")
    parser.add_argument("--engine", default="kokoro")
    parser.add_argument("--model-size")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--verify-speech", choices=["off", "suspicious", "all"], default="off")
    parser.add_argument("--repair-timing", action="store_true")
    parser.add_argument("--version-name")
    parser.add_argument("--from", dest="source", default="en",
                        help="Language of the supplied subtitle script")
    parser.add_argument("--to", dest="target", default="es")
    parser.add_argument("--model", default="ollama/qwen3:8b")
    parser.add_argument("--script-edits", type=Path,
                        help="JSON with exclude indices and per-index text/start/end edits")
    parser.add_argument("--mp4", type=Path, help="Also write an MP4 with the Spanish dub selected")
    args = parser.parse_args()
    if not args.profile and not args.cast_file:
        parser.error("provide --profile or --cast-file")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)
    job = DubJob(args.input.resolve(), args.source, args.target, args.subs.resolve())
    for attr, suffix in [("source_audio", "source"), ("vocals", "vocals"),
                         ("background", "background")]:
        path = (args.stems_dir / f"{args.input.stem}.{suffix}.wav").resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        setattr(job, attr, path)
    if args.script:
        load_reviewed_script(job, args.script)
    else:
        transcribe.run(job, work)
    job.speakers = {"NARRATOR": Speaker("NARRATOR", voicebox_profile_id=args.profile)}
    for seg in job.segments:
        seg.speaker = "NARRATOR"
    save_script(job, work)
    translator = PromptureTranslator(args.model)
    translator.direction = dict(Config.load()["translate"])
    pending = [s for s in job.segments if not s.text_translated]
    for offset in range(0, len(pending), 12):
        batch = pending[offset:offset + 12]
        text = "\n".join(s.text_src for s in batch)
        result = translator.translate(text, job.script_lang or job.source_lang, job.target_lang)
        lines = result.splitlines()
        if len(lines) != len(batch):
            raise ValueError("translation changed the number of subtitle lines")
        for seg, line in zip(batch, lines, strict=True):
            seg.text_translated = line
        save_script(job, work)
        logging.info("Translation checkpoint: %d/%d", sum(bool(s.text_translated)
                     for s in job.segments), len(job.segments))
    if args.script_edits:
        edits = json.loads(args.script_edits.read_text(encoding="utf-8"))
        excluded = set(edits.get("exclude", []))
        job.segments = [s for s in job.segments if s.index not in excluded]
        for seg in job.segments:
            edit = edits.get("lines", {}).get(str(seg.index), {})
            if "text" in edit:
                seg.text_translated = edit["text"]
            for field in ("start", "end"):
                if field in edit:
                    setattr(seg, field, float(edit[field]))
        # Keep the original translation cache intact for later edit revisions.
        save_script(job, work / "effective")
    cast = apply_cast(job, json.loads(args.cast_file.read_text(encoding="utf-8"))) \
        if args.cast_file else None
    save_script(job, work / "effective")
    vb = VoiceboxClient("http://127.0.0.1:17493", timeout=args.timeout)

    def render(segments=None):
        target = job if segments is None else replace(job, segments=segments)
        synthesize.run(target, vb, work, voice_mode="preset", engine=args.engine, cast=cast,
                       narrator_speakers=["NARRATOR"], model_size=args.model_size, seed=args.seed)

    def check(segments=None, retries=1):
        target = job if segments is None else replace(job, segments=segments)
        quality.run(target, vb, normalize=args.normalize, asr=args.verify_speech,
                    max_retries=retries, regenerate=lambda seg: render([seg]))

    def regenerate(seg):
        render([seg])
        check([seg], retries=0)

    render()
    if args.normalize or args.verify_speech != "off":
        check()
    fit_timing.run(job, work, translator=translator if args.repair_timing else None,
                   regenerate=regenerate if args.repair_timing else None,
                   checkpoint=lambda: save_script(job, work / "effective"), max_attempts=2)
    mix.run(job, work, force=bool(args.script_edits))
    # Encode separately: FFmpeg 7 can submit invalid attachment packets when
    # transcoding audio while copying this older Matroska source's font stream.
    # A stream-copy mux preserves the original subtitle font and video intact.
    encoded_dub = job.dubbed_track.with_suffix(".m4a")
    encoded_temp = encoded_dub.with_suffix(".partial.m4a")
    run_ffmpeg(["-y", "-i", str(job.dubbed_track), "-c:a", "aac", "-b:a", "192k",
                str(encoded_temp)])
    encoded_temp.replace(encoded_dub)
    job.dubbed_track = encoded_dub
    mux.run(job, args.output_dir, track_name_template=args.track_name,
            force=bool(args.script_edits), audio_codec="copy")
    import pysubs2
    subs = pysubs2.SSAFile()
    for seg in job.segments:
        subs.events.append(pysubs2.SSAEvent(start=round(seg.start * 1000),
                           end=round(seg.end * 1000), text=seg.text_translated or ""))
    sub_output = args.output_dir / f"{job.input_file.stem}.{job.target_lang}.srt"
    subs.save(str(sub_output))
    title = args.track_name.format(language_name=lang_name(job.target_lang),
                                   language=job.target_lang.upper())
    mp4 = export_mp4(job, args.mp4, sub_output, title) if args.mp4 else None
    if args.version_name:
        config = Config.load().with_overrides({
            "dub.version_name": args.version_name, "dub.voice_mode": "preset",
            "dub.track_name_template": args.track_name,
            "voicebox.default_engine": args.engine, "voicebox.model_size": args.model_size,
            "voicebox.seed": args.seed, "quality.asr": args.verify_speech,
            "quality.normalize": args.normalize,
        })
        preserve_version(job, config, cast=[dict(entry, speaker_id=label)
                                           for label, entry in (cast or {}).items()])
    report = {"input": str(job.input_file), "output": str(job.output_file.resolve()),
              "segments": len(job.segments), "engine": args.engine,
              "profile": args.profile,
              "cast": cast,
              "metrics": job.metrics,
              "version_id": job.version_id, "translation_id": job.translation_id,
              "version_file": str(job.version_file) if job.version_file else None,
              "mp4": str(mp4.resolve()) if mp4 else None,
              "subtitles": str(sub_output.resolve()),
              "script": str(save_script(job, work / "effective")),
              "limitations": ["Preset character voices; not actor voice clones" if cast else
                               "Single preset voice; not an actor voice clone",
                              "Translation and performance require listening review"]}
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    logging.info("Finished: %s", job.output_file)


if __name__ == "__main__":
    main()
