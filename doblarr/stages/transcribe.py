"""Stage 3 — get timed dialogue segments.

Sources, in order of preference:
  - an external subtitle file (job.subtitle_file)
  - an embedded subtitle track (target language preferred → already the script)
  - whisper on the extracted audio (whisperx, or faster-whisper as fallback),
    selected via transcribe.source: whisper
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import subtitles
from ..cues import SOURCE, Span, ensure_identity
from ..discovery import ISO3_TO_ISO2
from ..model_pool import model as pooled_model
from ..models import DubJob, Segment
from .common import DryRunPlan, dry, load_script, stage

log = logging.getLogger("doblarr.transcribe")


@stage("transcribe")
def run(
    job: DubJob,
    work_dir: Path,
    source: str = "subtitles",
    whisper_model: str = "large-v3",
    vb=None,
    segment_limit: int | None = None,
    max_seconds: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    options: dict | None = None,
) -> DryRunPlan | None:
    if dry_run:
        return dry(f"would build timed segments ({source})")

    # Resume: a persisted script (transcript + speakers + translations) skips
    # this stage entirely — and diarize/translate downstream no-op on it.
    job.transcription_options = {
        k: v
        for k, v in {
            "source": source if source != "subtitles" else None,
            "segment_limit": segment_limit,
            "max_seconds": max_seconds,
        }.items()
        if v is not None
    }
    job.transcription_options.update(options or {})
    if source == "whisper":
        job.transcription_options["whisper_model"] = whisper_model
    if load_script(job, work_dir, force):
        log.info("transcribe: restored script from cache (%d segments)", len(job.segments))
        return None

    used_lang = None
    if source == "whisper":
        segs = _whisper_segments(job, whisper_model)
    else:
        sub_path = job.subtitle_file
        if not sub_path:
            # Try an embedded subtitle track — prefer the target language (already the script).
            streams = subtitles.sub_streams(job.input_file)
            chosen = (
                subtitles.pick_stream(streams, job.target_lang)
                or subtitles.pick_stream(streams, job.source_lang)
                or (next((s for s in streams if s["codec"] in subtitles._TEXT_CODECS), None))
            )
            if not chosen:
                raise RuntimeError(
                    "no subtitle track found (give --subs or set transcribe.source "
                    f"to 'whisper'); streams: {streams}"
                )
            used_lang = chosen["lang"]
            sub_path = subtitles.extract_srt(
                job.input_file, chosen["index"], work_dir / f"{job.input_file.stem}.{used_lang}.srt"
            )
            log.info("extracted embedded %s subtitles -> %s", used_lang, sub_path.name)

        import pysubs2  # lazy

        subs = pysubs2.load(str(sub_path))
        segs = [
            Segment(
                index=i,
                start=line.start / 1000.0,
                end=line.end / 1000.0,
                text_src=line.plaintext.replace("\n", " ").strip(),
            )
            for i, line in enumerate(subs)
            if line.plaintext.strip() and not line.is_comment
        ]
    if max_seconds:
        segs = [s for s in segs if s.start < max_seconds]  # tease window
    if segment_limit:
        segs = segs[:segment_limit]
    # Re-index after limiting so clip names stay 0..N. The cue's ordinal keeps
    # its position in the ORIGINAL document, so a tease and a full dub of the
    # same media agree on which cue is which.
    method = "asr" if source == "whisper" else "subtitle"
    for n, s in enumerate(segs):
        s.lineage.ordinal = s.index
        s.index = n
        s.source.method = method
        s.source.word_domain = SOURCE
        s.source.word_method = method if s.words else "unknown"
        if s.end > s.start >= 0:
            s.source.spans = [Span(s.start, s.end, SOURCE)]
    job.segments = segs
    ensure_identity(job)
    job.script_lang = ISO3_TO_ISO2.get(used_lang, used_lang) if used_lang else job.source_lang
    if source != "whisper" and (options or {}).get("align_subtitles"):
        if job.script_lang == job.source_lang:
            _align_script(job)
        else:
            log.warning("Cannot align translated subtitles directly to source-language speech")
            for seg in job.segments:
                seg.issues.append("alignment_unavailable")

    # If we used the target-language track, the text is already the translation.
    if used_lang and job.script_lang == job.target_lang:
        job.script_is_target = True
        for s in job.segments:
            s.text_translated = s.text_src

    log.info(
        "transcribe -> %d segments%s",
        len(job.segments),
        " (already target language)" if job.script_is_target else "",
    )
    return None


def _align_script(job):
    try:
        import torch
        import whisperx

        device = job.transcription_options.get("device", "auto")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        with pooled_model(
            ("align", job.source_lang, device),
            lambda: whisperx.load_align_model(language_code=job.source_lang, device=device),
            job.transcription_options.get("keep_models_loaded", False),
        ) as loaded:
            result = whisperx.align(
                [{"start": s.start, "end": s.end, "text": s.text_src} for s in job.segments],
                loaded[0],
                loaded[1],
                str(job.vocals or job.source_audio),
                device,
            )
            del loaded
        aligned = result["segments"]
        if len(aligned) != len(job.segments):
            raise ValueError("alignment changed the number of subtitle cues")
        for seg, line in zip(job.segments, aligned, strict=True):
            seg.start, seg.end = float(line["start"]), float(line["end"])
            seg.words = line.get("words", [])
    except Exception as exc:  # noqa: BLE001 -- alignment is optional; preserve the script
        log.warning("Subtitle alignment unavailable: %s", exc)
        for seg in job.segments:
            seg.issues.append("alignment_unavailable")


def _whisper_segments(job: DubJob, whisper_model: str) -> list[Segment]:
    """Transcribe the extracted dialogue with whisperx; fall back to faster-whisper."""
    audio = job.vocals if job.vocals and job.vocals.exists() else job.source_audio
    if audio is None or not audio.exists():
        raise RuntimeError(
            "transcribe.source is 'whisper' but no extracted audio exists yet — "
            "the extract stage must run first"
        )
    try:
        return _whisperx_segments(job, audio, whisper_model)
    except ImportError:
        pass
    try:
        return _faster_whisper_segments(job, audio, whisper_model)
    except ImportError:
        raise RuntimeError(
            "transcribe.source is 'whisper' but no whisper backend is installed. "
            "Install whisperx (pip install whisperx, plus a torch build for your "
            "platform — see the notes in requirements.txt) or faster-whisper, or "
            "set transcribe.source back to 'subtitles'."
        ) from None


def _to_segments(raw: list[dict]) -> list[Segment]:
    """Whisper result dicts -> Segments; entries without usable timings are dropped."""
    segs = []
    for i, s in enumerate(raw):
        text = str(s.get("text", "")).replace("\n", " ").strip()
        if not text or s.get("start") is None or s.get("end") is None:
            continue
        segs.append(
            Segment(
                index=i,
                start=float(s["start"]),
                end=float(s["end"]),
                text_src=text,
                words=s.get("words", []),
            )
        )
    return segs


def _whisperx_segments(job: DubJob, audio: Path, whisper_model: str) -> list[Segment]:
    import whisperx  # lazy — heavy ML dep

    try:
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    options = job.transcription_options
    if options.get("device", "auto") != "auto":
        device = options["device"]
    if options.get("compute_type", "auto") != "auto":
        compute_type = options["compute_type"]
    keep = options.get("keep_models_loaded", False)
    log.info("whisperx transcribe (%s on %s) -> %s", whisper_model, device, audio.name)
    with pooled_model(
        ("whisperx", whisper_model, device, compute_type, job.source_lang),
        lambda: whisperx.load_model(
            whisper_model, device, compute_type=compute_type, language=job.source_lang
        ),
        keep,
    ) as model:
        kwargs = (
            {"batch_size": max(1, int(options["batch_size"]))} if "batch_size" in options else {}
        )
        result = model.transcribe(str(audio), **kwargs)
        del model
    # Align against the audio for word-accurate timings — fit_timing and diarize
    # downstream key off these start/end values.
    lang = result.get("language") or job.source_lang
    try:
        with pooled_model(
            ("align", lang, device),
            lambda: whisperx.load_align_model(language_code=lang, device=device),
            keep,
        ) as loaded:
            result = whisperx.align(result["segments"], loaded[0], loaded[1], str(audio), device)
            del loaded
    except Exception as exc:  # noqa: BLE001 — no align model for some languages; keep raw timings
        log.warning("whisperx alignment failed (%s) — using raw segment timings", exc)
    return _to_segments(result["segments"])


def _faster_whisper_segments(job: DubJob, audio: Path, whisper_model: str) -> list[Segment]:
    from faster_whisper import WhisperModel  # lazy — heavy ML dep

    log.info("faster-whisper transcribe (%s) -> %s", whisper_model, audio.name)
    options = job.transcription_options
    kwargs = {"device": options.get("device", "auto")}
    if options.get("compute_type", "auto") != "auto":
        kwargs["compute_type"] = options["compute_type"]
    with pooled_model(
        ("faster-whisper", whisper_model, str(kwargs)),
        lambda: WhisperModel(whisper_model, **kwargs),
        options.get("keep_models_loaded", False),
    ) as model:
        raw, _info = model.transcribe(str(audio), language=job.source_lang, word_timestamps=True)
        result = _to_segments(
            [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "words": [
                        {"start": w.start, "end": w.end, "word": w.word}
                        for w in (getattr(s, "words", None) or [])
                    ],
                }
                for s in raw
            ]
        )
        del model
    return result
