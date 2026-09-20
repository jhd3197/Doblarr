"""Clip diagnostics and optional ASR verification; raw generations remain intact."""

from __future__ import annotations

import math
import re
import sys
import wave
from array import array
from difflib import SequenceMatcher
from pathlib import Path

from ..artifacts import digest, matches, read_json, record, stamp
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..telemetry import write_json

ACOUSTIC_ISSUES = {"silence", "clipping", "unexpected_duration", "text_mismatch", "repetition"}


def inspect_pcm(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise wave.Error("analysis requires 16-bit PCM")
        count, clipped, squares, peak = 0, 0, 0, 0
        while raw := audio.readframes(65536):
            samples = array("h", raw)
            if sys.byteorder != "little":
                samples.byteswap()
            count += len(samples)
            squares += sum(v * v for v in samples)
            clipped += sum(abs(v) >= 32760 for v in samples)
            peak = max(peak, max((abs(v) for v in samples), default=0))
        rms = math.sqrt(squares / max(1, count)) / 32768
        return {
            "duration": audio.getnframes() / audio.getframerate(),
            "rms_db": 20 * math.log10(max(rms, 1e-10)),
            "peak": peak / 32768,
            "clipped_fraction": clipped / max(1, count),
        }


def spoken_form(text: str, pronunciations: dict) -> str:
    # Longest term first; substitutions occur once, so replacements cannot cascade.
    if not pronunciations:
        return text
    terms = sorted((k for k in pronunciations if k), key=len, reverse=True)
    if not terms:
        return text
    pattern = r"(?<!\w)(?:" + "|".join(re.escape(k) for k in terms) + r")(?!\w)"
    return re.sub(pattern, lambda match: pronunciations[match.group()], text)


def check_clip(seg, language, vb=None, asr="off", pronunciations=None, cancel=None):
    source = Path(seg.audio_clip)
    text = spoken_form(seg.text_translated or seg.text_src, pronunciations or {})
    request = {
        "source": stamp(source),
        "text": text,
        "slot": seg.duration,
        "language": language,
        "asr": asr,
        "version": 1,
    }
    receipt = source.with_suffix(".quality.json")
    saved = read_json(receipt)
    if saved.get("request") == request:
        return saved["issues"], saved["stats"], True
    try:
        stats = inspect_pcm(source)
    except (wave.Error, EOFError):
        converted = source.with_name(source.stem + ".analysis.wav")
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(source),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(converted),
            ],
            cancel=cancel,
        )
        stats = inspect_pcm(converted)
        converted.unlink(missing_ok=True)
    issues = []
    if stats["rms_db"] < -55:
        issues.append("silence")
    if stats["clipped_fraction"] > 0.001:
        issues.append("clipping")
    if stats["duration"] < 0.15 or stats["duration"] > max(2, seg.duration * 2):
        issues.append("unexpected_duration")
    if vb and (asr == "all" or (asr == "suspicious" and issues)):
        heard = vb.transcribe(source, language=language).get("text", "")

        def clean(value):
            return re.sub(r"[\W_]", "", value.casefold())

        if SequenceMatcher(None, clean(text), clean(heard)).ratio() < 0.55:
            issues.append("text_mismatch")
        words = heard.casefold().split()
        if len(words) > 12 and len(set(words)) / len(words) < 0.3:
            issues.append("repetition")
    write_json(receipt, {"request": request, "issues": issues, "stats": stats})
    return issues, stats, False


def run(
    job,
    vb=None,
    enabled=True,
    normalize=True,
    dialogue_lufs=-18,
    asr="off",
    max_retries=1,
    regenerate=None,
    checkpoint=None,
    pronunciations=None,
    cancel=None,
    dry_run=False,
):
    if dry_run or not enabled:
        return
    if asr not in {"off", "suspicious", "all"}:
        raise ValueError("quality.asr must be off, suspicious or all")
    attempts = max(0, min(3, int(max_retries)))
    for seg in job.segments:
        for attempt in range(attempts + 1):
            if cancel is not None and cancel.is_set():
                raise JobCancelled("cancelled during clip checks")
            issues, stats, hit = check_clip(seg, job.target_lang, vb, asr, pronunciations, cancel)
            key = "quality_cache_hits" if hit else "quality_checked"
            job.metrics[key] = job.metrics.get(key, 0) + 1
            seg.issues = [i for i in seg.issues if i not in ACOUSTIC_ISSUES] + issues
            retryable = set(issues) - {"unexpected_duration"}
            if not retryable or attempt >= attempts or regenerate is None:
                break
            seg.revision += 1
            if checkpoint:
                checkpoint()
            regenerate(seg)
            job.metrics["quality_retries"] = job.metrics.get("quality_retries", 0) + 1
        if normalize and "silence" not in issues:
            source = Path(seg.audio_clip)
            request = {"source": stamp(source), "lufs": float(dialogue_lufs), "version": 1}
            dest = source.parent / "normalized" / f"{source.stem}.{digest(request)[:12]}.wav"
            if not matches([dest], request):
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_suffix(".partial.wav")
                run_ffmpeg(
                    [
                        "-y",
                        "-i",
                        str(source),
                        "-af",
                        f"loudnorm=I={float(dialogue_lufs):g}:TP=-2:LRA=7",
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
            seg.audio_clip = dest
    job.metrics["quality_flags"] = sum(bool(s.issues) for s in job.segments)
