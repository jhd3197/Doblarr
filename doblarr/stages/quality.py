"""Clip diagnostics and optional ASR verification; raw generations remain intact."""

from __future__ import annotations

import math
import re
import sys
import wave
from array import array
from pathlib import Path

from .. import verify as content
from ..artifacts import digest, matches, read_json, record, stamp
from ..cues import (
    NORMALIZED,
    RAW,
    TRIMMED,
    Artifact,
    Finding,
    Verification,
    finding_id,
    now,
)
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


def recognizer_id(vb) -> str:
    """Best-effort identity of the recognizer, for the verification cache key.

    A client that cannot name its model is recorded as unknown rather than
    given a made-up version, so a real upgrade still invalidates evidence but a
    nameless stub does not pretend to be a pinned model.
    """
    if vb is None:
        return "none"
    name = type(vb).__name__
    for attribute in ("asr_model", "transcribe_model", "model_size"):
        value = getattr(vb, attribute, None)
        if value:
            return f"{name}/{value}"
    return f"{name}/unknown"


def verify_clip(seg, path: Path, language: str, vb, policy: str, reason: str,
                expected: str, budget=None, recognizer: str = "",
                audio_fingerprint: str = "", target: str = "") -> tuple[Verification, bool]:
    """Listen to one clip and compare it with what was asked for.

    Returns the verification and whether it was reused. Recognition is charged
    to the shared budget; a refusal leaves the line explicitly unchecked rather
    than silently verified. The only thing that can set `mismatch` here is the
    comparison in `doblarr.verify` — a service failure never becomes one.
    """
    recognizer = recognizer or recognizer_id(vb)
    key = content.verification_key(
        # The artifact's own fingerprint when the caller knows it; otherwise
        # the file stamp, which is at least stable for this machine.
        audio=audio_fingerprint or str(stamp(path)),
        expected=expected, language=language, policy=policy, recognizer=recognizer)
    if seg.verification.inputs == key and seg.verification.state != "unknown":
        return seg.verification, True
    receipt = Path(path).with_suffix(".verify.json")
    saved = read_json(receipt)
    if saved.get("key") == key and isinstance(saved.get("result"), dict):
        restored = Verification.from_dict(saved["result"])
        restored.attempts = seg.verification.attempts
        return restored, True
    if vb is None:
        return content.skipped(seg, policy, "no recognizer is configured",
                               language, expected), False
    if budget is not None and not budget.charge("asr"):
        return content.skipped(seg, policy, "the shared request budget is exhausted",
                               language, expected), False
    try:
        heard = vb.transcribe(Path(path), language=language) or {}
    except Exception as exc:  # noqa: BLE001 - any client failure is reviewable
        return content.failure(seg, policy, f"recognition failed: {exc}", language,
                               expected, recognizer), False
    if not isinstance(heard, dict):
        return content.failure(seg, policy, "recognition returned an unreadable result",
                               language, expected, recognizer), False
    comparison = content.compare(expected, str(heard.get("text") or ""), language,
                                 _confidence(heard),
                                 heard_language=str(heard.get("language") or ""))
    result = Verification(policy=policy, recognizer=recognizer, target=target,
                          inputs=key, at=now(), attempts=seg.verification.attempts,
                          **{k: v for k, v in comparison.items() if k in _VERIFY_FIELDS})
    if reason:
        result.reason = f"{result.reason} ({reason})"
    write_json(receipt, {"key": key, "result": result.as_dict()})
    return result, False


_VERIFY_FIELDS = frozenset({
    "state", "reason", "expected", "heard", "language", "tokenizer",
    "similarity", "confidence", "differences", "critical", "checker",
})


def _confidence(heard: dict) -> float | None:
    """Recognizer confidence when it supplies one; never a fabricated 1.0."""
    for key in ("confidence", "avg_logprob", "probability"):
        value = heard.get(key)
        if isinstance(value, int | float):
            # avg_logprob is a log probability; anything <= 0 is mapped through
            # exp so the scale is comparable, and anything else is taken as is.
            return round(min(1.0, max(0.0, math.exp(value) if value < 0 else float(value))), 4)
    return None


# The legacy string issues the new states map onto, so existing review filters,
# metrics and the `ACOUSTIC_ISSUES` contract keep working unchanged.
LEGACY_ISSUE = {"mismatch": "text_mismatch"}
# Issues the content check owns. They are recomputed from the verification on
# every pass, so a stale one from a cached acoustic receipt is dropped first.
_VERIFY_ISSUES = ("text_mismatch", "repetition")


def legacy_issues(result: Verification) -> list[str]:
    """The pre-Plan-03 issue strings implied by one verification."""
    issues = []
    code = LEGACY_ISSUE.get(result.state)
    if code:
        issues.append(code)
    if result.state == "mismatch" and "repeats" in result.reason:
        issues.append("repetition")
    return issues


def check_clip(seg, language, vb=None, asr="off", pronunciations=None, cancel=None,
               budget=None, reason="", verify=True, sample=0.0):
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
        # The acoustic measurement is reusable; the content check is not part
        # of this receipt and runs on its own cache, so a warm rerun still
        # reports verification instead of quietly leaving the line unchecked.
        issues, stats = list(saved["issues"]), dict(saved["stats"])
        if verify:
            issues = [i for i in issues if i not in _VERIFY_ISSUES]
            issues += _verify_here(seg, source, language, vb, asr, text, issues,
                                   budget, reason, stats, sample)
        return issues, stats, True, verification_fingerprint(request)
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
    write_json(receipt, {"request": request, "issues": issues, "stats": stats})
    if verify:
        issues += _verify_here(seg, source, language, vb, asr, text, issues, budget, reason,
                               stats, sample)
    return issues, stats, False, verification_fingerprint(request)


