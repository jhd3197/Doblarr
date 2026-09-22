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
from ..cues import NORMALIZED, RAW, TRIMMED, Artifact, Finding, finding_id, now
from ..errors import JobCancelled
from ..ffmpeg import run_ffmpeg
from ..fingerprints import processing as processing_fingerprint
from ..fingerprints import verification as verification_fingerprint
from ..telemetry import write_json
from . import boundaries

ACOUSTIC_ISSUES = {"silence", "clipping", "unexpected_duration", "text_mismatch", "repetition"}

# Structured classification for the legacy issue strings. Severity is about
# consequence; confidence (where a detector can state one) is separate.
DETECTOR = "clip-checks/1"
ISSUE_KINDS = {
    "silence": ("technical", "error"),
    "clipping": ("technical", "error"),
    "unexpected_duration": ("timing", "warning"),
    "text_mismatch": ("content", "warning"),
    "repetition": ("content", "warning"),
}


def apply_findings(seg, detector: str, inputs: str, observed: list[tuple]) -> None:
    """Merge this detector's observations into the cue's structured findings.

    A finding is never silently resolved by new audio: when the inputs change,
    an accepted finding reopens with its history intact, and a code that is no
    longer reported becomes `obsolete` rather than disappearing.
    """
    codes = {code for code, *_ in observed}
    for found in seg.findings:
        if found.detector != detector:
            continue
        if found.code in codes:
            if found.inputs != inputs and found.disposition != "open":
                found.history.append({"at": now(), "from": found.disposition,
                                      "to": "open", "reason": "inputs changed"})
                found.disposition = "open"
            found.inputs = inputs
        elif found.disposition != "obsolete":
            found.history.append({"at": now(), "from": found.disposition,
                                  "to": "obsolete", "reason": "no longer detected"})
            found.disposition = "obsolete"
    known = {f.code for f in seg.findings if f.detector == detector}
    for code, kind, severity, confidence, evidence in observed:
        if code in known:
            continue
        seg.findings.append(Finding(
            finding_id=finding_id(seg.cue_id, code, detector),
            code=code, kind=kind, severity=severity, confidence=confidence,
            scope="render", target=RAW, detector=detector, inputs=inputs,
            evidence=evidence))


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
    # The exact spoken form synthesis resolved for this line (per its speaker's
    # engine); the legacy flat pronunciation map remains the fallback.
    text = seg.tts_text or spoken_form(seg.text_translated or seg.text_src, pronunciations or {})
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
        return saved["issues"], saved["stats"], True, verification_fingerprint(request)
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

        # A low similarity is evidence of a mismatch, not proof of an omission;
        # the ratio is kept so review can weigh it.
        ratio = SequenceMatcher(None, clean(text), clean(heard)).ratio()
        stats["asr_similarity"] = ratio
        if ratio < 0.55:
            issues.append("text_mismatch")
        words = heard.casefold().split()
        if len(words) > 12 and len(set(words)) / len(words) < 0.3:
            issues.append("repetition")
    write_json(receipt, {"request": request, "issues": issues, "stats": stats})
    return issues, stats, False, verification_fingerprint(request)


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
    budget=None,
    boundary_options=None,
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
            issues, stats, hit, checked = check_clip(
                seg, job.target_lang, vb, asr, pronunciations, cancel)
            key = "quality_cache_hits" if hit else "quality_checked"
            job.metrics[key] = job.metrics.get(key, 0) + 1
            seg.issues = [i for i in seg.issues if i not in ACOUSTIC_ISSUES] + issues
            apply_findings(seg, DETECTOR, checked, [
                (code, *ISSUE_KINDS.get(code, ("technical", "warning")),
                 stats.get("asr_similarity") if code == "text_mismatch" else None,
                 dict(stats))
                for code in issues
            ])
            retryable = set(issues) - {"unexpected_duration"}
            if not retryable or attempt >= attempts or regenerate is None:
                break
            # Retries share one budget with timing repairs and (later) extra
            # candidates, so nested stages cannot multiply provider requests.
            if budget is not None and not budget.charge("quality_retry"):
                job.metrics["quality_retries_refused"] = (
                    job.metrics.get("quality_retries_refused", 0) + 1)
                break
            seg.revision += 1
            if checkpoint:
                checkpoint()
            regenerate(seg)
            job.metrics["quality_retries"] = job.metrics.get("quality_retries", 0) + 1
        # Boundary preparation runs after the raw checks have had their say —
        # an empty or failed generation must be rejected, not cropped into
        # something that looks usable — and before anything measures the clip
        # against its slot.
        prepared = boundaries.prepare(seg, boundary_options, cancel)
        job.metrics[f"trim_{prepared.decision}"] = (
            job.metrics.get(f"trim_{prepared.decision}", 0) + 1)
        if prepared.decision == "trimmed":
            job.metrics["trimmed_seconds"] = round(
                job.metrics.get("trimmed_seconds", 0.0) + prepared.trimmed, 3)
        if normalize and "silence" not in issues:
            # Normalize from the most upstream immutable artifact there is: the
            # prepared derivative when boundaries were removed, otherwise the
            # raw take. Reprocessing an already normalized file would
            # accumulate gain run after run.
            raw = seg.audio.raw()
            upstream = seg.audio.render(TRIMMED) or raw
            source = (Path(upstream.path) if upstream and upstream.exists()
                      else Path(seg.audio_clip))
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
            seg.audio.put_render(Artifact(
                role=NORMALIZED,
                path=str(dest),
                # Identity of the processing, not of this machine's copy of it:
                # the generation it came from plus the settings applied. The
                # on-disk receipt still keys on the file, as it always has.
                fingerprint=processing_fingerprint({
                    "input": upstream.fingerprint if upstream else "",
                    "lufs": float(dialogue_lufs), "version": 1}),
                derived_from=upstream.role if upstream else "",
                bytes=dest.stat().st_size if dest.exists() else None,
            ))
            seg.audio_clip = dest
    job.metrics["quality_flags"] = sum(bool(s.issues) for s in job.segments)