def verification_findings(seg) -> None:
    """Publish this cue's verification as structured findings.

    Kept out of `verify_clip` so a caller can compare without touching the cue,
    and so the findings for a reused verification are refreshed on every pass.
    """
    apply_findings(seg, content.CHECKER, seg.verification.inputs,
                   content.findings_for(seg.verification))


def _verify_here(seg, source, language, vb, asr, text, issues, budget, reason, stats,
                 sample=0.0):
    """Run the content check for this clip and fold its verdict into `issues`.

    Kept separate from the acoustic receipt on purpose: a checker-policy change
    must re-verify without re-measuring or regenerating anything, and an
    acoustic re-measure must not silently discard recognition evidence.
    """
    wanted, why = content.should_check(seg, asr, key=seg.cue_id, sample=sample,
                                       acoustic_issues=issues)
    if not wanted:
        seg.verification = content.skipped(seg, asr, why, language, text)
        return []
    result, _reused = verify_clip(
        seg, Path(source), language, vb, asr, reason or why, text, budget=budget,
        audio_fingerprint=_listened_fingerprint(seg), target=_listened_role(seg))
    seg.verification = result
    if result.similarity is not None:
        stats["asr_similarity"] = result.similarity
    return legacy_issues(result)


def _listened_fingerprint(seg) -> str:
    current = seg.audio.current()
    if current and current.fingerprint:
        return current.fingerprint
    raw = seg.audio.raw()
    return raw.fingerprint if raw else ""


def _listened_role(seg) -> str:
    current = seg.audio.current()
    return current.role if current else RAW


def coverage(job) -> dict:
    """What verification actually covered, in terms a reviewer can audit.

    `checked` counts lines recognition produced a verdict for. Every other
    line is counted under the state that explains why it did not, because
    reporting an unlistened line as verified is the one thing this must never
    do.
    """
    states: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for seg in job.segments:
        states[seg.verification.state] = states.get(seg.verification.state, 0) + 1
        if not seg.verification.checked and seg.verification.reason:
            reasons[seg.verification.reason] = reasons.get(seg.verification.reason, 0) + 1
    checked = sum(1 for s in job.segments if s.verification.checked)
    return {
        "policy": next((s.verification.policy for s in job.segments), "off"),
        "lines": len(job.segments),
        "checked": checked,
        "unchecked": len(job.segments) - checked,
        "states": dict(sorted(states.items())),
        "reasons": dict(sorted(reasons.items())[:12]),
    }


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
    own_levels=False,
    sample=0.0,
):
    """Check every generated clip, verify its words, and prepare its boundaries.

    `own_levels` hands loudness to the post-fit level owner (Plan 03): the
    pre-fit normalization below is the legacy path and the two never both run,
    because a second loudness pass after a performance gain would erase it.
    """
    if dry_run or not enabled:
        return
    if asr not in content.POLICIES:
        raise ValueError("quality.asr must be off, suspicious or all")
    attempts = max(0, min(3, int(max_retries)))
    for seg in job.segments:
        for attempt in range(attempts + 1):
            if cancel is not None and cancel.is_set():
                raise JobCancelled("cancelled during clip checks")
            issues, stats, hit, checked = check_clip(
                seg, job.target_lang, vb, asr, pronunciations, cancel,
                budget=budget, sample=sample)
            key = "quality_cache_hits" if hit else "quality_checked"
            job.metrics[key] = job.metrics.get(key, 0) + 1
            seg.issues = [i for i in seg.issues if i not in ACOUSTIC_ISSUES] + issues
            apply_findings(seg, DETECTOR, checked, [
                (code, *ISSUE_KINDS.get(code, ("technical", "warning")),
                 stats.get("asr_similarity") if code == "text_mismatch" else None,
                 dict(stats))
                for code in issues
            ])
            # The structured content findings carry the ordered differences the
            # legacy strings cannot; both are published so old filters keep
            # working while review can show where a word actually went.
            verification_findings(seg)
            retryable = set(issues) - {"unexpected_duration"}
            if not retryable or attempt >= attempts or regenerate is None:
                break
            # Retries share one budget with timing repairs and extra candidate
            # takes, so nested stages cannot multiply provider requests.
            if budget is not None and not budget.charge("quality_retry"):
                job.metrics["quality_retries_refused"] = (
                    job.metrics.get("quality_retries_refused", 0) + 1)
                seg.verification.reason = (
                    f"{seg.verification.reason}; repair stopped: budget exhausted")
                break
            seg.revision += 1
            seg.verification.attempts += 1
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
        if normalize and not own_levels and "silence" not in issues:
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
        elif own_levels:
            # The level owner reads the prepared (or raw) take directly, so any
            # normalized derivative left behind by a legacy run is not an input
            # to anything any more and must not reach the mix.
            seg.audio.drop_renders((NORMALIZED,))
            upstream = seg.audio.render(TRIMMED) or seg.audio.raw()
            if upstream and upstream.exists():
                seg.audio_clip = Path(upstream.path)
    job.metrics["quality_flags"] = sum(bool(s.issues) for s in job.segments)
    job.metrics["verification"] = coverage(job)
